# neutron-srijan

**MuleTrace** — mule-account transaction chain detection for UPI/NPCI.

*Team Neutron · Srijan 26 (GH Raisoni College) · Fraud Detection domain*

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

Measured by `backend/evaluate.py` against 45 injected chains with known ground truth.

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
| `POST /api/reload` | Rebuild graph, model and scan from the dataset on disk. `force=true` to rebuild regardless. |
| `GET /api/evaluation` | Validation figures recomputed for the loaded dataset. |
| `GET /api/health` | Liveness, current counts, and whether the data on disk has changed. |

## Live figures — nothing is hardcoded

Every number on the landing page and in the console is read from the running service against the dataset currently in memory. There are no figures written into the markup, so changing the data changes the whole product.

Regenerate the dataset with any parameters you like while the server is running:

```bash
.venv/Scripts/python.exe backend/generate_data.py --chains 70 --txns 20000 --seed 99
```

The service fingerprints the CSVs on every request, so it notices immediately. The refresh control in the console header turns amber, and clicking it reloads the graph, refits the model, re-runs the scan and re-renders every figure — no restart. `POST /api/reload` does the same thing from the command line.

Validation figures follow too: `GET /api/evaluation` recomputes the chain-walk accuracy, scan precision and the sensitivity sweep against whatever is loaded, and is invalidated whenever the data or the scan changes. The Model tab in the console shows those live rather than reading `data/evaluation.json`.

The dashboard counts and the chain table are derived from one `visible_chains()` definition, so they cannot drift apart — dismissing a chain decrements the stat card and removes the row in the same step.

## Feedback loop

Closed cases are the only route to labels that are not synthetic, so they are captured as first-class data. `POST /api/feedback` writes an append-only JSON Lines log (`data/feedback.jsonl`), replayed into memory at boot. Append-only because a freeze decision is an audit trail: a verdict is superseded by a later entry, never edited in place.

A confirmed chain marks its end node as a positive label and a dismissed one as a negative; `GET /api/feedback/labels` exposes the accumulated set. Labels are **not** applied to the live scorer — a model that shifted under an investigator mid-review would make the queue untrustworthy — they are staged for the next deliberate retrain. Dismissed chains drop out of the working queue.

## Tests

```bash
.venv/Scripts/python.exe -m pytest
```

35 tests. `tests/test_chain_walk.py` pins each hop-admission rule on small hand-built graphs — causal ordering, the time window, the forward-percentage floor and ceiling, split detection, cycle and hop guards, cash-out termination — because these are the decisions a bank would have to justify. `tests/test_api.py` boots the app through its lifespan hook and covers the endpoint contracts, the feedback loop, and that the dashboard counts can never disagree with the chain table.

## Honest scope

The demo runs on **synthetic data** — real UPI transaction data isn't accessible outside NPCI/banks, so the figures above show the logic works as specified, not what it would score in production. Cash-out (ATM, untraceable merchant, crypto off-ramp) is a dead end for tracing: the end node can be named, but funds already withdrawn can't be recovered by a freeze. Any real deployment needs a human reviewer before a freeze — a wrong freeze on a genuine account is a serious harm.

## Docs

- [Full pitch](docs/pitch.md) — problem, architecture, workflow, AI integration, limitations, scalability, budget, impact.
- [Technical brief](docs/technical-brief.md) — data model, detection logic, positioning vs. MuleHunter.AI.
