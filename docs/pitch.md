# MuleTrace — Mule Account Chain Detection for UPI/NPCI

*Fraud Detection domain — Srijan 26*

---

## 1. Problem

Right now if someone gets scammed over UPI, their money doesn't just sit in one account — it gets moved through 3-4 different "mule" accounts really fast before anyone can react, and by the time a complaint is filed the money's usually already gone. No single bank can see the full chain since it crosses multiple banks, but NPCI can, because every UPI transaction passes through it. Investigation today is still manual and reactive: it starts only after a complaint, and by then the money has usually already been withdrawn.

## 2. Proposed Solution

Treat all accounts and transactions like a graph — accounts are nodes, transactions are edges with a time and amount. Once one transaction gets flagged, trace forward through time to see where that specific money went next, hop by hop, until it stops moving. Wherever it stops is the account still holding the money — that's the one that actually needs to be frozen, not just any account that looks suspicious. The core rule: an outgoing transfer only counts as "the same chain" if it happens *after* the money arrived, within a short time window, and moves most of the amount forward — that's what separates a real mule hop from an unrelated coincidence.

## 3. Architecture

```
Bank / UPI switch transaction feed
        │
        ▼
Ingestion layer (stream of txns: sender, receiver, amount, timestamp)
        │
        ▼
Graph engine (accounts = nodes, txns = time-stamped directed edges)
        │
        ├──► Chain-walk module   → traces flagged txn forward hop by hop → end node
        │
        └──► Risk-scoring module → per-account features → mule-probability score
        │
        ▼
Alerting layer (ranked list of active chains + end-node recommendation)
        │
        ▼
Dashboard (investigator view: graph visual, risk score, "freeze recommended" flag)
```

Two things run side by side on the same graph: the **chain-walk** (reactive — given a flagged transaction, find the end node) and the **risk score** (proactive — flags accounts behaving like mules before any complaint exists).

## 4. Workflow

1. A transaction happens and lands in the feed (in the demo: a row in the synthetic dataset; in production: NPCI's real-time switch data).
2. The graph engine adds it as an edge between two account-nodes.
3. A trigger fires — either a victim complaint pointing at a specific transaction, or the risk-scoring module flagging an account on its own.
4. The chain-walk module follows outgoing edges from that account forward in time, only continuing where the next hop happens after the money arrived and forwards most of it.
5. The walk stops naturally when no more qualifying hops exist — that account is the end node.
6. The dashboard surfaces the full chain (victim → mules → end node) with a risk score and a "freeze recommended" flag on the end node.
7. Action taken outside the system (bank/law enforcement freezes the account) gets logged back in as feedback, improving future scoring.

## 5. Usage of AI / AI Integration

- **In the hackathon build:** a lightweight classifier (Logistic Regression or Random Forest) trained on a handful of engineered per-account features — how fast money leaves after it arrives, what percentage gets forwarded, how many different people an account transacts with, account age — to produce a mule-probability score alongside the graph trace. This is deliberately simple so it's trainable and explainable within the 3-hour window.
- **Where this goes in a real version:** a temporal Graph Neural Network trained on NPCI's full cross-bank graph, since NPCI is the only party positioned to see a complete chain rather than one bank's fragment of it. The chain-walk logic itself stays rule-based even at that scale — it's the *scoring* that gets smarter with AI, not the traversal, which needs to stay deterministic and explainable enough for a bank to act on it.
- **Feedback loop:** confirmed fraud cases (post-investigation) become labeled training data, so the model improves the more it's used.

## 6. Tech Stack

| Layer | Tool (hackathon build) |
|---|---|
| Dataset | Python script generating a synthetic transaction set with labeled fraud chains |
| Graph engine | `networkx` (in-memory graph, Python) |
| Backend / API | Flask or FastAPI |
| ML scoring | `scikit-learn` (Logistic Regression / Random Forest) |
| Frontend | HTML/CSS/JS + `vis.js` or D3.js for the force-directed graph visualization |
| Hosting for demo | Local / free-tier deployment (Render, Railway, or just local for judging) |

Everything here is free/open-source — intentional, since it needs to be built and run in a single 3-hour window on a laptop.

## 7. Limitations

- **No real data.** The demo runs on a synthetic dataset with injected fraud chains — real UPI transaction data isn't accessible outside NPCI/banks, so precision and recall numbers from the demo aren't real-world numbers.
- **Evasion is possible.** A sophisticated ring can break the "fast + high-percentage" pattern deliberately — hold money for days, split it into many small transfers, or cash out and redeposit through informal channels — none of which shows up as a clean temporal edge in the graph.
- **Cash-out is a dead end for tracing.** Once money leaves via ATM withdrawal or an untraceable merchant/crypto off-ramp, the graph has nothing more to walk — the end node can be flagged, but funds already withdrawn in cash can't be recovered by freezing an account.
- **Requires data-sharing NPCI doesn't fully have today.** Real-time, near-real-time cross-bank data access at this level involves regulatory and privacy considerations that are outside a hackathon team's control to solve.
- **False positives have real cost.** Freezing a genuine account on a wrong flag is a serious user-trust problem, so any real deployment needs a human-in-the-loop review step before a freeze, not full automation.

## 8. Scalability

- The chain-walk only touches the accounts actually involved in a flagged transaction's forward path — it doesn't require scanning the whole network, so it scales with the number of *flagged* transactions, not total UPI volume.
- The proactive risk-scoring pass is the heavier workload, but it's naturally parallelizable — each account's features can be computed independently and only need incremental updates as new transactions arrive, not a full recompute.
- A production version would sit on a proper graph database (Neo4j, TigerGraph, or similar) with streaming ingestion (Kafka/Flink), which are built for exactly this kind of workload at national-payments scale.
- NPCI already operates the infrastructure that sees every UPI transaction — this proposal adds a processing layer on top of an existing data stream rather than requiring new data collection.

## 9. Target Audience

- **Primary:** NPCI's fraud risk team and partner banks' AML/fraud investigation desks.
- **Secondary:** cybercrime law-enforcement units (e.g. the national cybercrime reporting ecosystem), who currently trace chains manually across banks.
- **Indirect beneficiary:** everyday UPI users, whose only real shot at recovering scammed money is a freeze within hours, not weeks.

## 10. Marketing / Go-to-Market Strategy

- **Pilot first:** start with 2-3 partner banks on permissioned data, similar to how RBI's own MuleHunter.AI was piloted, to validate the chain-walk against known past fraud cases before wider rollout.
- **Integrate, don't replace:** plug end-node alerts into banks' existing fraud/CRM systems and the national cybercrime reporting workflow, so a flagged end node turns into a freeze request automatically instead of sitting in a separate dashboard nobody checks.
- **Consumer trust angle:** notify a customer if their account is used as a pass-through — a strong signal their credentials were compromised or they were unknowingly recruited — turning detection into prevention for the next cycle.
- **Positioning line:** existing tools tell you an account *looks* suspicious; this tells you which account *still has the money*.

## 11. Estimated Budget

**For the hackathon build itself:** effectively ₹0 — synthetic data, open-source libraries, and free-tier hosting cover the full 3-hour build.

**Rough order-of-magnitude for a real pilot** (6-month pilot with 2-3 banks, for context if judges ask):

| Item | Estimate |
|---|---|
| Cloud infra (graph DB + streaming pipeline) | ₹8–15 lakh over 6 months |
| Engineering team (4–6 people, 6 months) | ₹40–60 lakh |
| Security/compliance review | ₹5–10 lakh |
| **Rough total** | **₹55–85 lakh (~$65K–100K)** for a 6-month pilot phase |

This is a ballpark for framing scale in a pitch, not a costed proposal — actual figures would depend heavily on NPCI's existing infrastructure reuse and negotiated bank data-sharing terms.

## 12. Impact

Shrinks the time between a fraud being reported and the money being frozen from days of manual, bank-by-bank tracing down to minutes — the single biggest lever on how much money actually gets recovered, since funds typically leave the end account within hours. It also turns the process from purely reactive (waiting for a complaint) to proactive (catching high-velocity chains as they form), and makes running a mule network riskier, since every extra hop is more detection surface, not more safety.
