# neutron-srijan

**MuleTrace** — mule-account transaction chain detection for UPI/NPCI.

*Team Neutron · Srijan 26 (GH Raisoni College) · Fraud Detection domain · 3-hour build*

---

## The idea in one line

When a UPI fraud victim's money enters the network it doesn't stop at one account — it hops through a chain of mule accounts within minutes. MuleTrace models transactions as a time-stamped directed graph, walks the chain forward hop by hop from a flagged transaction, and names the **end node** — the account still holding the money — so it can be frozen before cash-out.

> Existing tools tell you an account *looks* suspicious. MuleTrace tells you which account *still has the money*.

## How it works

- **Graph model** — accounts are nodes, transactions are directed edges carrying `(amount, timestamp)`.
- **Temporal chain walk** — an outgoing transfer continues the chain only if it happens *after* the money arrived, within a short window, and forwards a large fraction of the amount (≥70%). Deterministic and explainable by design.
- **Risk scoring** — a lightweight classifier over per-account features (inbound→outbound gap, forward percentage, fan-in/fan-out, account age, distinct counterparties) flags mule-like accounts proactively, before any complaint exists.
- **End node** — the walk terminates where money stops moving or hits a cash-out. That account gets the "freeze recommended" flag.

## Planned stack

| Layer | Tool |
|---|---|
| Dataset | Python generator — synthetic transactions with injected, labeled fraud chains |
| Graph engine | `networkx` (in-memory) |
| Backend | Flask / FastAPI — `/trace/<txn_id>`, `/risk-score/<account_id>` |
| ML scoring | `scikit-learn` (Logistic Regression / Random Forest) |
| Frontend | HTML/CSS/JS + vis.js or D3 force-directed graph |

## Docs

- [Full pitch](docs/pitch.md) — problem, architecture, workflow, AI integration, limitations, scalability, budget, impact.
- [Technical brief](docs/technical-brief.md) — data model, detection logic, build plan, positioning vs. MuleHunter.AI.

## Honest scope

The demo runs on **synthetic data** — real UPI transaction data isn't accessible outside NPCI/banks, so demo precision/recall are not real-world numbers. Cash-out (ATM, untraceable merchant, crypto off-ramp) is a dead end for tracing, and any real deployment needs a human in the loop before a freeze.
