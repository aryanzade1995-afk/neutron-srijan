"""MuleTrace API - chain tracing, mule scoring, and the investigator dashboard."""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

import pandas as pd
from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from feedback import FeedbackStore, Verdict
from graph_engine import DATA_DIR, WalkParams, load_graph
from risk_model import RiskModel, build_features

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "frontend"

STATE: dict = {}


def bootstrap() -> None:
    """Build the graph, score every account, and pre-run the proactive scan."""
    graph = load_graph()
    features = build_features(graph)
    model = RiskModel()
    report = model.fit(features)

    truth_path = DATA_DIR / "ground_truth.csv"
    truth = pd.read_csv(truth_path) if truth_path.exists() else pd.DataFrame()

    STATE["graph"] = graph
    STATE["model"] = model
    STATE["features"] = features
    STATE["report"] = report
    STATE["truth"] = truth
    STATE["feedback"] = FeedbackStore(DATA_DIR / "feedback.jsonl")
    rescan()


def rescan() -> list[dict]:
    """Re-run the scan and cache the decorated result.

    Decorating attaches per-node metadata and scores; doing it once here keeps
    /api/chains from repeating that work on every request.
    """
    graph, model = STATE["graph"], STATE["model"]
    chains = graph.scan(WalkParams(), min_hops=3, limit=60, risk_lookup=model.score)
    STATE["chains"] = [_decorate(c) for c in chains]
    return STATE["chains"]


@asynccontextmanager
async def lifespan(app: FastAPI):
    bootstrap()
    yield
    STATE.clear()


app = FastAPI(title="MuleTrace", version="0.2.0",
              description="Mule-account transaction chain detection for UPI/NPCI",
              lifespan=lifespan)


def _params(min_forward_pct: float, max_gap_hours: float, max_hops: int) -> WalkParams:
    return WalkParams(min_forward_pct=min_forward_pct, max_gap_hours=max_gap_hours,
                      max_hops=max_hops)


def _node_payload(graph, model, account: str, role: str) -> dict:
    meta = graph.account_meta(account)
    score = model.score(account)
    return {
        "account_id": account,
        "role": role,
        "bank": meta.get("bank", ""),
        "account_type": meta.get("account_type", "unknown"),
        "account_age_days": meta.get("account_age_days"),
        "kyc_tier": meta.get("kyc_tier"),
        "mule_score": round(score, 4) if score is not None else None,
        "known_mule": int(meta.get("is_mule") or 0),
    }


def _decorate(trace: dict) -> dict:
    graph, model = STATE["graph"], STATE["model"]
    path = trace["path"]
    nodes = []
    for i, account in enumerate(path):
        if i == 0:
            role = "victim"
        elif account == trace["end_node"]:
            role = "cashout" if trace["end_reason"] == "cash_out" else "end_node"
        else:
            role = "mule"
        nodes.append(_node_payload(graph, model, account, role))
    trace = dict(trace)
    trace["nodes"] = nodes
    trace.setdefault("risk", graph.score_chain(trace, model.score))
    trace["freeze_recommended"] = trace["recoverable"] and trace["risk"]["score"] >= 35

    store: FeedbackStore | None = STATE.get("feedback")
    recorded = store.for_chain(trace["entry_txn_id"]) if store else None
    trace["review"] = recorded.as_dict() if recorded else None
    return trace


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "transactions": int(len(STATE["graph"].txns))}


@app.get("/api/overview")
def overview() -> dict:
    graph, model, chains = STATE["graph"], STATE["model"], STATE["chains"]
    live = [c for c in chains if c["recoverable"]]
    at_risk = sum(c["amount_at_end"] for c in live)
    gaps = sorted(c["elapsed_minutes"] for c in chains) or [0]
    flagged = int((model.scores >= 0.5).sum()) if model.scores is not None else 0

    return {
        "transactions": int(len(graph.txns)),
        "accounts": int(len(graph.accounts)),
        "window": {"from": graph.window_start.isoformat(), "to": graph.window_end.isoformat()},
        "active_chains": len(chains),
        "freezable_chains": len(live),
        "cashed_out_chains": len(chains) - len(live),
        "funds_recoverable": round(at_risk, 2),
        "funds_lost": round(sum(c["amount_at_end"] for c in chains if not c["recoverable"]), 2),
        "median_chain_minutes": gaps[len(gaps) // 2],
        "accounts_flagged": flagged,
        "model": STATE["report"].as_dict(),
        "baseline": model.baseline_report,
    }


@app.get("/api/chains")
def chains(limit: int = 25, band: str | None = None,
           include_dismissed: bool = False) -> dict:
    items = STATE["chains"]
    if band:
        items = [c for c in items if c["risk"]["band"] == band]
    if not include_dismissed:
        # a chain an investigator has already dismissed should not keep returning
        # to the top of their queue
        items = [c for c in items
                 if not (c.get("review") or {}).get("verdict") == "false_positive"]
    return {"count": len(items), "chains": items[:limit]}


@app.post("/api/rescan")
def trigger_rescan() -> dict:
    chains = rescan()
    return {"chains": len(chains)}


@app.get("/api/trace/{txn_id}")
def trace(txn_id: str,
          min_forward_pct: float = Query(0.70, ge=0.1, le=1.0),
          max_gap_hours: float = Query(48.0, gt=0, le=720),
          max_hops: int = Query(10, ge=1, le=25)) -> dict:
    graph = STATE["graph"]
    try:
        walk = graph.chain_walk(txn_id, _params(min_forward_pct, max_gap_hours, max_hops))
    except KeyError:
        raise HTTPException(404, f"transaction {txn_id} not found")
    walk["risk"] = graph.score_chain(walk, STATE["model"].score)
    return _decorate(walk)


@app.get("/api/risk-score/{account_id}")
def risk_score(account_id: str) -> dict:
    graph, model = STATE["graph"], STATE["model"]
    if account_id not in STATE["features"].index:
        raise HTTPException(404, f"account {account_id} not found")
    explain = model.explain(account_id)
    meta = graph.account_meta(account_id)
    inbound, outbound = graph.incoming(account_id), graph.outgoing(account_id)
    return {
        **explain,
        "bank": meta.get("bank"),
        "account_type": meta.get("account_type"),
        "account_age_days": meta.get("account_age_days"),
        "kyc_tier": meta.get("kyc_tier"),
        "known_mule": int(meta.get("is_mule") or 0),
        "in_count": len(inbound),
        "out_count": len(outbound),
        "total_in": round(sum(e["amount"] for e in inbound), 2),
        "total_out": round(sum(e["amount"] for e in outbound), 2),
        "recent": [
            {"txn_id": e["txn_id"], "direction": "in" if e["receiver"] == account_id else "out",
             "counterparty": e["sender"] if e["receiver"] == account_id else e["receiver"],
             "amount": e["amount"], "timestamp": e["timestamp"], "mode": e["mode"]}
            for e in sorted(inbound + outbound, key=lambda e: e["ts"], reverse=True)[:12]
        ],
    }


@app.get("/api/watchlist")
def watchlist(limit: int = 20, min_score: float = 0.5) -> dict:
    return {"accounts": STATE["model"].ranked(limit=limit, min_score=min_score)}


@app.get("/api/complaints")
def complaints(limit: int = 12) -> dict:
    """Entry points an investigator would realistically be handed."""
    truth = STATE["truth"]
    if truth.empty:
        return {"complaints": []}
    graph = STATE["graph"]
    rows = []
    for row in truth.head(limit).itertuples(index=False):
        edge = graph.txn(row.entry_txn_id)
        rows.append({
            "txn_id": row.entry_txn_id,
            "victim": row.victim,
            "amount": edge["amount"] if edge else row.amount,
            "timestamp": edge["timestamp"] if edge else None,
            "chain_id": row.chain_id,
        })
    return {"complaints": rows}


@app.get("/api/search")
def search(q: str, limit: int = 10) -> dict:
    q = q.strip().lower()
    graph = STATE["graph"]
    if not q:
        return {"transactions": [], "accounts": []}
    txns = [{"txn_id": t["txn_id"], "sender": t["sender"], "receiver": t["receiver"],
             "amount": t["amount"], "timestamp": t["timestamp"]}
            for t in graph.find_transactions(q, limit)]
    accounts = [a for a in graph.accounts.index if q in a.lower()][:limit]
    return {"transactions": txns, "accounts": accounts}


# ---------- investigator feedback ----------

@app.get("/api/feedback")
def list_feedback() -> dict:
    store: FeedbackStore = STATE["feedback"]
    return {**store.summary(), "reviews": store.all()}


@app.post("/api/feedback", status_code=201)
def record_feedback(
    entry_txn_id: str = Body(..., embed=True),
    verdict: str = Body(..., embed=True),
    note: str = Body("", embed=True),
    reviewer: str = Body("unattributed", embed=True),
) -> dict:
    """Log a closed case. Confirmed chains become labels for the next retrain."""
    graph, store = STATE["graph"], STATE["feedback"]
    if graph.txn(entry_txn_id) is None:
        raise HTTPException(404, f"transaction {entry_txn_id} not found")

    walk = graph.chain_walk(entry_txn_id, WalkParams())
    try:
        recorded = store.record(Verdict(
            entry_txn_id=entry_txn_id,
            end_node=walk["end_node"],
            verdict=verdict,
            note=note,
            reviewer=reviewer,
        ))
    except ValueError as exc:
        raise HTTPException(422, str(exc))

    # reflect the verdict on the cached queue without a full rescan
    for chain in STATE["chains"]:
        if chain["entry_txn_id"] == entry_txn_id:
            chain["review"] = recorded.as_dict()

    return {"recorded": recorded.as_dict(), **store.summary()}


@app.get("/api/feedback/labels")
def feedback_labels() -> dict:
    """Accumulated supervision available to a retrain."""
    store: FeedbackStore = STATE["feedback"]
    labels = store.training_labels()
    return {
        "labelled_accounts": len(labels),
        "positives": sum(1 for v in labels.values() if v == 1),
        "negatives": sum(1 for v in labels.values() if v == 0),
        "labels": labels,
    }


if FRONTEND.exists():
    app.mount("/assets", StaticFiles(directory=FRONTEND), name="assets")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(FRONTEND / "index.html")

    @app.get("/console")
    def console() -> FileResponse:
        target = FRONTEND / "console.html"
        if not target.exists():
            raise HTTPException(404, "console not built yet")
        return FileResponse(target)
