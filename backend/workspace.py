"""Per-session datasets.

Every visitor gets their own synthetic network rather than all of them looking at
one canned dataset. A workspace is built from a seed, so it is stable for the
length of a session - refreshing mid-investigation must not reshuffle the case
you are looking at - while two people on the same demo see different networks.

Building one costs well under two seconds, so they are made on demand and kept in
a small LRU cache rather than pre-baked.
"""
from __future__ import annotations

import random
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from feedback import FeedbackStore
from generate_data import Generator
from graph_engine import TransactionGraph, WalkParams
from risk_model import RiskModel, build_features

MAX_WORKSPACES = 12
FEEDBACK_DIR = Path(__file__).resolve().parent.parent / "data" / "feedback"


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

    @property
    def label(self) -> str:
        """Short human-facing id, so two people can tell they are on different data."""
        return f"DS-{self.seed % 100000:05d}"

    def rescan(self, decorate) -> list:
        # High cap on purpose: the count has to reflect what was actually
        # detected. A low cap made every dataset report the same total and read
        # as a hardcoded number.
        chains = self.graph.scan(WalkParams(), min_hops=3, limit=500,
                                 risk_lookup=self.model.score)
        self.chains = [decorate(self, c) for c in chains]
        self.evaluation = None
        return self.chains

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


def build_workspace(seed: int) -> Workspace:
    params = dataset_params(seed)
    generator = Generator(seed=seed)
    txns, accounts, truth = generator.run(
        params["n_normal"], params["n_merchants"], params["n_txns"],
        params["n_chains"], params["n_forwarders"])

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
