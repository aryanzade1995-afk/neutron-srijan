"""Per-account feature engineering and the mule-probability classifier.

The chain walk answers "where is the money now". This answers the other half --
"which accounts behave like mules" -- so chains can be surfaced proactively,
before a victim complaint exists. Kept to a small, explainable feature set on
purpose: a bank has to be able to read why an account was flagged.
"""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass

import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (average_precision_score, precision_recall_fscore_support,
                             roc_auc_score)
from sklearn.model_selection import train_test_split
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

import config

# 300 trees is right on a laptop; a 0.1-CPU instance needs far fewer to answer a
# request in reasonable time. Accuracy drops a little, which is the honest trade
# for running at all on a small host.
FOREST_TREES = int(config.get("MULETRACE_FOREST_TREES", "300"))

FEATURES = [
    "in_count", "out_count", "total_in", "total_out", "forward_ratio",
    "median_response_min", "min_response_min", "max_forward_pct", "fast_forward_hops",
    "distinct_senders", "distinct_receivers", "fan_ratio",
    "account_age_days", "kyc_tier", "avg_in_amount", "max_in_amount",
    "active_days", "txns_per_active_day", "retained_ratio",
]

FEATURE_LABELS = {
    "in_count": "Inbound transactions",
    "out_count": "Outbound transactions",
    "total_in": "Total received",
    "total_out": "Total sent",
    "forward_ratio": "Share of inflow forwarded",
    "median_response_min": "Median minutes before money leaves",
    "min_response_min": "Fastest turnaround (min)",
    "max_forward_pct": "Largest single forward %",
    "fast_forward_hops": "Fast high-value forwards",
    "distinct_senders": "Distinct payers",
    "distinct_receivers": "Distinct payees",
    "fan_ratio": "Fan-in vs fan-out",
    "account_age_days": "Account age (days)",
    "kyc_tier": "KYC tier",
    "avg_in_amount": "Average inbound amount",
    "max_in_amount": "Largest inbound amount",
    "active_days": "Days active",
    "txns_per_active_day": "Transactions per active day",
    "retained_ratio": "Share of inflow retained",
}

MAX_GAP_MINUTES = 48 * 60
FORWARD_THRESHOLD = 0.70


def build_features(graph) -> pd.DataFrame:
    """One row per account, computed independently - the pass is parallelisable."""
    rows = []
    for account in graph.graph.nodes:
        inbound = graph.incoming(account)
        outbound = graph.outgoing(account)
        meta = graph.account_meta(account)

        total_in = sum(e["amount"] for e in inbound)
        total_out = sum(e["amount"] for e in outbound)
        out_times = [e["ts"] for e in outbound]

        responses: list[float] = []
        forward_pcts: list[float] = []
        fast_hops = 0
        for e in inbound:
            idx = bisect_right(out_times, e["ts"])
            if idx >= len(outbound):
                continue
            nxt = outbound[idx]
            gap = (nxt["ts"] - e["ts"]).total_seconds() / 60.0
            if gap > MAX_GAP_MINUTES:
                continue
            responses.append(gap)
            pct = nxt["amount"] / e["amount"] if e["amount"] else 0.0
            forward_pcts.append(min(pct, 1.5))
            if pct >= FORWARD_THRESHOLD:
                fast_hops += 1

        all_ts = [e["ts"] for e in inbound + outbound]
        active_days = max(1.0, (max(all_ts) - min(all_ts)).total_seconds() / 86400.0) if all_ts else 1.0
        senders = {e["sender"] for e in inbound}
        receivers = {e["receiver"] for e in outbound}

        rows.append({
            "account_id": account,
            "in_count": len(inbound),
            "out_count": len(outbound),
            "total_in": total_in,
            "total_out": total_out,
            "forward_ratio": (total_out / total_in) if total_in else 0.0,
            "median_response_min": float(np.median(responses)) if responses else MAX_GAP_MINUTES,
            "min_response_min": float(min(responses)) if responses else MAX_GAP_MINUTES,
            "max_forward_pct": float(max(forward_pcts)) if forward_pcts else 0.0,
            "fast_forward_hops": fast_hops,
            "distinct_senders": len(senders),
            "distinct_receivers": len(receivers),
            "fan_ratio": len(receivers) / max(len(senders), 1),
            "account_age_days": float(meta.get("account_age_days") or 0),
            "kyc_tier": float(meta.get("kyc_tier") or 0),
            "avg_in_amount": (total_in / len(inbound)) if inbound else 0.0,
            "max_in_amount": max((e["amount"] for e in inbound), default=0.0),
            "active_days": active_days,
            "txns_per_active_day": (len(inbound) + len(outbound)) / active_days,
            "retained_ratio": max(0.0, (total_in - total_out) / total_in) if total_in else 0.0,
            "account_type": meta.get("account_type", "unknown"),
            "bank": meta.get("bank", ""),
            "is_mule": int(meta.get("is_mule") or 0),
        })
    return pd.DataFrame(rows).set_index("account_id")


@dataclass
class ModelReport:
    algorithm: str
    n_accounts: int
    n_mules: int
    precision: float
    recall: float
    f1: float
    roc_auc: float
    avg_precision: float
    threshold: float
    top_features: list[dict]

    def as_dict(self) -> dict:
        return self.__dict__


class RiskModel:
    """Random Forest scorer with a Logistic Regression baseline for comparison."""

    def __init__(self, threshold: float = 0.5) -> None:
        self.threshold = threshold
        self.model: Pipeline | None = None
        self.baseline: Pipeline | None = None
        self.scores: pd.Series | None = None
        self.report: ModelReport | None = None
        self.baseline_report: dict | None = None

    def fit(self, features: pd.DataFrame, seed: int = 26) -> ModelReport:
        X = features[FEATURES].astype(float)
        y = features["is_mule"].astype(int)

        X_train, X_test, y_train, y_test = train_test_split(
            X, y, test_size=0.3, random_state=seed, stratify=y)

        forest = Pipeline([("scale", StandardScaler()),
                           ("clf", RandomForestClassifier(
                               n_estimators=FOREST_TREES, min_samples_leaf=2,
                               class_weight="balanced_subsample", random_state=seed, n_jobs=-1))])
        logistic = Pipeline([("scale", StandardScaler()),
                             ("clf", LogisticRegression(max_iter=2000, class_weight="balanced"))])

        forest.fit(X_train, y_train)
        logistic.fit(X_train, y_train)
        self.model, self.baseline = forest, logistic

        probs = forest.predict_proba(X_test)[:, 1]
        preds = (probs >= self.threshold).astype(int)
        precision, recall, f1, _ = precision_recall_fscore_support(
            y_test, preds, average="binary", zero_division=0)

        importances = forest.named_steps["clf"].feature_importances_
        top = sorted(zip(FEATURES, importances), key=lambda kv: -kv[1])[:8]

        self.report = ModelReport(
            algorithm=f"RandomForestClassifier({FOREST_TREES})",
            n_accounts=int(len(features)),
            n_mules=int(y.sum()),
            precision=round(float(precision), 4),
            recall=round(float(recall), 4),
            f1=round(float(f1), 4),
            roc_auc=round(float(roc_auc_score(y_test, probs)), 4),
            avg_precision=round(float(average_precision_score(y_test, probs)), 4),
            threshold=self.threshold,
            top_features=[{"feature": f, "label": FEATURE_LABELS[f], "importance": round(float(i), 4)}
                          for f, i in top],
        )

        base_probs = logistic.predict_proba(X_test)[:, 1]
        base_preds = (base_probs >= self.threshold).astype(int)
        bp, br, bf, _ = precision_recall_fscore_support(
            y_test, base_preds, average="binary", zero_division=0)
        self.baseline_report = {
            "algorithm": "LogisticRegression",
            "precision": round(float(bp), 4), "recall": round(float(br), 4),
            "f1": round(float(bf), 4),
            "roc_auc": round(float(roc_auc_score(y_test, base_probs)), 4),
        }

        self.scores = pd.Series(forest.predict_proba(X)[:, 1], index=features.index)
        self.features = features
        return self.report

    def score(self, account_id: str) -> float | None:
        if self.scores is None or account_id not in self.scores.index:
            return None
        return float(self.scores.loc[account_id])

    def explain(self, account_id: str) -> dict:
        """Why this account scored the way it did, in investigator-readable terms."""
        if self.scores is None or account_id not in self.features.index:
            return {"account_id": account_id, "score": None, "signals": []}
        row = self.features.loc[account_id]
        population = self.features[FEATURES].astype(float)

        signals = []
        for feature, _ in [(f["feature"], f) for f in self.report.top_features]:
            value = float(row[feature])
            pct = float((population[feature] <= value).mean())
            signals.append({
                "feature": feature,
                "label": FEATURE_LABELS[feature],
                "value": round(value, 2),
                "percentile": round(pct * 100, 1),
            })
        return {
            "account_id": account_id,
            "score": round(float(self.scores.loc[account_id]), 4),
            "signals": signals,
        }

    def ranked(self, limit: int = 25, min_score: float = 0.5) -> list[dict]:
        if self.scores is None:
            return []
        ranked = self.scores[self.scores >= min_score].sort_values(ascending=False)[:limit]
        out = []
        for account_id, score in ranked.items():
            row = self.features.loc[account_id]
            out.append({
                "account_id": account_id,
                "score": round(float(score), 4),
                "bank": row["bank"],
                "account_age_days": int(row["account_age_days"]),
                "forward_ratio": round(float(row["forward_ratio"]), 3),
                "median_response_min": round(float(row["median_response_min"]), 1),
                "total_in": round(float(row["total_in"]), 2),
                "is_mule": int(row["is_mule"]),
            })
        return out
