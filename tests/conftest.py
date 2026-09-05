"""Shared fixtures.

The graph tests build small hand-written networks rather than loading the
generated dataset, so a failure points at one rule instead of at 14k rows.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from graph_engine import TransactionGraph  # noqa: E402

T0 = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)


def ts(minutes: float) -> str:
    return (T0 + timedelta(minutes=minutes)).isoformat()


def build_graph(txns: list[dict], accounts: list[dict] | None = None) -> TransactionGraph:
    """Assemble a TransactionGraph from terse dict rows."""
    names = {t["sender"] for t in txns} | {t["receiver"] for t in txns}
    defaults = {a["account_id"]: a for a in (accounts or [])}
    rows = []
    for name in sorted(names):
        rows.append(defaults.get(name, {
            "account_id": name,
            "account_age_days": 900,
            "kyc_tier": 3,
            "account_type": "personal",
            "is_mule": 0,
            "bank": "TEST",
        }))

    frame = pd.DataFrame([{
        "txn_id": t["txn_id"],
        "sender": t["sender"],
        "receiver": t["receiver"],
        "amount": float(t["amount"]),
        "timestamp": t["timestamp"],
        "mode": t.get("mode", "P2P"),
        "chain_id": t.get("chain_id", ""),
        "hop_index": t.get("hop_index", -1),
        "is_fraud": t.get("is_fraud", 0),
    } for t in txns])

    return TransactionGraph(frame, pd.DataFrame(rows))


@pytest.fixture
def simple_chain() -> TransactionGraph:
    """victim -> m1 -> m2 -> m3, each hop fast and forwarding ~90%."""
    return build_graph([
        {"txn_id": "T1", "sender": "victim", "receiver": "m1", "amount": 100_000, "timestamp": ts(0)},
        {"txn_id": "T2", "sender": "m1", "receiver": "m2", "amount": 92_000, "timestamp": ts(12)},
        {"txn_id": "T3", "sender": "m2", "receiver": "m3", "amount": 85_000, "timestamp": ts(31)},
    ])
