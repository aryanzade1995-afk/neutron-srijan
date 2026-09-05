# MuleTrace — Mule Account Transaction Chain Detection for NPCI

*Domain: Fraud Detection | Srijan 26 (GH Raisoni College) — 3-hour build*

*(Working title — rename freely for the pitch deck)*

---

## One-line idea

When a fraud victim's money enters the UPI network, it doesn't stop at one account — it hops through a chain of "mule" accounts to launder and disperse it before anyone can react. MuleTrace reconstructs that chain in real time from transaction data, walks it hop by hop, and points investigators straight at the **end node** — the account still holding the money — so it can be frozen before cash-out.

---

## 1. Problem

Fraudsters recruit or rent "mule" bank accounts to break the direct link between a scammed victim and themselves. A typical flow looks like:

`Victim → Mule A → Mule B → Mule C → Cash-out (ATM / merchant / crypto off-ramp)`

Each hop happens within minutes, moves 80–100% of the received amount onward, and touches an account with little history and no real financial relationship to the sender. Individually, each transaction looks legitimate — it's only the **chain** that reveals the fraud. Today:

- Each bank only sees its own leg of the chain (its own customer's inflow or outflow), never the full path — because a chain routinely crosses two, three, or more banks.
- NPCI sits at the center of the interbank rail (UPI/IMPS) and is the only party positioned to see the *whole* chain across banks, but investigation today is still largely manual and reactive — it starts only after a victim complaint, by which point funds have often already cashed out.
- The real actionable target isn't "which accounts are suspicious" (that's necessary but not sufficient) — it's **where is the money right now**, i.e., the end node of the chain, because that's the only place funds can still be frozen.

**Problem statement:** *Given interbank transaction data flowing through NPCI's switch, detect mule-account chains formed by rapid, high-percentage, sequential fund transfers, and identify the terminal (end) node of each chain in near real time — before funds are withdrawn or moved beyond recovery.*

---

## 2. Idea

Model the transaction network as a **directed, time-stamped graph**: accounts are nodes, transactions are edges carrying `(amount, timestamp)`. A genuine mule chain is a *temporal path* — each hop must happen causally after the one before it, within a short time window, and forward most of the money received. Walking that path forward from a flagged/victim transaction — respecting time order — surfaces the chain and terminates naturally at the account that stops forwarding money: the end node.

This reframes "mule detection" from *scoring individual accounts in isolation* (what most existing tools, including RBI's own MuleHunter.AI pilot, do) to **reconstructing the whole money trail and naming the one account that matters most for recovery.**

---

## 3. Solution

### 3.1 Data model

- **Nodes:** accounts (VPA / bank account), tagged with lightweight features — account age, KYC tier, average daily transaction count/value.
- **Edges:** transactions, tagged with `amount`, `timestamp`, `mode` (P2P, P2M, cash-out/ATM).

### 3.2 Core detection logic — temporal chain walk

1. **Entry point:** a transaction flagged by a victim complaint, a bank's own alert, or (proactively) any transaction into an account with mule-like traits.
2. **Forward walk:** from the receiving account, look for its *next* outgoing transaction(s) where:
   - `outgoing.timestamp > incoming.timestamp` (causal ordering — money can't leave before it arrives),
   - the gap is short (e.g. under a configurable 24–48 hr window — mules move money fast),
   - the outgoing amount is a large fraction of the incoming amount (e.g. ≥ 70%, allowing for a small cut/fee).
3. **Repeat** hop by hop, building the path. A hop count guard (e.g. max 10) prevents infinite loops on cyclic/adversarial data.
4. **Terminate** when no qualifying outgoing transaction exists within the window, or the trail hits a cash-out (ATM withdrawal / P2M to an untraceable merchant). That account is the **end node** — flagged as highest-priority for a freeze request, since it's the last place the money is confirmed to sit.
5. **Ring detection (proactive mode):** run the walk continuously over the recent transaction window across *all* accounts, not just complaint-triggered ones, and surface any chain of length ≥ 3 with high velocity + high forward-percentage as a probable active ring — before a complaint is even filed.

### 3.3 3-hour build plan (what's actually feasible in the window)

| Time | Task |
|---|---|
| Hour 1 | Generate a synthetic transaction dataset (Python script: normal accounts + injected fraud chains of 3–6 hops with realistic amount decay and time gaps) → load into an in-memory graph (`networkx`) |
| Hour 2 | Implement the temporal chain-walk function + a simple per-account risk score (heuristic or a quick `scikit-learn` classifier trained on the synthetic labels) → wrap in a small Flask/FastAPI backend with 2 endpoints: `/trace/<txn_id>` and `/risk-score/<account_id>` |
| Hour 3 | Frontend: a single page (vis.js or D3 force-directed graph) that visualizes the traced chain — victim in red, intermediate mules in orange, end node highlighted in black with a "Freeze recommended" tag — plus a ranked dashboard of live suspicious chains. Polish + rehearse the pitch. |

Since real NPCI/UPI data isn't accessible for a hackathon, a synthetic dataset with clearly labeled "ground truth" chains is what makes the demo both buildable in 3 hours and honestly presentable — say this openly in the pitch rather than implying real data.

---

## 4. AI Integration

- **Demo-scope model:** a lightweight classifier (Logistic Regression / Random Forest) trained on engineered per-account features — inbound→outbound time gap, percentage of received amount forwarded, fan-in/fan-out ratio, account age, number of distinct counterparties — outputs a mule-probability score that ranks nodes on the dashboard alongside the graph trace.
- **Production-scope vision (the pitch-worthy part):** a **temporal Graph Neural Network** (e.g., GraphSAGE or a temporal-GNN variant) trained on NPCI's full cross-bank transaction graph. This is the genuine differentiator versus bank-side tools like MuleHunter.AI — since NPCI, not any single bank, is the only entity that can see a chain that crosses institutions, it's uniquely positioned to train a model on the *complete* path rather than one bank's fragment of it.
- Feedback loop: confirmed-fraud chains (post law-enforcement action) get fed back as labeled training data, so the model improves as it's used — mirroring how RBI's own MuleHunter.AI pilot is being iterated with partner banks.

---

## 5. Target audience

- **Primary:** NPCI's fraud risk management team and partner banks' AML/fraud investigation desks — the direct users who'd act on a flagged end node.
- **Secondary:** law-enforcement cybercrime units (e.g. I4C / the National Cybercrime Reporting Portal ecosystem), who currently have to manually request and stitch together records across multiple banks to trace a single complaint.
- **Indirect beneficiary:** everyday UPI users, whose scammed funds have a real chance of recovery only if frozen within hours, not weeks.

---

## 6. Campaign (rollout & adoption angle)

- **Phase 1 — Pilot:** mirror the model NPCI/RBI Innovation Hub already used for MuleHunter.AI — pilot with 2–3 partner banks on real (permissioned) transaction data, validating chain-walk precision/recall against known fraud cases before wider rollout.
- **Phase 2 — Integration:** plug end-node alerts directly into banks' existing fraud/CRM systems and into the national cybercrime reporting workflow, so a flagged end node auto-generates a freeze request instead of sitting in a dashboard.
- **Phase 3 — Awareness:** a consumer-facing angle — banks notify a customer if their account is *used as a pass-through* (a strong signal their KYC/credentials were compromised or they were recruited unknowingly), turning detection into prevention for the next cycle.
- **Positioning:** "MuleHunter.AI tells you an account looks suspicious. MuleTrace tells you exactly which account still has the money."

---

## 7. Impact

- Shrinks the time between "fraud reported" and "funds frozen" from days (manual, bank-by-bank tracing) to minutes — the single biggest lever on actual money recovered, since funds typically leave the terminal account within hours.
- Turns a reactive, complaint-driven process into a proactive one by surfacing high-velocity chains before a complaint is even filed.
- Scales naturally with UPI's transaction volume, since the underlying logic (temporal graph traversal) is exactly the kind of workload NPCI's central position and infrastructure are built to run across billions of monthly transactions.
- Makes mule recruitment a worse trade for fraud rings — every additional hop adds detection surface rather than additional safety.

---

## Notes on positioning against existing efforts (for Q&A)

RBI Innovation Hub has already piloted **MuleHunter.AI** with public-sector banks — it flags individual accounts as likely mules using ML on transaction/account data, and NPCI itself has begun piloting AI-based risk scoring shared across banks to catch rapid multi-hop fund movement. Be ready for a judge to ask "isn't this already being done?" — the honest answer: yes, mule *account* scoring exists; what's less built out is automated **end-to-end chain reconstruction that names the current end node for recovery**, which is the specific gap this project targets.

---

### Sources

- [Reserve Bank of India pilots new MuleHunter.AI solution to help identify mule accounts](https://www.fintechfutures.com/ai-in-fintech/reserve-bank-of-india-pilots-new-mulehunter-ai-solution-to-help-identify-mule-accounts)
- [RBI Launches AI-Powered Model, MuleHunter.AI, to Combat Mule Bank Accounts and Financial Fraud](https://vajiramandravi.com/current-affairs/rbi-launches-ai-powered-model-mulehunter-ai/)
- [Worried About UPI Fraud By Scamsters? Here's How NPCI Will Use AI To Take On Cheats](https://www.newsx.com/business/worried-about-upi-fraud-by-scamsters-heres-how-npci-will-use-ai-to-take-on-cheats-242854/)
- [15 more banks to adopt RBI's MuleHunter fraud detection tool by October](https://www.business-standard.com/industry/banking/15-more-banks-to-adopt-rbi-s-mulehunter-fraud-detection-tool-by-october-125080101845_1.html)
