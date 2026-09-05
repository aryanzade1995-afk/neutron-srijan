"""Temporal transaction graph and the chain-walk that finds the end node.

Accounts are nodes, transactions are time-stamped directed edges. A mule chain is
a *temporal path*: each hop must happen after the money arrived, inside a short
window, and forward most of the amount. The walk stays deliberately rule-based --
a bank has to be able to justify a freeze, so the traversal is deterministic and
every hop carries the numbers that admitted it.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import networkx as nx
import pandas as pd

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

# Defaults tuned on the synthetic set; all three are overridable per request so an
# investigator can loosen the window on a stale complaint.
MIN_FORWARD_PCT = 0.70
MAX_GAP_HOURS = 48.0
MAX_HOPS = 10
CASHOUT_MODES = {"ATM"}

# A transfer that moves substantially MORE than arrived is the account's own money,
# not the victim's moving on. Without this ceiling the walk hops onto unrelated
# larger transfers and reports impossible forward percentages.
MAX_FORWARD_PCT = 1.25


@dataclass
class WalkParams:
    min_forward_pct: float = MIN_FORWARD_PCT
    max_gap_hours: float = MAX_GAP_HOURS
    max_hops: int = MAX_HOPS
    max_forward_pct: float = MAX_FORWARD_PCT
    allow_splits: bool = True


@dataclass
class Hop:
    hop: int
    txn_id: str
    source: str
    target: str
    amount: float
    timestamp: str
    gap_minutes: float
    forward_pct: float
    mode: str
    split_of: int = 0          # >1 when the hop was one leg of a split transfer
    siblings: list[dict] = field(default_factory=list)


class TransactionGraph:
    """In-memory temporal graph over the transaction feed."""

    def __init__(self, txns: pd.DataFrame, accounts: pd.DataFrame) -> None:
        self.txns = txns.copy()
        self.txns["ts"] = pd.to_datetime(self.txns["timestamp"], format="ISO8601", utc=True)
        self.accounts = accounts.set_index("account_id")

        self.graph = nx.MultiDiGraph()
        for account_id, row in self.accounts.iterrows():
            self.graph.add_node(account_id, **row.to_dict())

        self._by_id: dict[str, dict] = {}
        self._out: dict[str, list[dict]] = defaultdict(list)
        self._in: dict[str, list[dict]] = defaultdict(list)

        for row in self.txns.itertuples(index=False):
            edge = {
                "txn_id": row.txn_id,
                "sender": row.sender,
                "receiver": row.receiver,
                "amount": float(row.amount),
                "ts": row.ts.to_pydatetime(),
                "timestamp": row.timestamp,
                "mode": row.mode,
                "chain_id": row.chain_id if isinstance(row.chain_id, str) else "",
                "is_fraud": int(row.is_fraud),
            }
            self._by_id[row.txn_id] = edge
            self._out[row.sender].append(edge)
            self._in[row.receiver].append(edge)
            if not self.graph.has_node(row.sender):
                self.graph.add_node(row.sender)
            if not self.graph.has_node(row.receiver):
                self.graph.add_node(row.receiver)
            self.graph.add_edge(row.sender, row.receiver, key=row.txn_id, **edge)

        for edges in self._out.values():
            edges.sort(key=lambda e: e["ts"])
        for edges in self._in.values():
            edges.sort(key=lambda e: e["ts"])

        self.window_start = self.txns["ts"].min().to_pydatetime()
        self.window_end = self.txns["ts"].max().to_pydatetime()

    # ---------- lookups ----------

    def txn(self, txn_id: str) -> dict | None:
        return self._by_id.get(txn_id)

    def outgoing(self, account: str) -> list[dict]:
        return self._out.get(account, [])

    def incoming(self, account: str) -> list[dict]:
        return self._in.get(account, [])

    def account_meta(self, account: str) -> dict:
        if account in self.accounts.index:
            meta = self.accounts.loc[account].to_dict()
            meta["account_id"] = account
            return meta
        return {"account_id": account, "account_type": "unknown", "is_mule": 0,
                "account_age_days": None, "kyc_tier": None, "bank": account.split("@")[-1].upper()}

    def is_cashout(self, account: str, via_mode: str = "") -> bool:
        if via_mode in CASHOUT_MODES:
            return True
        return self.account_meta(account).get("account_type") in ("cashout", "merchant")

    # ---------- the chain walk ----------

    def _candidates(self, account: str, arrived_at: datetime, amount: float,
                    params: WalkParams) -> list[dict]:
        """Outgoing transfers that could plausibly be the same money moving on."""
        deadline = arrived_at + timedelta(hours=params.max_gap_hours)
        floor = amount * params.min_forward_pct
        ceiling = amount * params.max_forward_pct
        return [e for e in self._out.get(account, [])
                if arrived_at < e["ts"] <= deadline and floor <= e["amount"] <= ceiling]

    def _split_legs(self, account: str, arrived_at: datetime, amount: float,
                    params: WalkParams) -> list[dict]:
        """Smurfing guard: several small transfers that together move the money on.

        Breaking one hop into five is the cheapest way to duck a percentage
        threshold, so the walk also aggregates everything leaving inside the window.
        """
        deadline = arrived_at + timedelta(hours=params.max_gap_hours)
        ceiling = amount * params.max_forward_pct
        legs = [e for e in self._out.get(account, [])
                if arrived_at < e["ts"] <= deadline and e["amount"] <= ceiling]
        if not 2 <= len(legs) <= 12:
            return []
        legs = sorted(legs, key=lambda e: -e["amount"])[:12]
        total = sum(e["amount"] for e in legs)
        if not amount * params.min_forward_pct <= total <= ceiling:
            return []
        return legs

    @staticmethod
    def _rank(candidate: dict, arrived_at: datetime, amount: float,
              params: WalkParams) -> float:
        """Prefer the leg that carries the most money, soonest."""
        forward = min(candidate["amount"] / amount, 1.0) if amount else 0.0
        gap_hours = (candidate["ts"] - arrived_at).total_seconds() / 3600.0
        speed = max(0.0, 1.0 - gap_hours / params.max_gap_hours)
        return 0.65 * forward + 0.35 * speed

    def chain_walk(self, txn_id: str, params: WalkParams | None = None) -> dict:
        """Follow one flagged transaction forward in time until the money stops."""
        params = params or WalkParams()
        entry = self.txn(txn_id)
        if entry is None:
            raise KeyError(f"unknown transaction {txn_id}")

        hops: list[Hop] = [Hop(
            hop=0, txn_id=entry["txn_id"], source=entry["sender"], target=entry["receiver"],
            amount=entry["amount"], timestamp=entry["timestamp"], gap_minutes=0.0,
            forward_pct=1.0, mode=entry["mode"],
        )]

        current = entry["receiver"]
        amount = entry["amount"]
        arrived_at = entry["ts"]
        visited = {entry["sender"], entry["receiver"]}
        reason = "no_qualifying_hop"
        splits_seen = 0

        while len(hops) <= params.max_hops:
            if self.is_cashout(current, hops[-1].mode):
                reason = "cash_out"
                break

            candidates = self._candidates(current, arrived_at, amount, params)
            split_legs: list[dict] = []
            if not candidates and params.allow_splits:
                split_legs = self._split_legs(current, arrived_at, amount, params)
                candidates = split_legs

            had_candidates = bool(candidates)
            candidates = [c for c in candidates if c["receiver"] not in visited]
            if not candidates:
                # money looping back to an account already on the path is a ring,
                # not a new hop - stop rather than walk in circles
                reason = "cycle" if had_candidates else "no_qualifying_hop"
                break

            best = max(candidates, key=lambda c: self._rank(c, arrived_at, amount, params))
            if split_legs:
                splits_seen += 1

            gap = (best["ts"] - arrived_at).total_seconds() / 60.0
            hops.append(Hop(
                hop=len(hops), txn_id=best["txn_id"], source=best["sender"],
                target=best["receiver"], amount=best["amount"], timestamp=best["timestamp"],
                gap_minutes=round(gap, 1),
                forward_pct=round(best["amount"] / amount, 4) if amount else 0.0,
                mode=best["mode"],
                split_of=len(split_legs),
                siblings=[{"txn_id": s["txn_id"], "receiver": s["receiver"],
                           "amount": s["amount"], "timestamp": s["timestamp"]}
                          for s in split_legs if s["txn_id"] != best["txn_id"]][:6],
            ))

            current = best["receiver"]
            amount = best["amount"]
            arrived_at = best["ts"]
            visited.add(current)
        else:
            reason = "max_hops"

        if self.is_cashout(current, hops[-1].mode):
            reason = "cash_out"

        elapsed = (arrived_at - entry["ts"]).total_seconds() / 60.0
        return {
            "entry_txn_id": entry["txn_id"],
            "victim": entry["sender"],
            "end_node": current,
            "end_reason": reason,
            "recoverable": reason != "cash_out",
            "hop_count": len(hops) - 1,
            "amount_in": round(entry["amount"], 2),
            "amount_at_end": round(amount, 2),
            "leakage_pct": round(1 - (amount / entry["amount"]), 4) if entry["amount"] else 0.0,
            "elapsed_minutes": round(elapsed, 1),
            "splits_detected": splits_seen,
            "first_seen": entry["timestamp"],
            "last_seen": arrived_at.isoformat(),
            "hops": [h.__dict__ for h in hops],
            "path": [entry["sender"]] + [h.target for h in hops],
            "ground_truth_chain": entry["chain_id"] or None,
        }

    # ---------- proactive scan ----------

    def scan(self, params: WalkParams | None = None, min_amount: float = 15000.0,
             min_hops: int = 3, limit: int = 40, lookback_days: float | None = None,
             risk_lookup=None) -> list[dict]:
        """Run the walk across the whole recent feed, before any complaint exists.

        Returns maximal chains only - a chain and its own tail are the same money,
        so the suffix is dropped rather than shown twice.
        """
        params = params or WalkParams()
        cutoff = (self.window_end - timedelta(days=lookback_days)) if lookback_days else None

        seeds = [e for e in self._by_id.values()
                 if e["amount"] >= min_amount
                 and e["mode"] == "P2P"
                 and (cutoff is None or e["ts"] >= cutoff)]
        seeds.sort(key=lambda e: -e["amount"])

        found: list[dict] = []
        for edge in seeds[:2500]:
            trace = self.chain_walk(edge["txn_id"], params)
            if trace["hop_count"] >= min_hops:
                found.append(trace)

        found.sort(key=lambda t: (-t["hop_count"], -t["amount_at_end"]))
        maximal: list[dict] = []
        covered: list[set[str]] = []
        for trace in found:
            nodes = set(trace["path"])
            if any(nodes <= seen for seen in covered):
                continue
            covered.append(nodes)
            maximal.append(trace)

        for trace in maximal:
            trace["risk"] = self.score_chain(trace, risk_lookup)
        maximal.sort(key=lambda t: -t["risk"]["score"])
        return maximal[:limit]

    def score_chain(self, trace: dict, risk_lookup=None) -> dict:
        """Blend the graph evidence with the ML mule score into one 0-100 priority."""
        hop_count = trace["hop_count"]
        gaps = [h["gap_minutes"] for h in trace["hops"][1:]] or [0.0]
        median_gap = sorted(gaps)[len(gaps) // 2]
        forwards = [h["forward_pct"] for h in trace["hops"][1:]] or [0.0]
        avg_forward = sum(forwards) / len(forwards)

        # a chain whose hops land within ~3 hours of each other is moving at mule speed
        velocity = max(0.0, 1.0 - median_gap / 180.0)
        depth = min(hop_count / 5.0, 1.0)
        value = min(trace["amount_at_end"] / 200000.0, 1.0)

        mule_scores = []
        if risk_lookup is not None:
            mule_scores = [risk_lookup(a) for a in trace["path"][1:]]
            mule_scores = [m for m in mule_scores if m is not None]
        ml = sum(mule_scores) / len(mule_scores) if mule_scores else None

        graph_score = 100 * (0.30 * depth + 0.25 * velocity + 0.25 * avg_forward + 0.20 * value)
        score = graph_score if ml is None else 0.6 * graph_score + 0.4 * (ml * 100)

        return {
            "score": round(min(score, 100.0), 1),
            "graph_score": round(graph_score, 1),
            "ml_mule_score": round(ml, 4) if ml is not None else None,
            "median_gap_minutes": round(median_gap, 1),
            "avg_forward_pct": round(avg_forward, 4),
            "band": "critical" if score >= 75 else "high" if score >= 55 else "medium" if score >= 35 else "low",
            "explanation": (
                f"{hop_count} hops, median {median_gap:.0f} min between hops, "
                f"{avg_forward * 100:.0f}% of funds forwarded each hop, "
                f"Rs {trace['amount_at_end']:,.0f} at the end node"
            ),
        }


def load_graph(data_dir: Path = DATA_DIR) -> TransactionGraph:
    txns = pd.read_csv(data_dir / "transactions.csv", keep_default_na=False)
    accounts = pd.read_csv(data_dir / "accounts.csv")
    return TransactionGraph(txns, accounts)
