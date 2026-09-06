# MuleTrace — project context

Paste this whole file into a new assistant session to hand over the project.

---

## What it is

**MuleTrace** — mule-account transaction chain detection for UPI/NPCI. Built for a
hackathon (Fraud Detection domain), intended to be shown to NPCI officials.

When a UPI fraud victim's money is stolen it does not stop at one account — it hops
through 3–6 "mule" accounts within minutes, then cashes out. Each individual
transaction looks legitimate; only the *chain* reveals the fraud. No single bank
sees the whole path because a chain crosses several banks. NPCI sits on the
interbank rail and can see all of it.

MuleTrace models transactions as a **time-stamped directed graph**, walks the chain
forward hop by hop from one flagged transaction, and names the **end node** — the
account still holding the money. That is the only account worth freezing.

Positioning line: *existing tools tell you an account looks suspicious; MuleTrace
tells you which account still has the money.*

Repo: `aryanzade1995-afk/neutron-srijan` (private). Local: `C:\Users\aryan\Documents\projects\neutron-srijan`.

---

## Stack

- **Python 3.14**, venv at `.venv` (Windows: `.venv/Scripts/python.exe`)
- **FastAPI + uvicorn** backend, **networkx** graph, **scikit-learn** classifier,
  **pandas/numpy**, **redis** client
- **Vanilla HTML/CSS/JS** frontend — no build step, no framework. `vis-network`
  (cdnjs) for the graph, `qrcodejs` (cdnjs) for MFA enrolment
- **pytest** — 54 tests, all passing

Run:
```bash
.venv/Scripts/python.exe -m uvicorn app:app --app-dir backend --port 8000
```
Landing `http://localhost:8000`, console `/console`. Tests: `.venv/Scripts/python.exe -m pytest`.

---

## Layout

```
backend/
  generate_data.py   synthetic UPI network + injected fraud chains (seeded, deterministic)
  graph_engine.py    temporal graph, chain walk, proactive scan, chain scoring
  risk_model.py      19 per-account features + RandomForest / LogisticRegression
  workspace.py       per-session datasets, LRU pool, publishes patterns to the store
  store.py           Redis wrapper + in-process fallback (datasets, patterns, auth state)
  auth.py            scrypt passwords + TOTP (RFC 6238), sessions, challenges, throttling
  feedback.py        append-only investigator verdict log -> training labels
  evaluate.py        validates walk + classifier, plus a threshold sensitivity sweep
  app.py             FastAPI: auth gate, trace, chains, watchlist, feedback, evaluation
frontend/
  index.html         landing page (all figures fetched live)
  console.html       investigator console
  login.html         two-step MFA sign-in
  app.js             console logic, vis-network graph, freeze-report modal
  globe.js           procedural canvas globe backdrop (no stock imagery)
  styles.css         design tokens + console
  landing.css        landing page
  freeze-report.css  freeze-request report modal
tests/
  test_chain_walk.py  one test per hop-admission rule, on hand-built graphs (14)
  test_api.py         endpoint contracts, feedback loop, seeding (23)
  test_auth.py        TOTP, both factors, replay, lockout (17)
docs/
  pitch.md, technical-brief.md   original hackathon docs
```

---

## Core algorithm — the temporal chain walk

`backend/graph_engine.py`. Accounts are nodes, transactions are directed edges
carrying `(amount, timestamp, mode)`. From a flagged transaction, follow outgoing
edges forward. An edge is admitted as "the same money moving on" only if:

1. **Causal** — it happens *after* the money arrived
2. **Inside the window** — within 48 h (configurable)
3. **Forwards most of it** — between **70% and 125%** of the amount received

The 125% **ceiling** matters: without it the walk hops onto the account's own
unrelated larger transfers and reports impossible forward percentages.

Also handles:
- **Split/smurfing detection** — if no single hop qualifies, aggregate everything
  leaving inside the window; follow the largest leg, record the siblings
- **Termination** — cash-out (ATM / untraceable merchant), cycle, hop ceiling, or
  no qualifying hop. Where it stops is the end node
- `hop_count` counts transfers, so it always equals `len(path) - 1`

The walk is deliberately **rule-based and deterministic** — a bank has to justify a
freeze, so the traversal stays explainable and every hop carries the numbers that
admitted it. Only the *scoring* is ML.

---

## Results (verified, reproducible)

`backend/evaluate.py` on the reference on-disk dataset (`generate_data.py` defaults,
seed 26): 14,654 transactions, 1,107 accounts, 45 injected chains.

**Chain walk** — 100% end-node accuracy, 100% full-path recovery, 100% mean hop recall.

**Sensitivity sweep** (the important part — the headline is near-circular on its own,
since the chains were generated to the same temporal logic the walk looks for):

| Setting | End-node accuracy |
|---|---|
| strict (85% fwd, 6 h) | 8.9% |
| tight (80%, 24 h) | 37.8% |
| **default (70%, 48 h)** | **100%** |
| loose (60%, 96 h) | 100% |
| very loose (40%, 168 h) | 88.9% |

Both over-tightening and over-loosening hurt. Over-loosening is the interesting
failure: the walk starts admitting unrelated transfers and wanders off the trail.

**Proactive scan** (no complaint filed): recall 100%, precision@10/20/30 all 100%,
precision over the whole surfaced set 75.8%, clean prefix 47. Every false positive
ranks *below* all 45 real chains, so an investigator working top-down clears every
genuine chain before meeting a false alarm.

**Mule classifier**: RandomForest AUC **0.918** / F1 0.650, vs LogisticRegression
baseline AUC 0.853 / F1 0.555.

> **Critical context on the classifier.** An earlier version of the generator opened
> every mule account fresh, so `account_age_days` separated the classes outright and
> every metric read **1.0** — an artifact, not a result. Mules are now recruited the
> way real rings recruit: **35% freshly opened, 50% existing accounts turned, 15%
> dormant reactivated**, so most arrive with ordinary age and real history.
> Legitimate high-value P2P was widened to ₹45k–420k so transaction size could not
> become the next free answer. Account age fell from top feature (0.233) to fifth
> (0.086). **Do not "improve" these numbers back toward 1.0 — that would be
> reintroducing leakage.**

**What transfers to real data:** the chain walk, directly. The classifier is a
*ranking aid* trained on synthetic behaviour and would need retraining on NPCI's
real graph — which is what the feedback loop accumulates labels for.

---

## Per-session datasets

There is no single canned dataset. **Every page load hands out a new synthetic
network.** The page routes issue a fresh session cookie; the cookie seeds a
workspace; every figure the client sees derives from it. Scale varies with the seed
(9k–22k transactions, 700–1400 accounts, 28–70 chains), not just contents —
otherwise every dashboard would show roughly the same totals and read as canned.

- The cookie **fixes** the dataset for all API calls that page makes, so tracing,
  dismissing and rescanning never shift data mid-investigation. Only a reload moves.
- `?seed=26` on any page or endpoint **pins** a dataset reproducibly (for slides).
  The page forwards the seed to every call it makes.
- Pages are served `Cache-Control: no-store` so a reload actually reaches the server.
- Workspaces are LRU-cached (12); building one costs ~1.5 s.

**Nothing numeric is hardcoded in the markup.** The landing page uses 16
`data-metric` placeholders filled from `/api/overview` and `/api/evaluation`.

---

## Redis store

`backend/store.py` wraps Redis and holds three things:

- **Datasets** — a generated network serialised once (zlib + JSON) and reused
- **Detection patterns** — ranked chains in a **ZSET** keyed by risk score,
  per-account mule scores in a **HASH**, so "worst chains now" / "score this
  account" are one command instead of a rescan
- **Auth state** — sessions and pending challenges, expiring on their own TTLs

**Be accurate about this:** Redis is a data store, not an inference engine. The
detection stays in the chain walk and the classifier; Redis stores and serves what
they found. Do not describe it as doing pattern recognition.

**No Redis server is running on the dev machine**, so an in-process backend with the
same interface takes over. Which one is live is reported on `/api/health` and
`/api/auth/status`, and stated on the login page. Point `MULETRACE_REDIS_URL` at a
server to switch — no code change.

---

## Authentication (currently OFF)

Password + TOTP (RFC 6238), enforced by one middleware in front of every data route.
TOTP implemented on the standard library; passwords are scrypt with per-user salt.
First factor issues only a 180 s challenge — **only the second factor mints a
session**. Challenges are single-use, failures throttle at 5 per 5 min, enrolment
secret returned exactly once.

**It ships OFF by default** so a live demo cannot hit a login wall. Enable with:
```bash
MULETRACE_AUTH=on .venv/Scripts/python.exe -m uvicorn app:app --app-dir backend --port 8000
```
The 17 auth tests force it on for their own duration, so it stays proven.

---

## Feedback loop

`POST /api/feedback` writes an **append-only** JSON Lines verdict log per seed
(`data/feedback/<seed>.jsonl`), replayed at boot. Append-only because a freeze
decision is an audit trail — a verdict is superseded by a later entry, never edited.

Confirmed chains mark their end node a positive label, dismissed ones negative;
`GET /api/feedback/labels` exposes the set. Labels are **not** applied to the live
scorer — a model shifting under an investigator mid-review would make the queue
untrustworthy — they are staged for a deliberate retrain. Dismissed chains drop out
of the queue.

---

## API

| Endpoint | Purpose |
|---|---|
| `GET /api/trace/{txn_id}` | Walk one transaction to its end node. `min_forward_pct`, `max_gap_hours`, `max_hops` |
| `GET /api/chains` | Ranked active chains. `include_dismissed`, `band` |
| `GET /api/overview` | Dashboard totals + model report |
| `GET /api/risk-score/{account_id}` | Mule probability + per-feature percentiles |
| `GET /api/watchlist` | Accounts the classifier flags |
| `GET /api/evaluation` | Validation figures recomputed for the caller's dataset |
| `POST /api/feedback` · `GET /api/feedback` · `GET /api/feedback/labels` | Verdicts and labels |
| `POST /api/rescan` · `POST /api/reload` | Re-scan; new dataset |
| `GET /api/health` | Counts, dataset id, store backend, auth mode |
| `POST /api/auth/login` · `/verify` · `/logout` · `GET /api/auth/status` | MFA |

Every endpoint resolves the caller's workspace from the `mt_session` cookie;
`?seed=<int>` pins one.

---

## UI

Two pages plus a login. **Theme: graphite/teal, deliberately not blue-gradient SaaS** —
it is meant to read institutional for an NPCI audience.

Tokens in `frontend/styles.css`: bg `#0b100f`, surfaces `#18201f`/`#263331`, accent
teal `#4f8f86`, mint `#9bc8bd`, text `#e8eeec`. Status colours desaturated
(`#6aa88f` / `#c99a4e` / `#bf5f66`) so it reads analytical, not alarmist. Solid
colours over gradients throughout.

- **Landing** — procedural canvas globe backdrop (`globe.js`, no stock imagery — the
  original reference was a watermarked Shutterstock comp, rebuilt procedurally),
  section-aware sliding nav indicator with scrollspy, live metric strip.
- **Console** — end-node banner, 4 stat cards **describing the selected chain**
  (they change on Next chain / row click; queue-wide figures kept in each sub-line),
  vis-network money trail, hop ledger, ranked chains table. No nav bar.
- **Freeze-request report modal** — replaced `window.confirm`/`alert`. Ten sections
  (request details, target account, transaction details, basis, fraud indicators,
  money trail, hop ledger, requested action, evidence, regulatory notice), all
  populated dynamically. 80vw / max 1300px / 88vh, internal scroll.

**Two integrity rules in the modal, do not "fix" them:**
1. Fields the feed does not carry (account number, IFSC, holder name, RRN/UTR,
   complaint ref) render as *"not carried in this feed"* — **never invent
   plausible-looking bank identifiers on a freeze request document.**
2. On submit it says *"Request sent — awaiting bank acknowledgement… no freeze is in
   force until the bank confirms."* It must **never** claim the account is frozen.

Cash-out chains keep a guard: the report opens as an evidence pack with a critical
notice, first card reads "Amount lost", submit hidden — a freeze cannot recover
funds that left the banking channel.

---

## Known bugs found and fixed (do not reintroduce)

- **`hop_count` off-by-one** — was `len(hops) - 1`, so a 3-transfer chain reported 2.
  The scan filters `min_hops=3`, so it silently discarded every 3-mule chain. Fixing
  it took scan recall 86.7% → 100%.
- **`max_hops` inclusive bound** — a cap of 4 returned 5 hops.
- **Missing forward-percentage ceiling** — walk hopped onto unrelated larger
  transfers, UI showed "20699% forwarded".
- **Stat cards never updated on chain selection** — `renderStats()` was only called
  from `refresh()`, never `runTrace()`.
- **Dashboard vs table disagreement** — overview counted raw chains, table filtered
  dismissed. Both now derive from one `visible_chains()`.
- **Dataset loaded once at startup** — regenerating data needed a restart.
- **`store.py` printed to stdout** — corrupted any script importing it. Now stderr.
- **Dataset cache round-trip** — `pd.read_json` converted the timestamp column to
  `Timestamp`, so cached datasets stopped matching fresh ones. Fixed with
  `convert_dates=False, convert_axes=False`.

---

## Outstanding

- `docs/pitch.md` and `docs/technical-brief.md` still quote **pre-fix numbers** and
  still say "Srijan 26" — they contradict the README and the live app. Not yet
  updated.
- `Dismiss` and `new dataset` buttons still use `confirm()`/`alert()` (only the
  freeze interaction was upgraded to a modal).
- No Redis server on the dev machine — only the fallback path is exercised locally.
- Backup/recovery codes for MFA not implemented.

## Working agreement

- **Do not push to GitHub.** Commit locally only. There are currently local commits
  ahead of origin.
- Verify changes in the browser before claiming they work; do not report success
  from code inspection alone.
- Keep the honesty framing intact — synthetic-data caveats, the "not carried in this
  feed" fields, the "awaiting acknowledgement" wording, and the sensitivity sweep are
  deliberate credibility features, not omissions to be polished away.
