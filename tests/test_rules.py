"""Each rule fires on the behaviour it claims to, and stays quiet otherwise.

The rule engine decides what gets traced at all, so a rule that fires on
ordinary activity wastes an investigator's time, and one that stays quiet on a
real pattern hides fraud. Both directions are tested.

Graphs here are hand-built so a failure names one rule rather than pointing at a
20,000-row dataset.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

import rules
from rules import RuleEngine
from conftest import build_graph, ts

WINDOW = 48.0
HOUR = 60.0


def routine(account: str, other: str, n: int, start_min: float,
            amount: float = 2_000.0, step_min: float = 60.0) -> list[dict]:
    """Regular, unremarkable history for one account."""
    return [{"txn_id": f"H{account}{i}", "sender": account, "receiver": other,
             "amount": amount, "timestamp": ts(start_min + i * step_min)}
            for i in range(n)]


def engine_for(txns, **kw):
    graph = build_graph(txns)
    return RuleEngine(graph, recent_hours=kw.pop("recent_hours", WINDOW), **kw)


def fired(alerts, account: str) -> set[str]:
    for alert in alerts:
        if alert.account == account:
            return {t.rule for t in alert.triggers}
    return set()


# History must sit entirely *outside* the recent window, or the engine has no
# baseline to call anything unusual against. 20 days back, compressed into a few
# hours, keeps it clear of a 48h window.
HIST_START = -20 * 24 * HOUR
RECENT = -1 * HOUR


def test_steady_account_raises_nothing():
    """The baseline case: consistent behaviour must not alert."""
    txns = routine("alice", "bob", 12, HIST_START)
    txns.append({"txn_id": "R1", "sender": "alice", "receiver": "bob",
                 "amount": 2_000.0, "timestamp": ts(RECENT)})
    alerts = engine_for(txns).evaluate()
    assert fired(alerts, "alice") == set()


def test_large_flow_is_relative_to_the_account():
    txns = routine("alice", "bob", 12, HIST_START)          # ~2k each
    txns.append({"txn_id": "BIG", "sender": "carol", "receiver": "alice",
                 "amount": 400_000.0, "timestamp": ts(RECENT)})
    assert "LARGE_FLOW" in fired(engine_for(txns).evaluate(), "alice")


def test_large_for_one_account_is_normal_for_another():
    """The same amount must not alert an account that always moves that much."""
    txns = routine("whale", "bob", 12, HIST_START, amount=380_000.0)
    txns.append({"txn_id": "R", "sender": "whale", "receiver": "bob",
                 "amount": 400_000.0, "timestamp": ts(RECENT)})
    assert "LARGE_FLOW" not in fired(engine_for(txns).evaluate(), "whale")


def test_trivial_amounts_never_alert_however_unusual():
    """A 10x jump on pocket change is not a fraud signal."""
    txns = routine("alice", "bob", 12, HIST_START, amount=50.0)
    txns.append({"txn_id": "R", "sender": "alice", "receiver": "bob",
                 "amount": 900.0, "timestamp": ts(RECENT)})
    assert fired(engine_for(txns).evaluate(), "alice") == set()


def test_pass_through_fires_on_fast_high_percentage_forwarding():
    txns = routine("mule", "shop", 8, HIST_START)
    txns += [
        {"txn_id": "IN", "sender": "victim", "receiver": "mule",
         "amount": 200_000.0, "timestamp": ts(RECENT)},
        {"txn_id": "OUT", "sender": "mule", "receiver": "next",
         "amount": 186_000.0, "timestamp": ts(RECENT + 12)},
    ]
    assert "PASS_THROUGH" in fired(engine_for(txns).evaluate(), "mule")


def test_pass_through_ignores_money_that_stays_put():
    txns = routine("saver", "shop", 8, HIST_START)
    txns.append({"txn_id": "IN", "sender": "payer", "receiver": "saver",
                 "amount": 200_000.0, "timestamp": ts(RECENT)})
    assert "PASS_THROUGH" not in fired(engine_for(txns).evaluate(), "saver")


def test_pass_through_ignores_a_small_onward_payment():
    """Receiving a salary then buying lunch is not layering."""
    txns = routine("worker", "shop", 8, HIST_START)
    txns += [
        {"txn_id": "IN", "sender": "employer", "receiver": "worker",
         "amount": 200_000.0, "timestamp": ts(RECENT)},
        {"txn_id": "OUT", "sender": "worker", "receiver": "shop",
         "amount": 900.0, "timestamp": ts(RECENT + 30)},
    ]
    assert "PASS_THROUGH" not in fired(engine_for(txns).evaluate(), "worker")


def test_new_counterparties_fires_on_unfamiliar_parties():
    txns = routine("alice", "bob", 10, HIST_START)
    txns += [{"txn_id": f"N{i}", "sender": "alice", "receiver": f"stranger{i}",
              "amount": 3_000.0, "timestamp": ts(RECENT + i)} for i in range(4)]
    assert "NEW_COUNTERPARTIES" in fired(engine_for(txns).evaluate(), "alice")


def test_familiar_counterparties_do_not_fire():
    txns = routine("alice", "bob", 10, HIST_START)
    txns += [{"txn_id": f"N{i}", "sender": "alice", "receiver": "bob",
              "amount": 3_000.0, "timestamp": ts(RECENT + i)} for i in range(4)]
    assert "NEW_COUNTERPARTIES" not in fired(engine_for(txns).evaluate(), "alice")


def test_debit_credit_shift_fires_when_direction_reverses():
    """Only ever received before; now only sends."""
    txns = [{"txn_id": f"H{i}", "sender": "payer", "receiver": "alice",
             "amount": 5_000.0, "timestamp": ts(HIST_START + i * 60)}
            for i in range(10)]
    txns += [{"txn_id": f"R{i}", "sender": "alice", "receiver": "bob",
              "amount": 5_000.0, "timestamp": ts(RECENT + i)} for i in range(4)]
    assert "DEBIT_CREDIT_SHIFT" in fired(engine_for(txns).evaluate(), "alice")


def test_dormant_wake_fires_after_a_long_quiet_period():
    txns = routine("sleeper", "bob", 8, -60 * 24 * HOUR)   # ~60 days ago
    txns.append({"txn_id": "WAKE", "sender": "payer", "receiver": "sleeper",
                 "amount": 250_000.0, "timestamp": ts(RECENT)})
    assert "DORMANT_WAKE" in fired(engine_for(txns).evaluate(), "sleeper")


def test_thin_history_fires_on_a_young_account_moving_real_money():
    graph = build_graph(
        [{"txn_id": "IN", "sender": "victim", "receiver": "fresh",
          "amount": 300_000.0, "timestamp": ts(RECENT)}],
        accounts=[{"account_id": "fresh", "account_age_days": 12, "kyc_tier": 1,
                   "account_type": "personal", "is_mule": 0, "bank": "TEST"}],
    )
    assert "THIN_HISTORY" in fired(RuleEngine(graph, recent_hours=WINDOW).evaluate(), "fresh")


def test_both_sides_of_a_transfer_are_assessed():
    """A mule is usually the beneficiary, so scoring only the payer misses it."""
    txns = routine("payer", "shop", 10, HIST_START)
    txns += routine("beneficiary", "shop", 10, HIST_START)
    txns.append({"txn_id": "BIG", "sender": "payer", "receiver": "beneficiary",
                 "amount": 500_000.0, "timestamp": ts(RECENT)})
    alerts = engine_for(txns).evaluate()
    assert "LARGE_FLOW" in fired(alerts, "payer")
    assert "LARGE_FLOW" in fired(alerts, "beneficiary")


def test_history_inside_the_window_is_not_used_as_its_own_baseline():
    """Everything recent means no baseline, so 'unusual' cannot be claimed."""
    txns = [{"txn_id": f"R{i}", "sender": "alice", "receiver": "bob",
             "amount": 2_000.0, "timestamp": ts(RECENT + i)} for i in range(10)]
    alerts = engine_for(txns).evaluate()
    assert "HIGH_VELOCITY" not in fired(alerts, "alice")


def test_mule_score_is_folded_in_when_supplied():
    txns = routine("alice", "bob", 10, HIST_START)
    txns.append({"txn_id": "R", "sender": "alice", "receiver": "bob",
                 "amount": 2_000.0, "timestamp": ts(RECENT)})
    alerts = engine_for(txns).evaluate(mule_scores=lambda a: 0.9)
    assert "MULE_SCORE" in fired(alerts, "alice")


def test_a_low_mule_score_does_not_fire():
    txns = routine("alice", "bob", 10, HIST_START)
    txns.append({"txn_id": "R", "sender": "alice", "receiver": "bob",
                 "amount": 2_000.0, "timestamp": ts(RECENT)})
    alerts = engine_for(txns).evaluate(mule_scores=lambda a: 0.1)
    assert "MULE_SCORE" not in fired(alerts, "alice")


# ---------- gating ----------

def test_monitored_is_a_subset_of_alerting():
    txns = routine("alice", "bob", 10, HIST_START)
    txns += [
        {"txn_id": "IN", "sender": "victim", "receiver": "mule",
         "amount": 300_000.0, "timestamp": ts(RECENT)},
        {"txn_id": "OUT", "sender": "mule", "receiver": "next",
         "amount": 280_000.0, "timestamp": ts(RECENT + 10)},
    ]
    engine = engine_for(txns)
    monitored, alerts = engine.monitored_accounts()
    assert monitored <= {a.account for a in alerts}
    assert all(a.score >= rules.MONITOR_THRESHOLD
               for a in alerts if a.account in monitored)


def test_a_clear_mule_chain_gets_monitored():
    txns = routine("mule", "shop", 8, HIST_START)
    txns += [
        {"txn_id": "IN", "sender": "victim", "receiver": "mule",
         "amount": 300_000.0, "timestamp": ts(RECENT)},
        {"txn_id": "OUT", "sender": "mule", "receiver": "next",
         "amount": 280_000.0, "timestamp": ts(RECENT + 10)},
    ]
    monitored, _ = engine_for(txns).monitored_accounts(mule_scores=lambda a: 0.8)
    assert "mule" in monitored


def test_summary_counts_line_up(simple_chain):
    engine = RuleEngine(simple_chain, recent_hours=24 * 365)
    alerts = engine.evaluate()
    summary = engine.summary(alerts)
    assert summary["accounts_alerting"] == len(alerts)
    assert summary["accounts_monitored"] == sum(1 for a in alerts if a.monitored)
    assert set(summary["rule_hits"]) == set(rules.RULE_WEIGHTS)


def test_scan_only_seeds_from_monitored_accounts(simple_chain):
    """The gate has to actually gate."""
    everything = simple_chain.scan(min_hops=1, limit=50)
    assert everything, "expected the ungated scan to find something"
    none_at_all = simple_chain.scan(min_hops=1, limit=50, monitored=set())
    assert none_at_all == []
