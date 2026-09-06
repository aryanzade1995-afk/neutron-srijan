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
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

import auth
import evaluate
from feedback import Verdict
from graph_engine import WalkParams
from db import DB
from store import STORE
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
    seeded = auth.ensure_demo_user() if auth.AUTH_ENABLED else None
    if not auth.AUTH_ENABLED:
        print("[auth] Authentication is OFF - every route is open. "
              "Set MULETRACE_AUTH=on to require password + TOTP.")
    if seeded:
        username, password, secret = seeded
        print("\n" + "=" * 72)
        print("  MuleTrace — an investigator account was created on first boot")
        print(f"  username : {username}")
        print(f"  password : {password}")
        print(f"  TOTP key : {secret}")
        print("  Enrol this key in an authenticator app, or open /login and scan the QR.")
        print("  These credentials are printed once. Set MULETRACE_AUTH=off to disable")
        print("  authentication entirely for a rehearsal.")
        print("=" * 72 + "\n")
    yield
    POOL._items.clear()


app = FastAPI(title="MuleTrace", version="0.4.0",
              description="Mule-account transaction chain detection for UPI/NPCI",
              lifespan=lifespan)


# ---------- authentication gate ----------

OPEN_PATHS = ("/login", "/assets", "/api/auth", "/api/health", "/favicon.ico",
              "/docs", "/openapi.json", "/redoc")


@app.middleware("http")
async def require_authentication(request: Request, call_next):
    """One gate in front of everything that exposes case data.

    The console shows victim VPAs, account ages and freeze recommendations, so
    the API is closed by default rather than relying on each route to remember.
    """
    path = request.url.path
    if not auth.AUTH_ENABLED or path.startswith(OPEN_PATHS):
        return await call_next(request)

    user = auth.session_user(request.cookies.get(auth.SESSION_COOKIE))
    if user:
        request.state.user = user
        return await call_next(request)

    if path.startswith("/api/"):
        return JSONResponse({"detail": "authentication required"}, status_code=401)
    return RedirectResponse(f"/login?next={path}", status_code=302)


# ---------- auth endpoints ----------

@app.get("/api/auth/status")
def auth_status(request: Request) -> dict:
    user = auth.session_user(request.cookies.get(auth.SESSION_COOKIE))
    return {
        "auth_enabled": auth.AUTH_ENABLED,
        "authenticated": bool(user) or not auth.AUTH_ENABLED,
        "user": user,
        "store": STORE.info(),
    }


@app.post("/api/auth/login")
def login(username: str = Body(..., embed=True),
          password: str = Body(..., embed=True)) -> dict:
    """First factor. Never issues a session on its own."""
    if auth.is_locked(username):
        raise HTTPException(429, "too many failed attempts — try again shortly")

    user = auth.get_user(username)
    # constant-ish work whether or not the user exists, so timing does not
    # distinguish a wrong username from a wrong password
    ok = bool(user) and auth.check_password(password, user["password_hash"], user["salt"])
    if not ok:
        attempts = auth.record_failure(username)
        raise HTTPException(401, f"invalid credentials ({auth.MAX_ATTEMPTS - attempts} left)")

    challenge = auth.start_challenge(username)
    enrolled = user["mfa_enrolled"] == "1"
    payload = {
        "mfa_required": True,
        "challenge": challenge,
        "enrolled": enrolled,
        "expires_in": auth.CHALLENGE_TTL,
    }
    if not enrolled:
        # shown once, during enrolment only
        payload["totp_secret"] = user["totp_secret"]
        payload["provisioning_uri"] = auth.provisioning_uri(username, user["totp_secret"])
    return payload


@app.post("/api/auth/verify")
def verify(response: Response,
           challenge: str = Body(..., embed=True),
           code: str = Body(..., embed=True)) -> dict:
    """Second factor. Only this issues a session."""
    username = auth.resolve_challenge(challenge)
    if not username:
        raise HTTPException(401, "challenge expired — sign in again")

    user = auth.get_user(username)
    if not user or not auth.verify_totp(user["totp_secret"], code):
        attempts = auth.record_failure(username)
        if attempts >= auth.MAX_ATTEMPTS:
            auth.consume_challenge(challenge)
            raise HTTPException(429, "too many failed attempts — try again shortly")
        raise HTTPException(401, "invalid verification code")

    auth.consume_challenge(challenge)
    auth.clear_failures(username)
    auth.mark_enrolled(username)

    token = auth.start_session(username)
    response.set_cookie(auth.SESSION_COOKIE, token, max_age=auth.SESSION_TTL,
                        httponly=True, samesite="lax")
    return {"authenticated": True, "user": username, "role": user.get("role")}


@app.post("/api/auth/logout")
def logout(request: Request, response: Response) -> dict:
    auth.end_session(request.cookies.get(auth.SESSION_COOKIE))
    response.delete_cookie(auth.SESSION_COOKIE)
    return {"authenticated": False}


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
def health() -> dict:
    """Liveness only. Deliberately does no work.

    This used to resolve the caller's workspace, which generates a dataset, fits
    a model and runs a scan - about 2.5s on a developer machine and far longer on
    a small instance. A platform health check that expensive fails, gets retried,
    and takes the deploy down with it. That is exactly what happened on Render.

    Anything that needs a workspace belongs on /api/overview.
    """
    return {
        "status": "ok",
        "pool": POOL.stats(),
        "store": STORE.info(),
        "database": DB.info(),
        "auth_enabled": auth.AUTH_ENABLED,
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
        "rules": ws.rule_summary,
        "accounts_monitored": ws.rule_summary.get("accounts_monitored", 0),
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
    ws.invalidate_traces()
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


@app.get("/api/datasets")
def datasets() -> dict:
    """What PostgreSQL is holding — the system of record for every dataset."""
    return {"database": DB.info(), "datasets": DB.catalogue()}


@app.get("/api/rules")
def rules_view(request: Request, response: Response,
               limit: int = 50, monitored_only: bool = False) -> dict:
    """What the rule engine decided, and why.

    This is the gate in front of chain tracing: only accounts listed here as
    monitored have their transactions seeded into the walk.
    """
    ws = resolve_workspace(request, response)
    alerts = ws.alerts
    if monitored_only:
        alerts = [a for a in alerts if a.monitored]
    return {
        "dataset": ws.label,
        "summary": ws.rule_summary,
        "count": len(alerts),
        "alerts": [a.as_dict() for a in alerts[:limit]],
    }


@app.get("/api/rules/{account_id}")
def rules_for_account(account_id: str, request: Request, response: Response) -> dict:
    ws = resolve_workspace(request, response)
    for alert in ws.alerts:
        if alert.account == account_id:
            return alert.as_dict()
    if account_id not in ws.features.index:
        raise HTTPException(404, f"account {account_id} not found")
    return {"account": account_id, "score": 0.0, "monitored": False,
            "rules": [], "triggers": [],
            "detail": "no rule fired for this account in the current window"}


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
    params_key = f"{min_forward_pct:.2f}:{max_gap_hours:g}:{max_hops}"

    cached = ws.cached_trace(txn_id, params_key)
    if cached is not None:
        # the verdict can have moved since the walk was cached
        recorded = ws.feedback.for_chain(txn_id)
        cached["review"] = recorded.as_dict() if recorded else None
        return cached

    try:
        walk = ws.graph.chain_walk(txn_id, _params(min_forward_pct, max_gap_hours, max_hops))
    except KeyError:
        raise HTTPException(404, f"transaction {txn_id} not found")
    walk["risk"] = ws.graph.score_chain(walk, ws.model.score)
    payload = _decorate(ws, walk)
    ws.cache_trace(txn_id, params_key, payload)
    return payload


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
    return {"accounts": ws.model.ranked(limit=limit, min_score=min_score),
            "ranked_in_store": ws.top_mule_accounts(limit)}


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


def _serve_page(path: Path, request: Request) -> FileResponse:
    """Serve a page and start a new session on it.

    Every page load hands out a fresh dataset, so opening or reloading the site
    shows a different network. The cookie then keeps that dataset stable for all
    the API calls the page makes, so working a case does not shift underneath
    you - only a reload moves to new data.

    `?seed=` opts out and pins a dataset, for reproducing exact numbers.
    """
    response = FileResponse(path)
    # the page must actually reach the server on reload, or it would keep
    # replaying a cached copy and never pick up a new session
    response.headers["Cache-Control"] = "no-store, must-revalidate"
    if "seed" not in request.query_params:
        response.set_cookie(SESSION_COOKIE, secrets.token_urlsafe(12),
                            max_age=60 * 60 * 24 * 7, httponly=True, samesite="lax")
    return response


if FRONTEND.exists():
    app.mount("/assets", StaticFiles(directory=FRONTEND), name="assets")

    @app.get("/")
    def index(request: Request) -> FileResponse:
        return _serve_page(FRONTEND / "index.html", request)

    @app.get("/login")
    def login_page(request: Request) -> FileResponse:
        return _serve_page(FRONTEND / "login.html", request)

    @app.get("/console")
    def console(request: Request) -> FileResponse:
        target = FRONTEND / "console.html"
        if not target.exists():
            raise HTTPException(404, "console not built yet")
        return _serve_page(target, request)
