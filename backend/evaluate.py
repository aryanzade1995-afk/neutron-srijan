"""Validate the chain walk and the classifier against the injected ground truth.

Every number the pitch quotes comes out of this script, so it can be re-run live
if a judge asks where the figures came from.
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from graph_engine import DATA_DIR, WalkParams, load_graph
from risk_model import RiskModel, build_features


def evaluate_chain_walk(graph, truth: pd.DataFrame, params: WalkParams) -> dict:
    exact_end_node = 0
    full_path = 0
    hop_recall = []

    for row in truth.itertuples(index=False):
        trace = graph.chain_walk(row.entry_txn_id, params)
        if trace["end_node"] == row.end_node:
            exact_end_node += 1
        truth_nodes = [row.victim] + row.mules.split("|")
        if row.cashed_out:
            truth_nodes.append(row.end_node)
        found = set(trace["path"])
        hop_recall.append(len(found & set(truth_nodes)) / len(truth_nodes))
        if set(truth_nodes) <= found:
            full_path += 1

    n = len(truth)
    return {
        "chains": n,
        "end_node_accuracy": round(exact_end_node / n, 4),
        "full_path_recovery": round(full_path / n, 4),
        "mean_hop_recall": round(sum(hop_recall) / n, 4),
    }


def evaluate_sensitivity(graph, truth: pd.DataFrame) -> list[dict]:
    """How end-node accuracy moves as the walk is tightened or loosened.

    The headline accuracy is measured on chains the generator built to the same
    temporal logic the walk looks for, so on its own it is close to circular.
    Sweeping the thresholds shows where the traversal actually degrades - which
    is the number worth quoting when someone asks how brittle the rule is.
    """
    grid = [
        ("strict   (85% fwd, 6h)", WalkParams(min_forward_pct=0.85, max_gap_hours=6)),
        ("tight    (80% fwd, 24h)", WalkParams(min_forward_pct=0.80, max_gap_hours=24)),
        ("default  (70% fwd, 48h)", WalkParams()),
        ("loose    (60% fwd, 96h)", WalkParams(min_forward_pct=0.60, max_gap_hours=96)),
        ("very loose (40% fwd, 168h)", WalkParams(min_forward_pct=0.40, max_gap_hours=168)),
    ]
    rows = []
    for label, params in grid:
        hits = sum(1 for row in truth.itertuples(index=False)
                   if graph.chain_walk(row.entry_txn_id, params)["end_node"] == row.end_node)
        rows.append({"setting": label, "end_node_accuracy": round(hits / len(truth), 4)})
    return rows


def evaluate_scan(graph, truth: pd.DataFrame, params: WalkParams, model: RiskModel) -> dict:
    chains = graph.scan(params, min_hops=3, limit=200, risk_lookup=model.score)
    truth_ends = set(truth["end_node"])
    truth_by_end = {row.end_node: row.chain_id for row in truth.itertuples(index=False)}

    hits = {truth_by_end[c["end_node"]] for c in chains if c["end_node"] in truth_ends}
    tp = sum(1 for c in chains if c["end_node"] in truth_ends)

    # An investigator works the queue top-down, so precision at the depths they
    # actually reach matters more than precision over the whole surfaced set.
    def precision_at(k: int) -> float:
        head = chains[:k]
        if not head:
            return 0.0
        return round(sum(1 for c in head if c["end_node"] in truth_ends) / len(head), 4)

    # how far down the ranked queue an investigator gets before the first false
    # alarm - the most direct read on whether the ordering is trustworthy
    clean_prefix = 0
    for chain in chains:
        if chain["end_node"] not in truth_ends:
            break
        clean_prefix += 1

    return {
        "chains_surfaced": len(chains),
        "true_chains_found": len(hits),
        "recall_vs_injected": round(len(hits) / len(truth), 4),
        "precision": round(tp / len(chains), 4) if chains else 0.0,
        "precision_at_10": precision_at(10),
        "precision_at_20": precision_at(20),
        "precision_at_30": precision_at(30),
        "clean_prefix": clean_prefix,
    }


def compute(graph, truth: pd.DataFrame, model: RiskModel, report) -> dict:
    """Full evaluation over an already-built graph and fitted model.

    Kept separate from main() so the API can recompute these numbers in-process
    after the dataset changes, rather than serving figures from a stale file.
    """
    params = WalkParams()
    return {
        "dataset": {
            "transactions": int(len(graph.txns)),
            "accounts": int(len(graph.accounts)),
            "injected_chains": int(len(truth)),
            "cashed_out_chains": int(truth["cashed_out"].sum()) if len(truth) else 0,
        },
        "chain_walk": evaluate_chain_walk(graph, truth, params),
        "sensitivity": evaluate_sensitivity(graph, truth),
        "proactive_scan": evaluate_scan(graph, truth, params, model),
        "risk_model": report.as_dict(),
        "baseline": model.baseline_report,
    }


def main() -> None:
    graph = load_graph()
    truth = pd.read_csv(DATA_DIR / "ground_truth.csv")

    features = build_features(graph)
    model = RiskModel()
    report = model.fit(features)

    results = compute(graph, truth, model, report)

    out = DATA_DIR / "evaluation.json"
    out.write_text(json.dumps(results, indent=2), encoding="utf-8")

    print("=== Dataset ===")
    for k, v in results["dataset"].items():
        print(f"  {k:24} {v:,}")
    print("\n=== Chain walk (complaint-triggered) ===")
    for k, v in results["chain_walk"].items():
        print(f"  {k:24} {v}")
    print("\n=== Walk sensitivity (end-node accuracy) ===")
    for row in results["sensitivity"]:
        print(f"  {row['setting']:28} {row['end_node_accuracy']}")
    print("\n=== Proactive scan (no complaint) ===")
    for k, v in results["proactive_scan"].items():
        print(f"  {k:24} {v}")
    print("\n=== Mule classifier ===")
    for k in ("algorithm", "precision", "recall", "f1", "roc_auc", "avg_precision"):
        print(f"  {k:24} {report.as_dict()[k]}")
    print(f"  baseline (logreg)        {model.baseline_report}")
    print("\n  top features:")
    for f in report.top_features[:6]:
        print(f"    {f['importance']:.3f}  {f['label']}")
    print(f"\nwritten to {out}")


if __name__ == "__main__":
    main()
