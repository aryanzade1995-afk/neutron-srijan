"""MuleTrace API - chain tracing, mule scoring, and the investigator dashboard.

Each browser session works on its own synthetic network. The session cookie
carries a seed, the seed builds a workspace, and every figure the client sees is
derived from that workspace - so two people on the same demo are looking at
genuinely different data, and neither is looking at anything hardcoded.
"""
from __future__ import annotations

import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

import evaluate
from feedback import Verdict
from graph_engine import WalkParams
from workspace import Workspace, WorkspacePool

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "frontend"

SESSION_COOKIE = "mt_session"
POOL = WorkspacePool()


# ---------- workspace plumbing ----------

def _seed_from_token(token: str) -> int:
    """Stable seed for a session token."""
    return int.from_bytes(token.encode("utf-8")[:8].ljust(8, b"\0"), "big") % (2 ** 31)


def resolve_workspace(request: Request, response: Response) -> Workspace:
    """The caller's dataset, built on first contact.

    `?seed=` pins a specific dataset - useful when a demo needs to reproduce the
    exact numbers on a slide, or when two people want to compare the same case.
    """
    pinned = request.query_params.get("seed")
    if pinned is not None:
        try:
            seed = int(pinned) % (2 ** 31)
        except ValueError:
            raise HTTPException(422, "seed must be an integer")
        return POOL.get(seed, _decorate)

    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        token = secrets.token_urlsafe(12)
        response.set_cookie(SESSION_COOKIE, token, max_age=60 * 60 * 24 * 7,
                            httponly=True, samesite="lax")
    return POOL.get(_seed_from_token(token), _decorate)


def _new_session(response: Response) -> str:
    token = secrets.token_urlsafe(12)
    response.set_cookie(SESSION_COOKIE, token, max_age=60 * 60 * 24 * 7,
                        httponly=True, samesite="lax")
    return token


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    POOL._items.clear()


app = FastAPI(title="MuleTrace", version="0.3.0",
              description="Mule-account transaction chain detection for UPI/NPCI",
              lifespan=lifespan)


def _params(min_forward_pct: float, max_gap_hours: float, max_hops: int) -> WalkParams:
    return WalkParams(min_forward_pct=min_forward_pct, max_gap_hours=max_gap_hours,
                      max_hops=max_hops)


def _node_payload(ws: Workspace, account: str, role: str) -> dict:
    meta = ws.graph.account_meta(account)
    score = ws.model.score(account)
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


def _decorate(ws: Workspace, trace: dict) -> dict:
    nodes = []
    for i, account in enumerate(trace["path"]):
        if i == 0:
            role = "victim"
        elif account == trace["end_node"]:
            role = "cashout" if trace["end_reason"] == "cash_out" else "end_node"
        else:
            role = "mule"
        nodes.append(_node_payload(ws, account, role))

    trace = dict(trace)
    trace["nodes"] = nodes
    trace.setdefault("risk", ws.graph.score_chain(trace, ws.model.score))
    trace["freeze_recommended"] = trace["recoverable"] and trace["risk"]["score"] >= 35

    recorded = ws.feedback.for_chain(trace["entry_txn_id"]) if ws.feedback else None
    trace["review"] = recorded.as_dict() if recorded else None
    return trace


# ---------- endpoints ----------

@app.get("/api/health")
def health(request: Request, response: Response) -> dict:
    ws = resolve_workspace(request, response)
    return {
        "status": "ok",
        "dataset": ws.label,
        "seed": ws.seed,
        "transactions": int(len(ws.graph.txns)),
        "accounts": int(len(ws.graph.accounts)),
        "chains": len(ws.chains),
        "pool": POOL.stats(),
    }


@app.get("/api/overview")
def overview(request: Request, response: Response) -> dict:
    ws = resolve_workspace(request, response)
    chains = ws.visible_chains()                    # same queue the table renders
    live = [c for c in chains if c["recoverable"]]
    gaps = sorted(c["elapsed_minutes"] for c in chains) or [0]
    flagged = int((ws.model.scores >= 0.5).sum()) if ws.model.scores is not None else 0

    return {
        "dataset": ws.label,
        "seed": ws.seed,
        "transactions": int(len(ws.graph.txns)),
        "accounts": int(len(ws.graph.accounts)),
        "window": {"from": ws.graph.window_start.isoformat(),
                   "to": ws.graph.window_end.isoformat()},
        "active_chains": len(chains),
        "freezable_chains": len(live),
        "cashed_out_chains": len(chains) - len(live),
        "dismissed_chains": len(ws.chains) - len(chains),
        "funds_recoverable": round(sum(c["amount_at_end"] for c in live), 2),
        "funds_lost": round(sum(c["amount_at_end"] for c in chains
                                if not c["recoverable"]), 2),
        "median_chain_minutes": gaps[len(gaps) // 2],
        "accounts_flagged": flagged,
        "mule_accounts_known": int(ws.features["is_mule"].sum()),
        "injected_chains": int(len(ws.truth)),
        "review": ws.feedback.summary(),
        "model": ws.report.as_dict(),
        "baseline": ws.model.baseline_report,
    }


@app.get("/api/chains")
def chains(request: Request, response: Response, limit: int = 25,
           band: str | None = None, include_dismissed: bool = False) -> dict:
    ws = resolve_workspace(request, response)
    items = ws.visible_chains(band, include_dismissed)
    return {"count": len(items), "chains": items[:limit], "dataset": ws.label}


@app.post("/api/rescan")
def trigger_rescan(request: Request, response: Response) -> dict:
    ws = resolve_workspace(request, response)
    return {"chains": len(ws.rescan(_decorate)), "dataset": ws.label, "seed": ws.seed}


@app.post("/api/reload")
def new_dataset(response: Response) -> dict:
    """Hand the caller a brand new synthetic network.

    Issues a fresh session token so every subsequent request lands on a
    different seed, and every figure on screen changes with it.
    """
    token = _new_session(response)
    ws = POOL.get(_seed_from_token(token), _decorate)
    return {
        "reloaded": True,
        "dataset": ws.label,
        "seed": ws.seed,
        "chains": len(ws.chains),
        "transactions": int(len(ws.graph.txns)),
        "accounts": int(len(ws.graph.accounts)),
    }


@app.get("/api/evaluation")
def evaluation(request: Request, response: Response) -> dict:
    """Validation figures for the caller's dataset, computed on demand."""
    ws = resolve_workspace(request, response)
    if ws.evaluation is None:
        if ws.truth.empty:
            raise HTTPException(404, "no ground truth for this dataset")
        ws.evaluation = evaluate.compute(ws.graph, ws.truth, ws.model, ws.report)
    return ws.evaluation


@app.get("/api/trace/{txn_id}")
def trace(txn_id: str, request: Request, response: Response,
          min_forward_pct: float = Query(0.70, ge=0.1, le=1.0),
          max_gap_hours: float = Query(48.0, gt=0, le=720),
          max_hops: int = Query(10, ge=1, le=25)) -> dict:
    ws = resolve_workspace(request, response)
    try:
        walk = ws.graph.chain_walk(txn_id, _params(min_forward_pct, max_gap_hours, max_hops))
    except KeyError:
        raise HTTPException(404, f"transaction {txn_id} not found")
    walk["risk"] = ws.graph.score_chain(walk, ws.model.score)
    return _decorate(ws, walk)


@app.get("/api/risk-score/{account_id}")
def risk_score(account_id: str, request: Request, response: Response) -> dict:
    ws = resolve_workspace(request, response)
    if account_id not in ws.features.index:
        raise HTTPException(404, f"account {account_id} not found")
    explain = ws.model.explain(account_id)
    meta = ws.graph.account_meta(account_id)
    inbound, outbound = ws.graph.incoming(account_id), ws.graph.outgoing(account_id)
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
def watchlist(request: Request, response: Response,
              limit: int = 20, min_score: float = 0.5) -> dict:
    ws = resolve_workspace(request, response)
    return {"accounts": ws.model.ranked(limit=limit, min_score=min_score)}


@app.get("/api/complaints")
def complaints(request: Request, response: Response, limit: int = 12) -> dict:
    """Entry points an investigator would realistically be handed."""
    ws = resolve_workspace(request, response)
    if ws.truth.empty:
        return {"complaints": []}
    rows = []
    for row in ws.truth.head(limit).itertuples(index=False):
        edge = ws.graph.txn(row.entry_txn_id)
        rows.append({
            "txn_id": row.entry_txn_id,
            "victim": row.victim,
            "amount": edge["amount"] if edge else row.amount,
            "timestamp": edge["timestamp"] if edge else None,
            "chain_id": row.chain_id,
        })
    return {"complaints": rows}


@app.get("/api/search")
def search(q: str, request: Request, response: Response, limit: int = 10) -> dict:
    ws = resolve_workspace(request, response)
    q = q.strip().lower()
    if not q:
        return {"transactions": [], "accounts": []}
    txns = [{"txn_id": t["txn_id"], "sender": t["sender"], "receiver": t["receiver"],
             "amount": t["amount"], "timestamp": t["timestamp"]}
            for t in ws.graph.find_transactions(q, limit)]
    accounts = [a for a in ws.graph.accounts.index if q in a.lower()][:limit]
    return {"transactions": txns, "accounts": accounts}


# ---------- investigator feedback ----------

@app.get("/api/feedback")
def list_feedback(request: Request, response: Response) -> dict:
    ws = resolve_workspace(request, response)
    return {**ws.feedback.summary(), "reviews": ws.feedback.all()}


@app.post("/api/feedback", status_code=201)
def record_feedback(
    request: Request,
    response: Response,
    entry_txn_id: str = Body(..., embed=True),
    verdict: str = Body(..., embed=True),
    note: str = Body("", embed=True),
    reviewer: str = Body("unattributed", embed=True),
) -> dict:
    """Log a closed case. Confirmed chains become labels for the next retrain."""
    ws = resolve_workspace(request, response)
    if ws.graph.txn(entry_txn_id) is None:
        raise HTTPException(404, f"transaction {entry_txn_id} not found")

    walk = ws.graph.chain_walk(entry_txn_id, WalkParams())
    try:
        recorded = ws.feedback.record(Verdict(
            entry_txn_id=entry_txn_id,
            end_node=walk["end_node"],
            verdict=verdict,
            note=note,
            reviewer=reviewer,
        ))
    except ValueError as exc:
        raise HTTPException(422, str(exc))

    for chain in ws.chains:
        if chain["entry_txn_id"] == entry_txn_id:
            chain["review"] = recorded.as_dict()

    return {"recorded": recorded.as_dict(), **ws.feedback.summary()}


@app.get("/api/feedback/labels")
def feedback_labels(request: Request, response: Response) -> dict:
    """Accumulated supervision available to a retrain."""
    ws = resolve_workspace(request, response)
    labels = ws.feedback.training_labels()
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
