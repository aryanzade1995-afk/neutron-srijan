# MuleTrace — Mule Account Transaction Chain Detection for NPCI

*Technical brief — transaction chain intelligence for the interbank rail*

---

## One-line idea

When a fraud victim's money enters the UPI network, it doesn't stop at one account — it hops through a chain of "mule" accounts to launder and disperse it before anyone can react. MuleTrace reconstructs that chain from transaction data, walks it hop by hop, and points investigators straight at the **end node** — the account still holding the money — so it can be frozen before cash-out.

---

## 1. Problem

Fraudsters recruit or rent "mule" bank accounts to break the direct link between a scammed victim and themselves. A typical flow looks like:

`Victim → Mule A → Mule B → Mule C → Cash-out (ATM / merchant / crypto off-ramp)`

Each hop happens within minutes, moves most of the received amount onward, and often touches an account with little history and no real financial relationship to the sender. Individually, each transaction looks legitimate — it's only the **chain** that reveals the fraud. Today:

- Each bank only sees its own leg of the chain, never the full path — because a chain routinely crosses two, three, or more banks.
- NPCI sits at the centre of the interbank rail (UPI/IMPS) and is the only party positioned to see the *whole* chain across banks, but investigation today is still largely manual and reactive.
- The real actionable target isn't "which accounts are suspicious" (necessary but not sufficient) — it's **where is the money right now**, i.e. the end node of the chain, because that's the only place funds can still be frozen.

**Problem statement:** *Given interbank transaction data flowing through NPCI's switch, detect mule-account chains formed by rapid, high-percentage, sequential fund transfers, and identify the terminal (end) node of each chain in near real time — before funds are withdrawn or moved beyond recovery.*

---

## 2. Idea

Model the transaction network as a **directed, time-stamped graph**: accounts are nodes, transactions are edges carrying `(amount, timestamp, mode)`. A genuine mule chain is a *temporal path* — each hop must happen causally after the one before it, within a short time window, and forward most of the money received. Walking that path forward from a flagged transaction — respecting time order — surfaces the chain and terminates at the account that stops forwarding money: the end node.

This reframes "mule detection" from *scoring individual accounts in isolation* (what most existing tools, including RBI's MuleHunter.AI pilot, do) to **reconstructing the whole money trail and naming the one account that matters most for recovery.**

---

## 3. Solution

### 3.1 Data model

- **Nodes:** accounts (VPA / bank account), tagged with account age, KYC tier, account type.
- **Edges:** transactions, tagged with `amount`, `timestamp`, `mode` (P2P, P2M, ATM/cash-out).

### 3.2 Core detection logic — temporal chain walk

Implemented in `backend/graph_engine.py`.

1. **Entry point:** a transaction flagged by a victim complaint, a bank's own alert, or (proactively) surfaced by the scan.
2. **Forward walk:** from the receiving account, look for outgoing transactions where:
   - `outgoing.timestamp > incoming.timestamp` — causal ordering; money cannot leave before it arrives
   - the gap is inside a configurable window (default 48 h)
   - the outgoing amount is **between 70% and 125%** of the incoming amount
3. **The upper bound is not decoration.** Without a ceiling the walk hops onto the account's own unrelated, larger outflows and reports impossible forward percentages (an early build displayed "20699% forwarded"). A transfer substantially larger than what arrived is the account's own money, not the victim's moving on.
4. **Candidate ranking:** where several hops qualify, prefer the leg carrying the most money soonest — `0.65 × forward_fraction + 0.35 × speed`.
5. **Split / smurfing detection:** if no single hop qualifies, aggregate everything leaving inside the window. If the total clears the threshold across 2–12 legs, the hop is recorded as a split, the largest leg is followed, and the siblings are retained as evidence. Breaking one hop into five is the cheapest way to duck a percentage threshold, so it is handled explicitly.
6. **Termination:** the walk stops at a cash-out (ATM / untraceable merchant), on a cycle (money returning to an account already on the path), at a hop ceiling (default 10), or where no qualifying hop exists. That account is the **end node**.
7. **Proactive mode:** run the walk continuously across all recent transactions, not just complaint-triggered ones, and surface any chain of length ≥ 3 as a probable active ring — before a complaint is filed.

`hop_count` counts transfers, so it always equals `len(path) - 1`. This was an off-by-one at one point (`len(hops) - 1`), which silently made the proactive scan discard every 3-mule chain; fixing it took scan recall from 86.7% to 100%.

### 3.3 Chain scoring

Each surfaced chain gets a 0–100 recovery priority blending graph evidence with the ML score: hop depth, velocity (median gap between hops), average forward percentage, and value at the end node, combined 60/40 with the mean mule score along the path. Bands: critical ≥ 75, high ≥ 55, medium ≥ 35, low below.

---

## 4. AI Integration

- **Current model:** a Random Forest (300 trees, balanced class weights) over 19 engineered per-account features — inbound→outbound time gap, percentage of received amount forwarded, fan-in/fan-out ratio, distinct counterparties, account age, KYC tier, retained ratio, transactions per active day. A Logistic Regression baseline trains alongside it for comparison.

  | Metric | Random Forest | Logistic baseline |
  |---|---|---|
  | ROC AUC | **0.918** | 0.853 |
  | Average precision | **0.766** | — |
  | Precision / Recall / F1 | 0.667 / 0.633 / **0.650** | 0.453 / 0.717 / 0.555 |

- **On why these are not near 1.0.** An earlier generator opened every mule account fresh, so `account_age_days` separated the classes outright and every metric read 1.0 — an artifact of the data, not a result, and the first thing a reviewer would puncture. Mules are now recruited as real rings recruit them: **35% freshly opened, 50% existing accounts turned (keeping their real age, KYC tier and history), 15% dormant accounts reactivated**. Legitimate high-value P2P was widened to ₹45k–420k so transaction size could not become the next free answer. Account age fell from the top feature (0.233) to fifth (0.086); the model now leans on turnaround speed and amount behaviour. The ensemble also now clearly beats its baseline, which it did not when both trivially saturated.

- **Production-scope vision:** a **temporal Graph Neural Network** (GraphSAGE or a temporal-GNN variant) trained on NPCI's full cross-bank transaction graph. This is the genuine differentiator versus bank-side tools — since NPCI, not any single bank, is the only entity that can see a chain crossing institutions, it is uniquely positioned to train on the *complete* path rather than one bank's fragment.

- **Feedback loop:** confirmed-fraud chains become labelled training data through an append-only verdict log (`backend/feedback.py`). Append-only because a freeze decision is an audit trail: a verdict is superseded by a later entry, never edited in place. Labels are staged for a deliberate retrain rather than applied to the live scorer — a model shifting under an investigator mid-review would make the queue untrustworthy.

---

## 5. Validation

`backend/evaluate.py`, reference dataset: 14,654 transactions, 1,107 accounts, 45 injected chains (17 cashed out).

**Chain walk:** end-node accuracy 100%, full-path recovery 100%, mean hop recall 100%.

Read with the sensitivity sweep, not alone — the chains were generated to the same temporal logic the walk looks for, so the headline is close to circular by itself:

| Walk setting | End-node accuracy |
|---|---|
| strict (85% forwarded, 6 h) | 8.9% |
| tight (80%, 24 h) | 37.8% |
| **default (70%, 48 h)** | **100%** |
| loose (60%, 96 h) | 100% |
| very loose (40%, 168 h) | 88.9% |

The default sits on a plateau and the rule degrades in both directions. Over-loosening is the informative failure: the walk begins admitting unrelated transfers.

**Proactive scan:** 62 chains surfaced, all 45 real ones found — recall 100%, precision 75.8% over the whole set, but **precision@10, @20 and @30 all 100%**, with the first 47 ranked chains genuine. Every false positive ranks below every real chain, so an investigator working top-down never meets one until the queue is cleared.

**Test suite:** 54 tests. `tests/test_chain_walk.py` pins each hop-admission rule on small hand-built graphs — causal ordering, the time window, the forward-percentage floor *and* ceiling, split detection, cycle and hop guards, cash-out termination — because these are the decisions a bank would have to justify. `tests/test_api.py` covers endpoint contracts and the feedback loop; `tests/test_auth.py` covers TOTP, both factors, challenge replay and lockout.

---

## 6. System design

### 6.1 Per-session datasets

The served application does not ship one canned dataset. Every page load issues a fresh session cookie, the cookie seeds a workspace, and every figure the client sees derives from it. Scale varies with the seed (9k–22k transactions, 700–1400 accounts, 28–70 chains), not just contents — otherwise every dashboard would show near-identical totals and read as hardcoded. `?seed=<int>` pins a dataset reproducibly. Workspaces are LRU-cached; building one costs about 1.5 s.

### 6.2 Store

`backend/store.py` wraps Redis and holds three things:

- **Datasets** — a generated network serialised once and reused
- **Detection patterns** — ranked chains in a sorted set keyed by risk score, per-account mule scores in a hash, so "worst chains right now" and "score this account" cost one command instead of a rescan
- **Auth state** — sessions and pending challenges, expiring on their own TTLs

Redis stores and serves what the detection found; the recognition itself stays in the chain walk and the classifier. If no Redis answers, an in-process backend with the same interface takes over, and which one is live is reported on `/api/health` — a silent fallback would be worse than none.

### 6.3 Access control

Password (scrypt, per-user salt) plus TOTP (RFC 6238), enforced by one middleware in front of every data route. The first factor issues only a short-lived challenge; only the second mints a session. Challenges are single-use, failures throttle at five per five minutes, and the enrolment secret is returned exactly once. Ships disabled so a live demo cannot hit a login wall; `MULETRACE_AUTH=on` enables it.

---

## 7. Investigator console

- **End-node banner** — the account still holding the money, with amount, hop count, elapsed time, skim percentage and mule score.
- **Stat row** — describes the chain currently open (funds at end node, duration, hops, recovery priority), with the queue-wide figure kept in each sub-line for context.
- **Money trail** — force-directed graph, victim red, mule hops amber, end node mint, cash-out grey; edges labelled with amount, gap and forward percentage.
- **Hop ledger** — every hop with the numbers that admitted it.
- **Freeze-request report** — a ten-section document generated from the open chain: request details, target account, transaction details, basis for freeze, fraud indicators (emitted only where the data supports them), money trail, hop ledger, requested action, evidence, and a regulatory notice stating this is an operational fraud-intervention request and **not** an STR filing under the PMLA.

Two integrity rules in that report:

1. Fields the feed does not carry — account number, IFSC, holder name, RRN/UTR — are shown as *not carried in this feed*, never filled with plausible-looking values. A fabricated identifier on a freeze request is worse than a visible gap.
2. On submission it states *"Request sent — awaiting bank acknowledgement… no freeze is in force until the bank confirms."* It never claims the account has been frozen.

Cash-out chains keep a guard: the report opens as an evidence pack for law-enforcement escalation with submission disabled, because a freeze cannot recover funds that left the banking channel.

---

## 8. Target audience

- **Primary:** NPCI's fraud risk management team and partner banks' AML/fraud investigation desks — the direct users who'd act on a flagged end node.
- **Secondary:** law-enforcement cybercrime units (I4C / the National Cybercrime Reporting Portal ecosystem), who currently stitch records together across banks manually.
- **Indirect beneficiary:** everyday UPI users, whose scammed funds have a real chance of recovery only if frozen within hours.

---

## 9. Rollout

- **Phase 1 — Pilot:** mirror the model NPCI/RBI Innovation Hub used for MuleHunter.AI — pilot with 2–3 partner banks on permissioned data, validating chain-walk precision and recall against known fraud cases.
- **Phase 2 — Integration:** plug end-node alerts into banks' existing fraud/CRM systems and the national cybercrime reporting workflow, so a flagged end node auto-generates a freeze request.
- **Phase 3 — Awareness:** banks notify a customer whose account was *used as a pass-through* — a strong signal their credentials were compromised or they were recruited unknowingly — turning detection into prevention for the next cycle.
- **Positioning:** "MuleHunter.AI tells you an account looks suspicious. MuleTrace tells you exactly which account still has the money."

---

## 10. Impact

- Shrinks the time between "fraud reported" and "funds frozen" from days of manual, bank-by-bank tracing to minutes — the single biggest lever on money actually recovered, since funds typically leave the terminal account within hours.
- Turns a reactive, complaint-driven process proactive: on the reference dataset the scan surfaces 100% of injected chains with no complaint filed.
- Scales with the number of *flagged* transactions rather than total UPI volume, because the walk touches only the accounts on a chain's forward path.
- Makes mule recruitment a worse trade for fraud rings — every additional hop adds detection surface rather than additional safety.

---

## Notes on positioning against existing efforts (for Q&A)

RBI Innovation Hub has piloted **MuleHunter.AI** with public-sector banks — it flags individual accounts as likely mules using ML on transaction/account data, and NPCI has begun piloting AI-based risk scoring shared across banks to catch rapid multi-hop fund movement. Be ready for "isn't this already being done?" — the honest answer: yes, mule *account* scoring exists; what's less built out is automated **end-to-end chain reconstruction that names the current end node for recovery**, which is the specific gap this targets.

Expect the follow-up "how do we know your numbers aren't tuned?" — the sensitivity sweep is the answer. Thresholds are swept rather than fitted, and both over-tightening and over-loosening are shown to cost accuracy.

---

### Sources

- [Reserve Bank of India pilots new MuleHunter.AI solution to help identify mule accounts](https://www.fintechfutures.com/ai-in-fintech/reserve-bank-of-india-pilots-new-mulehunter-ai-solution-to-help-identify-mule-accounts)
- [RBI Launches AI-Powered Model, MuleHunter.AI, to Combat Mule Bank Accounts and Financial Fraud](https://vajiramandravi.com/current-affairs/rbi-launches-ai-powered-model-mulehunter-ai/)
- [Worried About UPI Fraud By Scamsters? Here's How NPCI Will Use AI To Take On Cheats](https://www.newsx.com/business/worried-about-upi-fraud-by-scamsters-heres-how-npci-will-use-ai-to-take-on-cheats-242854/)
- [15 more banks to adopt RBI's MuleHunter fraud detection tool by October](https://www.business-standard.com/industry/banking/15-more-banks-to-adopt-rbi-s-mulehunter-fraud-detection-tool-by-october-125080101845_1.html)
