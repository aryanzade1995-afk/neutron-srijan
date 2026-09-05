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
- **Temporal chain walk** — an outgoing transfer continues the chain only if it happens *after* the money arrived, within a 48 h window, and moves 70–125% of the amount. The upper bound matters: without it the walk hops onto the account's own unrelated, larger transfers.
- **Split detection** — if no single hop qualifies, the walk aggregates everything leaving inside the window, so smurfing the money into five transfers doesn't duck the threshold.
- **Termination** — the walk stops at a cash-out (ATM / untraceable merchant), at a cycle, or where no qualifying hop exists. That account is the end node.
- **Risk scoring** — a Random Forest over 19 explainable per-account features ranks accounts as probable mules so chains surface *before* a complaint is filed.

## Results

Measured by `backend/evaluate.py` against 45 injected chains with known ground truth:

| Metric | Value |
|---|---|
| End-node accuracy (complaint-triggered) | **100%** |
| Full-path recovery | **100%** |
| Chains found by proactive scan, no complaint | **82.2%** |
| Proactive scan precision | **97.4%** |
| Top-10 ranked chains that were genuinely fraudulent | **10/10** |

**On the classifier's perfect scores:** the mule classifier reports precision/recall/AUC of 1.0, and that is a property of the synthetic data, not evidence of real-world performance. Mule accounts are generated young, so `account_age_days` separates the classes almost perfectly. Real mule accounts are recruited from ordinary aged accounts and will not. **The chain walk is the part that transfers to real data; the scorer would need retraining on NPCI's actual graph.**

## Layout

```
backend/
  generate_data.py   synthetic UPI network + injected fraud chains (seeded, deterministic)
  graph_engine.py    temporal graph, chain walk, proactive scan, chain scoring
  risk_model.py      per-account features + Random Forest / Logistic Regression
  evaluate.py        validates walk + classifier against ground truth
  app.py             FastAPI: /api/trace, /api/risk-score, /api/chains, /api/watchlist
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
| `GET /api/chains` | Ranked active chains from the proactive scan. |
| `GET /api/watchlist` | Accounts the classifier flags, highest score first. |
| `GET /api/overview` | Dashboard totals and the model report. |

## Honest scope

The demo runs on **synthetic data** — real UPI transaction data isn't accessible outside NPCI/banks, so the figures above show the logic works as specified, not what it would score in production. Cash-out (ATM, untraceable merchant, crypto off-ramp) is a dead end for tracing: the end node can be named, but funds already withdrawn can't be recovered by a freeze. Any real deployment needs a human reviewer before a freeze — a wrong freeze on a genuine account is a serious harm.

## Docs

- [Full pitch](docs/pitch.md) — problem, architecture, workflow, AI integration, limitations, scalability, budget, impact.
- [Technical brief](docs/technical-brief.md) — data model, detection logic, positioning vs. MuleHunter.AI.
