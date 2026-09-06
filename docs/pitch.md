# MuleTrace — Mule Account Chain Detection for UPI/NPCI

*Fraud Detection — transaction chain intelligence for the interbank rail*

---

## 1. Problem

Right now if someone gets scammed over UPI, their money doesn't just sit in one account — it gets moved through 3-4 different "mule" accounts really fast before anyone can react, and by the time a complaint is filed the money's usually already gone. No single bank can see the full chain since it crosses multiple banks, but NPCI can, because every UPI transaction passes through it. Investigation today is still manual and reactive: it starts only after a complaint, and by then the money has usually already been withdrawn.

## 2. Proposed Solution

Treat all accounts and transactions like a graph — accounts are nodes, transactions are edges with a time and amount. Once one transaction gets flagged, trace forward through time to see where that specific money went next, hop by hop, until it stops moving. Wherever it stops is the account still holding the money — that's the one that actually needs to be frozen, not just any account that looks suspicious.

The core rule: an outgoing transfer only counts as "the same chain" if it happens *after* the money arrived, within a short time window, and moves most of the amount forward — **but not substantially more than arrived**. That upper bound matters as much as the lower one: without it the walk hops onto the account's own unrelated larger transfers and reports impossible forward percentages.

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
Pattern store (Redis: ranked chains, per-account scores, cached traces)
        │
        ▼
Alerting layer (ranked list of active chains + end-node recommendation)
        │
        ▼
Console (investigator view: graph visual, hop ledger, freeze request report)
        │
        ▼
Feedback loop (confirmed / dismissed verdicts → labels for the next retrain)
```

Two things run side by side on the same graph: the **chain-walk** (reactive — given a flagged transaction, find the end node) and the **risk score** (proactive — flags accounts behaving like mules before any complaint exists).

## 4. Workflow

1. A transaction happens and lands in the feed (in the demo: a generated synthetic network; in production: NPCI's real-time switch data).
2. The graph engine adds it as an edge between two account-nodes.
3. A trigger fires — either a victim complaint pointing at a specific transaction, or the risk-scoring module flagging an account on its own.
4. The chain-walk module follows outgoing edges from that account forward in time, only continuing where the next hop happens after the money arrived and forwards most of it.
5. The walk stops naturally when no more qualifying hops exist, at a cash-out, or on a cycle — that account is the end node.
6. The console surfaces the full chain (victim → mules → end node) with a risk score, and a freeze-request report carrying every hop and the numbers that admitted it.
7. Action taken outside the system (bank/law enforcement freezes the account) gets logged back as a verdict, and confirmed cases become labelled training data.

## 5. Usage of AI / AI Integration

- **In the current build:** a Random Forest over 19 engineered per-account features — how fast money leaves after it arrives, what percentage gets forwarded, fan-in/fan-out, distinct counterparties, account age, KYC tier — producing a mule-probability score alongside the graph trace. A Logistic Regression baseline is trained at the same time for comparison.
- **Measured performance** (reference dataset, 45 injected chains):

  | Metric | Random Forest | Logistic baseline |
  |---|---|---|
  | ROC AUC | **0.918** | 0.853 |
  | Average precision | **0.766** | — |
  | Precision / Recall / F1 | 0.667 / 0.633 / **0.650** | 0.453 / 0.717 / 0.555 |

- **Why these are not higher, deliberately.** An earlier version of the data generator opened every mule account fresh, so `account_age_days` separated the classes outright and every metric read 1.0 — an artifact of the data, not a result. Mules are now recruited the way real rings recruit them: **35% freshly opened, 50% existing accounts turned, 15% dormant accounts reactivated**, so most arrive with an ordinary account age and a genuine transaction history. Legitimate high-value P2P was widened to overlap the fraud amount range for the same reason. Account age fell from the top feature (0.233) to fifth (0.086); the model now leans on turnaround speed and amount behaviour.
- **Division of labour.** The chain-walk logic stays rule-based and deterministic even at scale — it's the *scoring* that gets smarter with AI, not the traversal, which needs to stay explainable enough for a bank to act on. Every hop carries the gap, forward percentage and amount that admitted it.
- **Where this goes in a real version:** a temporal Graph Neural Network trained on NPCI's full cross-bank graph, since NPCI is the only party positioned to see a complete chain rather than one bank's fragment of it.
- **Feedback loop:** confirmed fraud cases become labelled training data through an append-only verdict log, so the model improves the more it's used.

## 6. Tech Stack

| Layer | Tool |
|---|---|
| Dataset | Python generator — synthetic network with labelled fraud chains, deterministic per seed |
| Graph engine | `networkx` (in-memory temporal graph) |
| Backend / API | FastAPI + uvicorn |
| ML scoring | `scikit-learn` (Random Forest, Logistic Regression baseline) |
| Store | Redis — ranked chains (sorted set), per-account scores (hash), sessions, cached datasets |
| Access control | Password + TOTP (RFC 6238), scrypt hashing, session middleware |
| Frontend | Vanilla HTML/CSS/JS, `vis-network` force-directed graph |
| Tests | `pytest` — 54 tests |

Everything here is free/open-source.

## 7. Results

Measured by `backend/evaluate.py` against 45 injected chains with known ground truth (14,654 transactions, 1,107 accounts). Reproducible with one command.

**Chain walk — given a flagged transaction, find the account still holding the money:**

| Metric | Value |
|---|---|
| End-node accuracy | 100% |
| Full-path recovery | 100% |
| Mean hop recall | 100% |

That figure should be read together with the sensitivity sweep below, not on its own. These chains were generated to the same temporal logic the walk looks for, so the headline alone is close to circular. The sweep shows where the rule actually degrades:

| Walk setting | End-node accuracy |
|---|---|
| strict (85% forwarded, 6 h) | 8.9% |
| tight (80%, 24 h) | 37.8% |
| **default (70%, 48 h)** | **100%** |
| loose (60%, 96 h) | 100% |
| very loose (40%, 168 h) | 88.9% |

The default sits on a genuine plateau, and the rule degrades in *both* directions. Over-tightening misses real hops; over-loosening is the more interesting failure — the walk starts admitting unrelated transfers and wanders off the money trail.

**Proactive scan — surfacing chains with no complaint filed:**

| Metric | Value |
|---|---|
| Recall vs. injected chains | 100% |
| Precision @ 10 / @ 20 / @ 30 | 100% / 100% / 100% |
| Precision over the whole surfaced set | 75.8% |

The gap between those last two rows is the point. All 45 real chains rank above every false positive, so an investigator working the queue top-down clears every genuine chain before meeting a single false alarm. The 75.8% is what you get by counting a tail nobody needs to work.

## 8. Limitations

- **No real data.** The demo runs on synthetic networks with injected fraud chains — real UPI transaction data isn't accessible outside NPCI/banks, so the figures above demonstrate that the traversal and scoring work as specified. They are not real-world precision and recall.
- **What transfers and what doesn't.** The chain walk is deterministic and rule-based and transfers directly. The classifier is a *ranking aid* trained on synthetic behaviour and would need retraining on NPCI's real graph — which is exactly what the feedback loop accumulates labels for.
- **Evasion is possible.** A sophisticated ring can break the "fast + high-percentage" pattern deliberately — hold money for days, or cash out and redeposit through informal channels. Splitting a hop into several smaller legs is already handled: the walk aggregates everything leaving inside the window, so structuring does not duck the threshold on its own.
- **Cash-out is a dead end for tracing.** Once money leaves via ATM withdrawal or an untraceable merchant, the graph has nothing more to walk. The console handles this explicitly — such chains are presented as evidence packs for law-enforcement escalation, not freeze requests, because a freeze cannot recover funds that left the banking channel.
- **Requires data-sharing NPCI doesn't fully have today.** Real-time cross-bank data access at this level involves regulatory and privacy considerations outside a project team's control.
- **False positives have real cost.** Freezing a genuine account on a wrong flag is a serious user-trust problem, so any real deployment needs a human-in-the-loop review step before a freeze, not full automation. The system reflects this: it raises a *request* and states that no freeze is in force until the bank confirms.

## 9. Scalability

- The chain-walk only touches the accounts actually involved in a flagged transaction's forward path — it doesn't require scanning the whole network, so it scales with the number of *flagged* transactions, not total UPI volume.
- The proactive risk-scoring pass is the heavier workload, but it's naturally parallelisable — each account's features are computed independently and need only incremental updates as new transactions arrive.
- Detection output is published to Redis as it is produced: ranked chains in a sorted set keyed by risk score, per-account mule scores in a hash. "Worst chains right now" and "score this account" then cost one command rather than a rescan.
- A production version would sit on a proper graph database (Neo4j, TigerGraph) with streaming ingestion (Kafka/Flink), which are built for exactly this workload at national-payments scale.
- NPCI already operates the infrastructure that sees every UPI transaction — this proposal adds a processing layer on top of an existing data stream rather than requiring new data collection.

## 10. Security and Privacy

- The console exposes victim VPAs, account ages and freeze recommendations, so the API is closed by default behind a single authentication gate rather than per-route checks.
- **Multi-factor:** password (scrypt, per-user salt) plus a time-based one-time code (TOTP, RFC 6238). The first factor issues only a short-lived challenge; only the second mints a session. Challenges are single-use, failed attempts are throttled, and the enrolment secret is shown exactly once.
- Sessions and pending challenges live in Redis with TTLs, so they expire on their own.
- The freeze-request report deliberately does **not** fabricate fields the feed doesn't carry — account number, IFSC, holder name, RRN/UTR are shown as unavailable rather than filled with plausible-looking values. A fabricated identifier on a freeze request is worse than a visible gap.

## 11. Target Audience

- **Primary:** NPCI's fraud risk team and partner banks' AML/fraud investigation desks.
- **Secondary:** cybercrime law-enforcement units, who currently trace chains manually across banks.
- **Indirect beneficiary:** everyday UPI users, whose only real shot at recovering scammed money is a freeze within hours, not weeks.

## 12. Go-to-Market Strategy

- **Pilot first:** start with 2-3 partner banks on permissioned data, similar to how RBI's own MuleHunter.AI was piloted, to validate the chain-walk against known past fraud cases before wider rollout.
- **Integrate, don't replace:** plug end-node alerts into banks' existing fraud/CRM systems and the national cybercrime reporting workflow, so a flagged end node turns into a freeze request automatically instead of sitting in a separate dashboard nobody checks.
- **Consumer trust angle:** notify a customer if their account is used as a pass-through — a strong signal their credentials were compromised or they were unknowingly recruited — turning detection into prevention for the next cycle.
- **Positioning line:** existing tools tell you an account *looks* suspicious; this tells you which account *still has the money*.

## 13. Estimated Budget

**For the current build:** effectively ₹0 — synthetic data, open-source libraries, free-tier hosting.

**Rough order-of-magnitude for a real pilot** (6-month pilot with 2-3 banks):

| Item | Estimate |
|---|---|
| Cloud infra (graph DB + streaming pipeline) | ₹8–15 lakh over 6 months |
| Engineering team (4–6 people, 6 months) | ₹40–60 lakh |
| Security/compliance review | ₹5–10 lakh |
| **Rough total** | **₹55–85 lakh (~$65K–100K)** for a 6-month pilot phase |

This is a ballpark for framing scale, not a costed proposal — actual figures would depend heavily on NPCI's existing infrastructure reuse and negotiated bank data-sharing terms.

## 14. Impact

Shrinks the time between a fraud being reported and the money being frozen from days of manual, bank-by-bank tracing down to minutes — the single biggest lever on how much money actually gets recovered, since funds typically leave the end account within hours. It also turns the process from purely reactive (waiting for a complaint) to proactive: on the reference dataset the scan surfaces **100% of injected chains with no complaint filed**, and the first 47 ranked chains are all genuine. And it makes running a mule network riskier, since every extra hop is more detection surface, not more safety.
