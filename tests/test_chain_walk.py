"""The rules that decide whether a hop is 'the same money moving on'.

Each test pins one admission rule, because these are exactly the decisions a
bank would have to justify when acting on a freeze recommendation.
"""
from __future__ import annotations

import pytest

from graph_engine import WalkParams
from conftest import build_graph, ts


def test_walks_to_the_end_of_the_chain(simple_chain):
    trace = simple_chain.chain_walk("T1")
    assert trace["end_node"] == "m3"
    assert trace["hop_count"] == 3
    assert trace["path"] == ["victim", "m1", "m2", "m3"]
    assert trace["end_reason"] == "no_qualifying_hop"
    assert trace["recoverable"] is True


def test_amount_at_end_tracks_the_last_hop(simple_chain):
    trace = simple_chain.chain_walk("T1")
    assert trace["amount_at_end"] == 85_000
    assert trace["amount_in"] == 100_000
    assert trace["leakage_pct"] == pytest.approx(0.15, abs=1e-6)


def test_will_not_follow_money_backwards_in_time():
    """An outgoing transfer that predates the inflow cannot be the same money."""
    graph = build_graph([
        {"txn_id": "OUT", "sender": "m1", "receiver": "m2", "amount": 95_000, "timestamp": ts(-30)},
        {"txn_id": "IN", "sender": "victim", "receiver": "m1", "amount": 100_000, "timestamp": ts(0)},
    ])
    trace = graph.chain_walk("IN")
    assert trace["end_node"] == "m1"
    assert trace["hop_count"] == 1  # entry transfer only


def test_ignores_hops_outside_the_time_window():
    graph = build_graph([
        {"txn_id": "IN", "sender": "victim", "receiver": "m1", "amount": 100_000, "timestamp": ts(0)},
        {"txn_id": "LATE", "sender": "m1", "receiver": "m2", "amount": 95_000,
         "timestamp": ts(60 * 72)},                       # 72h later, window is 48h
    ])
    assert graph.chain_walk("IN")["end_node"] == "m1"
    # widening the window admits the same hop
    loose = graph.chain_walk("IN", WalkParams(max_gap_hours=96))
    assert loose["end_node"] == "m2"


def test_ignores_hops_that_forward_too_little():
    graph = build_graph([
        {"txn_id": "IN", "sender": "victim", "receiver": "m1", "amount": 100_000, "timestamp": ts(0)},
        {"txn_id": "SMALL", "sender": "m1", "receiver": "m2", "amount": 20_000, "timestamp": ts(10)},
    ])
    assert graph.chain_walk("IN")["end_node"] == "m1"


def test_ignores_transfers_far_larger_than_what_arrived():
    """The ceiling rule: a much larger transfer is the account's own money.

    Without it the walk hops onto unrelated outflows and reports impossible
    forward percentages.
    """
    graph = build_graph([
        {"txn_id": "IN", "sender": "victim", "receiver": "m1", "amount": 20_000, "timestamp": ts(0)},
        {"txn_id": "BIG", "sender": "m1", "receiver": "m2", "amount": 4_000_000, "timestamp": ts(9)},
    ])
    trace = graph.chain_walk("IN")
    assert trace["end_node"] == "m1"
    assert all(h["forward_pct"] <= 1.25 for h in trace["hops"])


def test_every_admitted_hop_satisfies_all_three_rules(simple_chain):
    params = WalkParams()
    trace = simple_chain.chain_walk("T1", params)
    for prev, hop in zip(trace["hops"], trace["hops"][1:]):
        assert hop["gap_minutes"] > 0                                  # causal
        assert hop["gap_minutes"] <= params.max_gap_hours * 60         # inside window
        assert params.min_forward_pct <= hop["forward_pct"] <= params.max_forward_pct
        assert hop["source"] == prev["target"]                         # contiguous path


def test_terminates_at_a_cash_out():
    graph = build_graph(
        [
            {"txn_id": "IN", "sender": "victim", "receiver": "m1", "amount": 100_000, "timestamp": ts(0)},
            {"txn_id": "ATM", "sender": "m1", "receiver": "atm1", "amount": 95_000,
             "timestamp": ts(20), "mode": "ATM"},
        ],
        accounts=[{"account_id": "atm1", "account_age_days": 1200, "kyc_tier": 3,
                   "account_type": "cashout", "is_mule": 0, "bank": "TEST"}],
    )
    trace = graph.chain_walk("IN")
    assert trace["end_reason"] == "cash_out"
    assert trace["recoverable"] is False


def test_stops_on_a_cycle_rather_than_looping():
    graph = build_graph([
        {"txn_id": "IN", "sender": "victim", "receiver": "m1", "amount": 100_000, "timestamp": ts(0)},
        {"txn_id": "A", "sender": "m1", "receiver": "m2", "amount": 95_000, "timestamp": ts(10)},
        {"txn_id": "B", "sender": "m2", "receiver": "m1", "amount": 92_000, "timestamp": ts(20)},
        {"txn_id": "C", "sender": "m1", "receiver": "m2", "amount": 90_000, "timestamp": ts(30)},
    ])
    trace = graph.chain_walk("IN")
    assert trace["end_reason"] == "cycle"
    assert len(trace["path"]) == len(set(trace["path"]))


def test_respects_the_hop_ceiling():
    txns = [{"txn_id": "IN", "sender": "a0", "receiver": "a1", "amount": 100_000, "timestamp": ts(0)}]
    amount = 100_000.0
    for i in range(1, 15):
        amount *= 0.95
        txns.append({"txn_id": f"H{i}", "sender": f"a{i}", "receiver": f"a{i + 1}",
                     "amount": amount, "timestamp": ts(10 * i)})
    graph = build_graph(txns)
    trace = graph.chain_walk("IN", WalkParams(max_hops=4))
    assert trace["hop_count"] <= 4
    assert trace["end_reason"] == "max_hops"


def test_detects_a_split_transfer():
    """Smurfing one hop into several legs must not duck the percentage floor."""
    graph = build_graph([
        {"txn_id": "IN", "sender": "victim", "receiver": "m1", "amount": 100_000, "timestamp": ts(0)},
        {"txn_id": "S1", "sender": "m1", "receiver": "m2", "amount": 34_000, "timestamp": ts(5)},
        {"txn_id": "S2", "sender": "m1", "receiver": "m3", "amount": 31_000, "timestamp": ts(7)},
        {"txn_id": "S3", "sender": "m1", "receiver": "m4", "amount": 28_000, "timestamp": ts(9)},
    ])
    trace = graph.chain_walk("IN")
    assert trace["splits_detected"] == 1
    assert trace["hop_count"] == 2
    assert trace["end_node"] == "m2"                 # follows the largest leg
    assert trace["hops"][-1]["split_of"] == 3
    assert {s["txn_id"] for s in trace["hops"][-1]["siblings"]} == {"S2", "S3"}


def test_split_detection_can_be_disabled():
    graph = build_graph([
        {"txn_id": "IN", "sender": "victim", "receiver": "m1", "amount": 100_000, "timestamp": ts(0)},
        {"txn_id": "S1", "sender": "m1", "receiver": "m2", "amount": 34_000, "timestamp": ts(5)},
        {"txn_id": "S2", "sender": "m1", "receiver": "m3", "amount": 31_000, "timestamp": ts(7)},
        {"txn_id": "S3", "sender": "m1", "receiver": "m4", "amount": 28_000, "timestamp": ts(9)},
    ])
    trace = graph.chain_walk("IN", WalkParams(allow_splits=False))
    assert trace["end_node"] == "m1"
    assert trace["splits_detected"] == 0


def test_unknown_transaction_raises():
    graph = build_graph([
        {"txn_id": "IN", "sender": "a", "receiver": "b", "amount": 1000, "timestamp": ts(0)},
    ])
    with pytest.raises(KeyError):
        graph.chain_walk("NOPE")


def test_chain_score_is_bounded_and_banded(simple_chain):
    risk = simple_chain.score_chain(simple_chain.chain_walk("T1"))
    assert 0 <= risk["score"] <= 100
    assert risk["band"] in {"critical", "high", "medium", "low"}
    assert risk["explanation"]
