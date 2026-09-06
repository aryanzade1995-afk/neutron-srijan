# neutron-srijan

**MuleTrace** — mule-account transaction chain detection for UPI/NPCI.

*Transaction chain intelligence for the interbank rail*

---

## The idea in one line

When a UPI fraud victim's money enters the network it doesn't stop at one account — it hops through a chain of mule accounts within minutes. MuleTrace models transactions as a time-stamped directed graph, walks the chain forward hop by hop from a flagged transaction, and names the **end node** — the account still holding the money — so it can be frozen before cash-out.

> Existing tools tell you an account *looks* suspicious. MuleTrace tells you which account *still has the money*.

## Quickstart

```bash
python -m venv .venv && .venv/Scripts/python.exe -m pip install -r requirements.txt
```

```bash
.venv/Scripts/python.exe backend/generate_data.py
```

```bash
.venv/Scripts/python.exe -m uvicorn app:app --app-dir backend --port 8000
```

Then open <http://localhost:8000> for the landing page and <http://localhost:8000/console> for the investigator console.

To reproduce every number quoted below:

```bash
.venv/Scripts/python.exe backend/evaluate.py
```

## How it works

- **Graph model** — accounts are nodes, transactions are directed edges carrying `(amount, timestamp, mode)`.
- **Temporal chain walk** — an outgoing transfer continues the chain only if it happens *after* the money arrived, within a 48 h window, and moves 70–125% of the amount. `hop_count` counts transfers, so it always equals `len(path) - 1`. The upper bound matters: without it the walk hops onto the account's own unrelated, larger transfers.
- **Split detection** — if no single hop qualifies, the walk aggregates everything leaving inside the window, so smurfing the money into five transfers doesn't duck the threshold.
- **Termination** — the walk stops at a cash-out (ATM / untraceable merchant), at a cycle, or where no qualifying hop exists. That account is the end node.
- **Risk scoring** — a Random Forest over 19 explainable per-account features ranks accounts as probable mules so chains surface *before* a complaint is filed.

## Results

Measured by `backend/evaluate.py` against 45 injected chains with known ground truth, on the reference dataset (`--seed 26`). The served app builds a fresh dataset per session, so the console's own figures will differ from these — see [A different dataset for every session](#a-different-dataset-for-every-session).

**Chain walk** — given a flagged transaction, find the account still holding the money:

| Metric | Value |
|---|---|
| End-node accuracy | **100%** |
| Full-path recovery | **100%** |
| Mean hop recall | **100%** |

Read that with the sensitivity sweep below, not on its own. These chains were generated to the same temporal logic the walk looks for, so the headline number is close to circular. What the sweep shows is that the default thresholds sit on a genuine plateau, and that the rule degrades in *both* directions:

| Walk setting | End-node accuracy |
|---|---|
| strict (85% forwarded, 6 h) | 8.9% |
| tight (80%, 24 h) | 37.8% |
| **default (70%, 48 h)** | **100%** |
| loose (60%, 96 h) | 100% |
| very loose (40%, 168 h) | 88.9% |

Over-tightening misses real hops. Over-loosening is the more interesting failure: the walk starts admitting unrelated transfers and wanders off the actual money trail.

**Proactive scan** — surface chains with no complaint filed:

| Metric | Value |
|---|---|
| Recall vs. injected chains | **100%** |
| Precision @ 10 / @ 20 / @ 30 | **100% / 100% / 100%** |
| Precision over the whole surfaced set | 75.8% |

The gap between those last two rows is the point. All 45 real chains rank above every false positive, so an investigator working the queue top-down clears every genuine chain before meeting a single false alarm. The 75.8% is what you get counting the tail nobody needs to work.

**Mule classifier** — Random Forest over 19 explainable per-account features:

| Metric | Random Forest | Logistic baseline |
|---|---|---|
| ROC AUC | **0.918** | 0.853 |
| Average precision | **0.766** | — |
| Precision / Recall / F1 | 0.667 / 0.633 / 0.650 | 0.453 / 0.717 / 0.555 |

These are deliberately not perfect. An earlier version of the generator opened every mule account fresh, so `account_age_days` separated the classes outright and every metric read 1.0 — an artifact, not a result. Mules are now recruited the way real rings recruit: **35% freshly opened, 50% existing accounts turned, 15% dormant accounts reactivated**, so most arrive with an ordinary age and a real history. Legitimate high-value P2P was widened to overlap the fraud amount range for the same reason. Account age has fallen from the top feature (0.233) to fifth (0.086), and the model now leans on turnaround speed and amount behaviour.

**What transfers and what does not:** the chain walk is deterministic and rule-based, and transfers directly. The classifier is a *ranking aid* trained on synthetic behaviour — it would need retraining on NPCI's real graph, which is exactly what the feedback loop below accumulates labels for.

## Layout

```
backend/
  generate_data.py   synthetic UPI network + injected fraud chains (seeded, deterministic)
  graph_engine.py    temporal graph, chain walk, proactive scan, chain scoring
  risk_model.py      per-account features + Random Forest / Logistic Regression
  feedback.py        append-only investigator verdict log -> training labels
  workspace.py       per-session datasets, built from a seed and LRU cached
  evaluate.py        validates walk + classifier, plus the sensitivity sweep
  app.py             FastAPI: trace, risk-score, chains, watchlist, feedback
tests/
  test_chain_walk.py  one test per hop-admission rule, on hand-built graphs
  test_api.py         endpoint contracts and the feedback loop
frontend/
  index.html         landing page
  globe.js           procedural canvas globe backdrop (no stock imagery)
  console.html       investigator console
  app.js             console logic, vis-network graph
  styles.css         shared design tokens + console
  landing.css        landing page
docs/
  pitch.md           full pitch
  technical-brief.md technical brief
```

## API

| Endpoint | Purpose |
|---|---|
| `GET /api/trace/{txn_id}` | Walk one flagged transaction to its end node. Accepts `min_forward_pct`, `max_gap_hours`, `max_hops`. |
| `GET /api/risk-score/{account_id}` | Mule probability plus the per-feature percentiles behind it. |
| `GET /api/chains` | Ranked active chains from the proactive scan. `include_dismissed` to see reviewed-away ones. |
| `GET /api/watchlist` | Accounts the classifier flags, highest score first. |
| `GET /api/overview` | Dashboard totals and the model report. |
| `POST /api/feedback` | Record an investigator verdict on a chain. |
| `GET /api/feedback` | Verdict log and review counts. |
| `GET /api/feedback/labels` | Accumulated supervision available to the next retrain. |
| `POST /api/rescan` | Re-run the proactive scan and refresh the queue. |
| `POST /api/reload` | Issue a fresh session and build a new dataset for the caller. |
| `GET /api/evaluation` | Validation figures recomputed for the caller's dataset. |
| `GET /api/health` | Liveness, the caller's dataset id, current counts and pool stats. |

Every endpoint resolves the caller's workspace from the `mt_session` cookie. Append `?seed=<int>` to pin a specific dataset instead.

## A different dataset for every session

Nothing on the site is hardcoded, and no two visitors see the same network. Each browser session gets a seed, the seed builds its own synthetic dataset, and every figure the client sees is derived from it. Five sessions side by side:

| Session | Transactions | Accounts | Chains detected | Fraud chains | Recoverable |
|---|---|---|---|---|---|
| DS-15001 | 15,427 | 1,146 | 74 | 51 | ₹47.31 L |
| DS-10151 | 18,535 | 1,398 | 57 | 37 | ₹21.91 L |
| DS-69869 | 14,114 | 1,410 | 74 | 63 | ₹41.75 L |
| DS-74197 | 10,968 | 1,105 | 84 | 66 | ₹44.42 L |
| DS-94544 | 15,869 | 1,303 | 79 | 56 | ₹36.76 L |

Scale varies with the seed, not just contents — otherwise every dashboard would still show roughly the same totals and read as canned. The dataset id is shown in the console header so two people can see immediately that they are on different data.

**Every page load hands out a new dataset**, so opening or refreshing the site shows a different network. The session cookie then keeps that dataset fixed for all the API calls the page makes, so tracing, dismissing and rescanning never shift the data underneath you - only a reload moves to new data. Building one costs about 1.5 s, so they are made on demand and held in a 12-entry LRU cache.

- **New dataset** — reload the page, or use the refresh control in the console header. `POST /api/reload` does the same over HTTP.
- **Pin a dataset** — append `?seed=26` to any page or endpoint and it stays fixed across reloads. Useful when the numbers need to match a slide, or when two people want to look at the same case. The page forwards the seed to every call it makes.

Validation figures follow the session's data too: `GET /api/evaluation` recomputes chain-walk accuracy, scan precision and the sensitivity sweep for that workspace, so the Model tab is never quoting another dataset's results. Expect them to move between sessions — a harder draw might give 96.1% end-node accuracy where an easier one gives 100%, and that variation is the honest picture.

The dashboard counts and the chain table both come from one `visible_chains()` definition, so they cannot drift apart — dismissing a chain decrements the stat card and removes the row in the same step. Feedback is per-session too, so one visitor's dismissals never affect another's queue.

`backend/generate_data.py` still writes a dataset to `data/` for reproducible offline evaluation via `backend/evaluate.py`; the served app does not read those files.

## Access control

The console carries case data — victim VPAs, account ages, freeze
recommendations — so it supports password + TOTP (RFC 6238) multi-factor sign-in,
enforced by one middleware in front of every data route rather than per-route
checks.

**It is on by default.** To run the console open for a rehearsal:

```bash
MULETRACE_AUTH=off .venv/Scripts/python.exe -m uvicorn app:app --app-dir backend --port 8000
```

### Accounts

There is no external identity provider — password and TOTP are both verified
locally, and any authenticator app works (Google Authenticator, Authy, Microsoft
Authenticator, 1Password).

Accounts persist to `data/users.json` (gitignored), because a TOTP secret that
regenerated on every restart would mean re-enrolling your phone each boot. Only
sessions, challenges and the failure counter are ephemeral — those live in the
store and losing them just means signing in again.

On first boot with no account, the server creates one and prints the username,
password and TOTP key **once**. To set them yourself:

```bash
MULETRACE_USER=investigator MULETRACE_PASSWORD='your-password' .venv/Scripts/python.exe -m uvicorn app:app --app-dir backend --port 8000
```

### Getting the one-time code

The second factor comes from any authenticator app — Google Authenticator,
Microsoft Authenticator, Authy, 1Password. Two ways to enrol:

- **Scan the QR.** `/login` shows one on the first sign-in for an account that
  has not enrolled yet. It is shown once, so if that sign-in has already
  happened, re-arm it:

  ```bash
  .venv/Scripts/python.exe scripts/otp.py --reset-enrolment
  ```

- **Enter the key by hand.** Print the secret and `otpauth://` URI:

  ```bash
  .venv/Scripts/python.exe scripts/otp.py --enrol
  ```

Without a phone — for scripted testing, or a demo where holding a phone to a
projector is awkward — print the current code directly:

```bash
.venv/Scripts/python.exe scripts/otp.py --watch
```

That reads the shared secret from `data/users.json`, so anyone who can run it
can sign in. It is a local development convenience, not something to point at a
real deployment.

To add or reset an account:

```bash
.venv/Scripts/python.exe -c "import sys; sys.path.insert(0,'backend'); import auth; r=auth.create_user('investigator','your-password'); print(auth.provisioning_uri('investigator', r['totp_secret']))"
``` `/login` walks two steps — credentials, then the six-digit code — and shows
a QR for enrolment. The first factor only issues a 180-second challenge; only the
second mints a session. Challenges are single-use, failed attempts throttle at
five per five minutes, and the enrolment secret is returned exactly once.

Passwords are scrypt with a per-user salt. TOTP is implemented on the standard
library rather than adding a dependency.

## Data pipeline

    PostgreSQL  ->  Redis  ->  in-memory graph
     durable        working      detection
     truth          copy

**PostgreSQL is the system of record.** Every dataset — accounts, transactions and
the labelled chains — is written there and can be queried with SQL by anyone
auditing a case. In a real deployment this is where the bank's own warehouse
plugs in.

**Redis holds the working copy**: a serialised snapshot of the dataset in play,
plus the detection output (ranked chains, per-account scores, cached traces).
Losing it costs a reload from Postgres, nothing more.

A seed nobody has produced yet is generated once, persisted to Postgres, and
cached in Redis. After that it is served from Redis, falling back to Postgres
when the cache is cold. Measured on a 12,275-transaction dataset:

| Path | Cost |
|---|---|
| Cold — generate, persist, build | 1.63 s |
| Warm — Redis working copy | 1.03 s |
| Redis cleared — read from PostgreSQL | 1.23 s |

Both tiers are optional downward: no Redis means reading Postgres each time, no
Postgres means generating in memory. Neither absence stops the app, and
`/api/health` reports which tiers are live.

Point elsewhere with `MULETRACE_POSTGRES_URL`; the application database is
created on first use. Install on Windows from an **elevated** terminal:

```powershell
winget install --id PostgreSQL.PostgreSQL.17 --accept-package-agreements --accept-source-agreements
```

`GET /api/datasets` lists what Postgres is holding.

## Store

`backend/store.py` wraps Redis and holds three things:

- **Datasets** — a generated network is serialised once and reused, so a seed
  that has been built before is not regenerated.
- **Detection patterns** — ranked chains in a sorted set keyed by risk score,
  per-account mule scores in a hash, so "worst chains right now" and "score this
  account" are a single command instead of a rescan.
- **Auth state** — sessions and pending challenges, expiring on their own TTLs.

The detection itself stays in the temporal chain walk and the classifier. Redis
stores and serves what they found; it does not do the recognising.

If no Redis answers, an in-process backend with the same interface takes over so
the console still runs. Which one is live is reported on `/api/health` and
`/api/auth/status`, and the login page says so — a silent fallback would be worse
than none. Point `MULETRACE_REDIS_URL` at a server to switch, no code change.

### Running a Redis

On Windows, Memurai is a Redis-compatible service and installs in one step from
an **elevated** terminal (the MSI's custom actions fail without elevation):

```powershell
winget install --id Memurai.MemuraiDeveloper --accept-package-agreements --accept-source-agreements
```

Alternatives: `docker run -d -p 6379:6379 redis:7-alpine`, or set
`MULETRACE_REDIS_URL` to a managed instance.

Then verify — this exercises exactly the operations the app issues and cleans up
after itself:

```bash
.venv/Scripts/python.exe scripts/check_redis.py
```

`tests/test_store.py` runs the same 19 assertions against both backends. They
skip when no server answers, so a green run without Redis is not evidence the
Redis path works; run them once a server is up.

## Feedback loop

Closed cases are the only route to labels that are not synthetic, so they are captured as first-class data. `POST /api/feedback` writes an append-only JSON Lines log (`data/feedback.jsonl`), replayed into memory at boot. Append-only because a freeze decision is an audit trail: a verdict is superseded by a later entry, never edited in place.

A confirmed chain marks its end node as a positive label and a dismissed one as a negative; `GET /api/feedback/labels` exposes the accumulated set. Labels are **not** applied to the live scorer — a model that shifted under an investigator mid-review would make the queue untrustworthy — they are staged for the next deliberate retrain. Dismissed chains drop out of the working queue.

## Tests

```bash
.venv/Scripts/python.exe -m pytest
```

54 tests. `tests/test_chain_walk.py` pins each hop-admission rule on small hand-built graphs — causal ordering, the time window, the forward-percentage floor and ceiling, split detection, cycle and hop guards, cash-out termination — because these are the decisions a bank would have to justify. `tests/test_api.py` boots the app through its lifespan hook and covers the endpoint contracts, the feedback loop, that the dashboard counts can never disagree with the chain table, and that a pinned seed is reproducible while different seeds give different networks.

## Honest scope

The demo runs on **synthetic data** — real UPI transaction data isn't accessible outside NPCI/banks, so the figures above show the logic works as specified, not what it would score in production. Cash-out (ATM, untraceable merchant, crypto off-ramp) is a dead end for tracing: the end node can be named, but funds already withdrawn can't be recovered by a freeze. Any real deployment needs a human reviewer before a freeze — a wrong freeze on a genuine account is a serious harm.

## Docs

- [Full pitch](docs/pitch.md) — problem, architecture, workflow, AI integration, limitations, scalability, budget, impact.
- [Technical brief](docs/technical-brief.md) — data model, detection logic, positioning vs. MuleHunter.AI.
