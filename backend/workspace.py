"""Per-session datasets.

Every visitor gets their own synthetic network rather than all of them looking at
one canned dataset. A workspace is built from a seed, so it is stable for the
length of a session - refreshing mid-investigation must not reshuffle the case
you are looking at - while two people on the same demo see different networks.

Building one costs well under two seconds, so they are made on demand and kept in
a small LRU cache rather than pre-baked.
"""
from __future__ import annotations

import io
import json
import random
import time
import threading
import zlib
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from db import DB
from feedback import FeedbackStore
from generate_data import Generator
from graph_engine import TransactionGraph, WalkParams
from risk_model import RiskModel, build_features
from rules import RuleEngine
from store import STORE, keys_for

MAX_WORKSPACES = 12
FEEDBACK_DIR = Path(__file__).resolve().parent.parent / "data" / "feedback"

# Cached datasets outlive a session so a revisited seed is not rebuilt; patterns
# and traces expire sooner because they follow the current scan.
DATASET_TTL = 60 * 60 * 24
PATTERN_TTL = 60 * 60 * 6
TRACE_TTL = 60 * 30


def dataset_params(seed: int) -> dict:
    """Scale as well as content varies with the seed.

    If only the contents changed, every user would still see roughly the same
    totals on the dashboard and it would look canned.
    """
    rng = random.Random(seed)
    return {
        "n_normal": rng.randint(700, 1400),
        "n_merchants": rng.randint(90, 170),
        "n_txns": rng.randint(9_000, 22_000),
        "n_chains": rng.randint(28, 70),
        "n_forwarders": rng.randint(25, 55),
    }


@dataclass
class Workspace:
    seed: int
    graph: TransactionGraph
    features: pd.DataFrame
    model: RiskModel
    report: object
    truth: pd.DataFrame
    params: dict
    chains: list = field(default_factory=list)
    evaluation: dict | None = None
    feedback: FeedbackStore | None = None
    alerts: list = field(default_factory=list)      # rule-engine output
    monitored: set = field(default_factory=set)     # accounts worth tracing
    rule_summary: dict = field(default_factory=dict)

    @property
    def label(self) -> str:
        """Short human-facing id, so two people can tell they are on different data."""
        return f"DS-{self.seed % 100000:05d}"

    def run_rules(self) -> list:
        """Decide which accounts are worth watching before anything is traced."""
        engine = RuleEngine(self.graph)
        self.monitored, self.alerts = engine.monitored_accounts(self.model.score)
        self.rule_summary = engine.summary(self.alerts)
        return self.alerts

    def rescan(self, decorate) -> list:
        # The rule engine gates the scan: only transactions touching a monitored
        # account are seeded. High cap on purpose - the count has to reflect what
        # was actually detected, not a display limit.
        self.run_rules()
        chains = self.graph.scan(WalkParams(), min_hops=3, limit=500,
                                 risk_lookup=self.model.score,
                                 monitored=self.monitored)
        self.chains = [decorate(self, c) for c in chains]
        self.evaluation = None
        self.publish_patterns()
        return self.chains

    def publish_patterns(self) -> None:
        """Push the detection output into the store.

        Ranked chains go into a sorted set and per-account mule scores into a
        hash, so 'worst chains right now' and 'score this account' are one
        command each instead of a rescan. The scan and the classifier stay
        where they are - this publishes what they found.
        """
        k = keys_for(self.seed)
        STORE.delete(k["chains"], k["accounts"], k["mule"])

        if self.chains:
            STORE.zadd(k["chains"], {c["entry_txn_id"]: float(c["risk"]["score"])
                                     for c in self.chains})
            for chain in self.chains:
                STORE.set(k["chain"] + chain["entry_txn_id"],
                          json.dumps(chain).encode(), ttl=PATTERN_TTL)

        if self.model.scores is not None:
            scores = self.model.scores
            STORE.hset(k["mule"], {a: f"{s:.6f}".encode() for a, s in scores.items()})
            STORE.zadd(k["accounts"], {a: float(s) for a, s in scores.items()})

        STORE.hset(k["meta"], {
            "seed": str(self.seed).encode(),
            "transactions": str(len(self.graph.txns)).encode(),
            "accounts": str(len(self.graph.accounts)).encode(),
            "chains": str(len(self.chains)).encode(),
            "published_at": str(int(time.time())).encode(),
        })

    # ---- pattern lookups served straight from the store ----

    def ranked_chains(self, limit: int = 25) -> list[tuple[str, float]]:
        return STORE.zrevrange(keys_for(self.seed)["chains"], 0, limit - 1)

    def mule_score(self, account: str) -> float | None:
        raw = STORE.hget(keys_for(self.seed)["mule"], account)
        return float(raw) if raw else None

    def top_mule_accounts(self, limit: int = 20) -> list[tuple[str, float]]:
        return STORE.zrevrange(keys_for(self.seed)["accounts"], 0, limit - 1)

    def cached_trace(self, txn_id: str, params_key: str) -> dict | None:
        raw = STORE.get(keys_for(self.seed)["trace"] + f"{txn_id}:{params_key}")
        return json.loads(raw) if raw else None

    def cache_trace(self, txn_id: str, params_key: str, payload: dict) -> None:
        STORE.set(keys_for(self.seed)["trace"] + f"{txn_id}:{params_key}",
                  json.dumps(payload).encode(), ttl=TRACE_TTL)

    def invalidate_traces(self) -> None:
        keys = STORE.scan(keys_for(self.seed)["trace"] + "*")
        if keys:
            STORE.delete(*keys)

    def visible_chains(self, band: str | None = None,
                       include_dismissed: bool = False) -> list:
        """The working queue. One definition, so dashboard counts and table rows
        can never disagree."""
        items = self.chains
        if band:
            items = [c for c in items if c["risk"]["band"] == band]
        if not include_dismissed:
            items = [c for c in items
                     if (c.get("review") or {}).get("verdict") != "false_positive"]
        return items


def _serialise(frames: tuple[pd.DataFrame, ...]) -> bytes:
    buffer = io.BytesIO()
    for frame in frames:
        payload = frame.to_json(orient="split").encode()
        buffer.write(len(payload).to_bytes(4, "big"))
        buffer.write(payload)
    return zlib.compress(buffer.getvalue(), 6)


def _deserialise(blob: bytes, count: int) -> list[pd.DataFrame]:
    buffer = io.BytesIO(zlib.decompress(blob))
    frames = []
    for _ in range(count):
        size = int.from_bytes(buffer.read(4), "big")
        frames.append(pd.read_json(
            io.StringIO(buffer.read(size).decode()), orient="split",
            # left on, read_json turns the timestamp column into Timestamps and a
            # cached dataset stops matching a freshly generated one - the engine
            # keeps timestamps as ISO strings and parses them itself
            convert_dates=False, convert_axes=False))
    return frames


def load_dataset(seed: int) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, dict]:
    """PostgreSQL -> Redis -> graph.

    Postgres is the system of record. Redis holds a serialised working copy so a
    revisited dataset costs a deserialise rather than a SQL round trip, and the
    generator only runs for a seed nobody has produced yet - at which point the
    result is written back to Postgres so it is durable from then on.

    Every tier is optional in the downward direction: no Redis means read from
    Postgres each time, no Postgres means generate in memory. Neither absence
    stops the app, and /api/health reports which tiers are live.
    """
    params = dataset_params(seed)
    key = keys_for(seed)["dataset"]

    # 1. Redis working copy
    blob = STORE.get(key)
    if blob:
        try:
            txns, accounts, truth = _deserialise(blob, 3)
            return txns, accounts, truth, params
        except Exception:
            STORE.delete(key)          # unreadable cache entry must not be fatal

    # 2. PostgreSQL, the system of record
    loaded = DB.load(seed)
    if loaded is not None:
        txns, accounts, truth = loaded
        STORE.set(key, _serialise((txns, accounts, truth)), ttl=DATASET_TTL)
        return txns, accounts, truth, params

    # 3. Nobody has this seed yet - generate it, then persist so it is durable
    generator = Generator(seed=seed)
    txns, accounts, truth = generator.run(
        params["n_normal"], params["n_merchants"], params["n_txns"],
        params["n_chains"], params["n_forwarders"])

    DB.save(seed, f"DS-{seed % 100000:05d}", txns, accounts, truth, params)
    STORE.set(key, _serialise((txns, accounts, truth)), ttl=DATASET_TTL)
    return txns, accounts, truth, params


def build_workspace(seed: int) -> Workspace:
    txns, accounts, truth, params = load_dataset(seed)
    graph = TransactionGraph(txns, accounts)
    features = build_features(graph)
    model = RiskModel()
    report = model.fit(features)

    FEEDBACK_DIR.mkdir(parents=True, exist_ok=True)
    return Workspace(
        seed=seed, graph=graph, features=features, model=model, report=report,
        truth=truth, params=params,
        feedback=FeedbackStore(FEEDBACK_DIR / f"{seed}.jsonl"),
    )


class WorkspacePool:
    """LRU cache of live workspaces, keyed by seed."""

    def __init__(self, capacity: int = MAX_WORKSPACES) -> None:
        self.capacity = capacity
        self._items: OrderedDict[int, Workspace] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, seed: int, decorate) -> Workspace:
        with self._lock:
            existing = self._items.get(seed)
            if existing is not None:
                self._items.move_to_end(seed)
                return existing

        # built outside the lock so one slow build does not stall other sessions
        workspace = build_workspace(seed)
        workspace.rescan(decorate)

        with self._lock:
            if seed in self._items:                    # another thread won the race
                self._items.move_to_end(seed)
                return self._items[seed]
            self._items[seed] = workspace
            while len(self._items) > self.capacity:
                self._items.popitem(last=False)
            return workspace

    def drop(self, seed: int) -> None:
        with self._lock:
            self._items.pop(seed, None)

    def stats(self) -> dict:
        with self._lock:
            return {"live_workspaces": len(self._items),
                    "capacity": self.capacity,
                    "seeds": list(self._items)}
