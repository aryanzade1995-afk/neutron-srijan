"""Rule engine — decides which accounts are worth watching.

Chain tracing every transaction on a national rail is not a plan. This narrows
the field first: each account is scored against its **own** past behaviour over a
recent window, and only accounts that trip a rule get monitored. The chain walk
then seeds exclusively from transactions touching a monitored account.

The rules follow the factors an AML/fraud desk actually works from:

  LARGE_FLOW          a transfer far above what this account normally moves
  HIGH_VELOCITY       many more transactions than this account normally makes
  DEBIT_CREDIT_SHIFT  the balance of debits to credits departs from its norm
  NEW_COUNTERPARTIES  money moving to and from people it has never dealt with
  PASS_THROUGH        funds arriving and leaving fast, mostly intact
  DORMANT_WAKE        a long-quiet account suddenly transacting
  THIN_HISTORY        a young or barely-used account handling real money
  MULE_SCORE          the classifier's own view

Two things worth stating plainly:

* Every rule is **relative to the account**, not to a global threshold, except
  where an absolute floor is needed to stop trivial amounts generating noise.
  "Unusual" only means anything against a baseline.
* Both ends of a transfer are evaluated. A mule is often the *beneficiary* of the
  suspicious transaction and the sender of a perfectly ordinary-looking one, so
  scoring only the payer misses the account that matters.
"""
from __future__ import annotations

import os
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta

# How much of the feed counts as "now". Everything before it forms the baseline.
#
# This is the main operational dial. Measured on the reference dataset, trading
# how much of the network is watched against how much fraud is caught:
#
#     window   monitored   of network   chains   recall   precision
#       48 h          76        6.6%        31    36.4%       80.6%
#      168 h         318       27.4%        80    86.4%       73.8%
#      336 h         536       46.2%        87    93.9%       73.6%
#      720 h         462       39.8%        86    95.5%       75.6%
#
# 48 h catches 100% of the chains that *start* inside it - the near-real-time
# surface is sound. It reads low above only because a 30-day static dataset has
# most of its chains in the past; a live rail would see each one as it forms.
# 168 h is the default here: roughly a quarter of the network watched for most
# of the recall of watching all of it.
RECENT_HOURS = float(os.environ.get("MULETRACE_RULE_WINDOW_HOURS", 168.0))

# Absolute floor, so a 40-rupee coffee that is 10x someone's norm is not an alert.
MIN_MATERIAL_AMOUNT = 15_000.0

# An account needs some history before "unusual for this account" means anything.
MIN_BASELINE_TXNS = 6

MONITOR_THRESHOLD = 45.0        # score at or above this is monitored

RULE_WEIGHTS = {
    "LARGE_FLOW": 1.0,
    "HIGH_VELOCITY": 0.8,
    "DEBIT_CREDIT_SHIFT": 0.6,
    "NEW_COUNTERPARTIES": 0.7,
    "PASS_THROUGH": 1.2,
    "DORMANT_WAKE": 0.8,
    "THIN_HISTORY": 0.6,
    "MULE_SCORE": 1.0,
}

RULE_LABELS = {
    "LARGE_FLOW": "Unusually large flow for this account",
    "HIGH_VELOCITY": "Transaction count well above this account's norm",
    "DEBIT_CREDIT_SHIFT": "Debit/credit balance departs from this account's norm",
    "NEW_COUNTERPARTIES": "Dealing with counterparties it has never used",
    "PASS_THROUGH": "Funds arriving and leaving fast, mostly intact",
    "DORMANT_WAKE": "Dormant account suddenly transacting",
    "THIN_HISTORY": "Little or no history behind a material amount",
    "MULE_SCORE": "Behavioural classifier scores this account as a probable mule",
}


@dataclass
class Baseline:
    """What normal looks like for one account, from its own past."""
    txn_count: int = 0
    active_days: float = 1.0
    per_day: float = 0.0
    median_amount: float = 0.0
    p95_amount: float = 0.0
    debit_ratio: float = 0.5
    counterparties: set = field(default_factory=set)
    last_seen: datetime | None = None
    established: bool = False


@dataclass
class Trigger:
    rule: str
    score: float                 # 0..1, how strongly it fired
    detail: str

    def as_dict(self) -> dict:
        return {"rule": self.rule, "label": RULE_LABELS[self.rule],
                "score": round(self.score, 3), "detail": self.detail}


@dataclass
class AccountAlert:
    account: str
    score: float
    triggers: list[Trigger]
    recent_in: float
    recent_out: float
    recent_txns: int

    @property
    def monitored(self) -> bool:
        return self.score >= MONITOR_THRESHOLD

    def as_dict(self) -> dict:
        return {
            "account": self.account,
            "score": round(self.score, 1),
            "monitored": self.monitored,
            "rules": [t.rule for t in self.triggers],
            "triggers": [t.as_dict() for t in self.triggers],
            "recent_inflow": round(self.recent_in, 2),
            "recent_outflow": round(self.recent_out, 2),
            "recent_transactions": self.recent_txns,
        }


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, int(round(pct * (len(ordered) - 1))))
    return ordered[idx]


class RuleEngine:
    """Scores accounts against their own history over a recent window."""

    def __init__(self, graph, recent_hours: float = RECENT_HOURS,
                 threshold: float = MONITOR_THRESHOLD) -> None:
        self.graph = graph
        self.recent_hours = recent_hours
        self.threshold = threshold
        self.cutoff = graph.window_end - timedelta(hours=recent_hours)

    # ---- baselines ----

    def _split(self, account: str) -> tuple[list[dict], list[dict]]:
        """This account's edges, as (history, recent)."""
        edges = self.graph.incoming(account) + self.graph.outgoing(account)
        history = [e for e in edges if e["ts"] < self.cutoff]
        recent = [e for e in edges if e["ts"] >= self.cutoff]
        return history, recent

    def baseline_for(self, account: str, history: list[dict]) -> Baseline:
        base = Baseline()
        if not history:
            return base

        amounts = [e["amount"] for e in history]
        stamps = [e["ts"] for e in history]
        span = (max(stamps) - min(stamps)).total_seconds() / 86_400.0

        base.txn_count = len(history)
        base.active_days = max(span, 1.0)
        base.per_day = len(history) / base.active_days
        base.median_amount = statistics.median(amounts)
        base.p95_amount = _percentile(amounts, 0.95)
        debits = sum(1 for e in history if e["sender"] == account)
        base.debit_ratio = debits / len(history)
        base.counterparties = {
            e["receiver"] if e["sender"] == account else e["sender"] for e in history}
        base.last_seen = max(stamps)
        base.established = len(history) >= MIN_BASELINE_TXNS
        return base

    # ---- rules ----

    def _evaluate(self, account: str, base: Baseline, recent: list[dict]) -> list[Trigger]:
        triggers: list[Trigger] = []
        if not recent:
            return triggers

        inflow = [e for e in recent if e["receiver"] == account]
        outflow = [e for e in recent if e["sender"] == account]
        amounts = [e["amount"] for e in recent]
        largest = max(amounts)

        # --- LARGE_FLOW: big for this account, and material in absolute terms
        if largest >= MIN_MATERIAL_AMOUNT:
            if base.established and base.p95_amount > 0:
                ratio = largest / base.p95_amount
                if ratio >= 2.0:
                    triggers.append(Trigger(
                        "LARGE_FLOW", min(1.0, (ratio - 2.0) / 8.0 + 0.35),
                        f"Rs {largest:,.0f} against a 95th-percentile of "
                        f"Rs {base.p95_amount:,.0f} — {ratio:.1f}x"))
            elif largest >= MIN_MATERIAL_AMOUNT * 4:
                # no usable baseline, so fall back to an absolute view
                triggers.append(Trigger(
                    "LARGE_FLOW", 0.5,
                    f"Rs {largest:,.0f} with too little history to compare against"))

        # --- HIGH_VELOCITY: more activity than this account normally has
        expected = base.per_day * (self.recent_hours / 24.0)
        if base.established and expected > 0 and len(recent) >= 4:
            ratio = len(recent) / max(expected, 0.5)
            if ratio >= 3.0:
                triggers.append(Trigger(
                    "HIGH_VELOCITY", min(1.0, (ratio - 3.0) / 9.0 + 0.3),
                    f"{len(recent)} transactions in {self.recent_hours:.0f}h against "
                    f"an expected {expected:.1f} — {ratio:.1f}x"))

        # --- DEBIT_CREDIT_SHIFT: the mix of sending vs receiving has moved
        if base.established and len(recent) >= 3:
            recent_debit_ratio = len(outflow) / len(recent)
            shift = abs(recent_debit_ratio - base.debit_ratio)
            if shift >= 0.4:
                direction = "sending" if recent_debit_ratio > base.debit_ratio else "receiving"
                triggers.append(Trigger(
                    "DEBIT_CREDIT_SHIFT", min(1.0, shift),
                    f"Now {recent_debit_ratio:.0%} debits against a usual "
                    f"{base.debit_ratio:.0%} — shifted toward {direction}"))

        # --- NEW_COUNTERPARTIES: no established relationship
        if base.established:
            seen_now = {e["receiver"] if e["sender"] == account else e["sender"]
                        for e in recent}
            fresh = seen_now - base.counterparties
            if seen_now and len(fresh) / len(seen_now) >= 0.6 and len(fresh) >= 2:
                triggers.append(Trigger(
                    "NEW_COUNTERPARTIES", min(1.0, len(fresh) / max(len(seen_now), 1)),
                    f"{len(fresh)} of {len(seen_now)} counterparties never dealt with before"))

        # --- PASS_THROUGH: the actual mule signature, on this account's own edges
        best = self._pass_through(account, inflow, outflow)
        if best:
            gap, pct, amount = best
            triggers.append(Trigger(
                "PASS_THROUGH", min(1.0, pct * (1.0 - min(gap, 720) / 720) + 0.25),
                f"Rs {amount:,.0f} arrived and {pct:.0%} left again after "
                f"{gap:.0f} min"))

        # --- DORMANT_WAKE: quiet for a long time, then active with real money
        if base.last_seen and largest >= MIN_MATERIAL_AMOUNT:
            quiet_days = (min(e["ts"] for e in recent) - base.last_seen).total_seconds() / 86_400
            if quiet_days >= 10:
                triggers.append(Trigger(
                    "DORMANT_WAKE", min(1.0, quiet_days / 30.0),
                    f"No activity for {quiet_days:.0f} days, then Rs {largest:,.0f}"))

        # --- THIN_HISTORY: young or barely-used account handling material money
        meta = self.graph.account_meta(account)
        age = meta.get("account_age_days")
        if largest >= MIN_MATERIAL_AMOUNT and (
                (age is not None and age < 120) or base.txn_count < 3):
            triggers.append(Trigger(
                "THIN_HISTORY", 0.6 if base.txn_count < 3 else 0.45,
                f"Account age {age} days, {base.txn_count} prior transactions, "
                f"handling Rs {largest:,.0f}"))

        return triggers

    @staticmethod
    def _pass_through(account: str, inflow: list[dict],
                      outflow: list[dict]) -> tuple[float, float, float] | None:
        """Strongest inflow→outflow pairing: fast and mostly forwarded."""
        best = None
        for credit in inflow:
            for debit in outflow:
                if debit["ts"] <= credit["ts"]:
                    continue
                gap = (debit["ts"] - credit["ts"]).total_seconds() / 60.0
                if gap > 48 * 60:
                    continue
                pct = debit["amount"] / credit["amount"] if credit["amount"] else 0.0
                if pct < 0.6 or pct > 1.25:
                    continue
                if best is None or pct * (1 - gap / 2880) > best[1] * (1 - best[0] / 2880):
                    best = (gap, min(pct, 1.0), credit["amount"])
        return best

    # ---- driver ----

    def evaluate(self, mule_scores=None) -> list[AccountAlert]:
        """Score every account that transacted in the recent window.

        Both ends are covered: an account appears here if it was the sender *or*
        the beneficiary, so a mule that only ever receives-then-forwards is
        assessed on the same footing as the payer.
        """
        active: set[str] = set()
        for edge in self.graph.txns_since(self.cutoff):
            active.add(edge["sender"])
            active.add(edge["receiver"])

        alerts: list[AccountAlert] = []
        for account in active:
            history, recent = self._split(account)
            base = self.baseline_for(account, history)
            triggers = self._evaluate(account, base, recent)

            if mule_scores is not None:
                score = mule_scores(account)
                if score is not None and score >= 0.5:
                    triggers.append(Trigger(
                        "MULE_SCORE", float(score),
                        f"Classifier score {score * 100:.0f}/100 on behavioural features"))

            if not triggers:
                continue

            weighted = sum(t.score * RULE_WEIGHTS[t.rule] for t in triggers)
            possible = sum(RULE_WEIGHTS.values())
            # a single strong rule should still be able to reach the threshold,
            # so the scale is deliberately not a plain mean over every rule
            score = min(100.0, 100.0 * (weighted / (possible * 0.45)) ** 0.85)

            alerts.append(AccountAlert(
                account=account,
                score=score,
                triggers=sorted(triggers, key=lambda t: -t.score * RULE_WEIGHTS[t.rule]),
                recent_in=sum(e["amount"] for e in recent if e["receiver"] == account),
                recent_out=sum(e["amount"] for e in recent if e["sender"] == account),
                recent_txns=len(recent),
            ))

        alerts.sort(key=lambda a: -a.score)
        return alerts

    def monitored_accounts(self, mule_scores=None) -> tuple[set[str], list[AccountAlert]]:
        alerts = self.evaluate(mule_scores)
        return {a.account for a in alerts if a.monitored}, alerts

    def summary(self, alerts: list[AccountAlert]) -> dict:
        counts: dict[str, int] = {}
        for alert in alerts:
            for trigger in alert.triggers:
                counts[trigger.rule] = counts.get(trigger.rule, 0) + 1
        monitored = [a for a in alerts if a.monitored]
        return {
            "accounts_on_network": int(len(self.graph.accounts)),
            "accounts_active_in_window": len(alerts),
            "accounts_alerting": len(alerts),
            "accounts_monitored": len(monitored),
            "monitor_threshold": self.threshold,
            "recent_window_hours": self.recent_hours,
            "rule_hits": {rule: counts.get(rule, 0) for rule in RULE_WEIGHTS},
            "rule_labels": RULE_LABELS,
        }
