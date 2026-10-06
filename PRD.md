# GESIT: Product Requirements Document

Intelligent Supplier Selection and Procurement Disruption Management.
Agentic AI Hackathon 2026, Track A: Intelligent Supply Chain (Sokrates with AWS, SAP, APP Group).

| | |
|---|---|
| Status | Building toward Final/Demo Day, **Saturday 31 October 2026** (in person, 15 min incl. Q&A, **English**) |
| Last updated | 6 October 2026 |
| Feature freeze | 25 October 2026 |
| Source documents | Accepted proposal, finalist work plan (feedback), participant briefing deck |

When a requirement changes, update this file in the same change and add a line to the Changelog at the end.

---

## 1. Problem

An Indonesian F&B manufacturer buys raw materials (sugar first; cooking oil and packaging film later) from approved suppliers. When the main supplier is late, raises prices or runs out of capacity, procurement has to notice it, collect backup quotes and trade price against lead time, reliability and capacity, usually from memory or old spreadsheets. That takes hours to days while stock runs down, and a stock-out stops the production line.

Supplier choice can also be bent by conflicts of interest (kickbacks, personal relationships), so the best-value supplier does not always win.

Figures used in the proposal (cite them as such in the deck): direct procurement disruptions cost about USD 16 million per year on average; over ten years disruptions erase about 45% of one year's profit; disruptions longer than a month happen about every 3.7 years. SAP's own Bid Analysis Agent in Ariba shows the problem is real.

## 2. Goals and non-goals

### Goals (what the demo must prove)
1. **Predict** which open POs are likely to be late (SAP-RPT), not only react after they are late.
2. **Decide**: rank backup suppliers by expected total cost (price plus the cost of downtime risk) and explain the trade-off in plain English.
3. **Act** with guardrails: draft the replacement PO; anything above the approval threshold or off the approved panel is blocked by policy and routed to a manager.
4. **Guard integrity**: when a buyer overrides the recommendation, require a written justification, log it and flag suspicious buyer and supplier patterns.
5. **Learn and prove value**: update supplier reliability after each goods receipt, and show with simulation that GESIT beats "always cheapest" and "always fastest".

### Non-goals (do not build these)
- A live connection to a real S/4HANA or Ariba system. The public S/4HANA Cloud sandbox has returned HTTP 401 since about 1 Sep 2026. Data is S/4-shaped and sits behind an adapter instead.
- Multi-plant, multi-currency, contract management, invoicing, payments.
- More than one material in the demo (the design must not block adding more).
- Fully autonomous PO release. A human always approves above the threshold.

## 3. Users

| Persona | Needs | Main screen |
|---|---|---|
| Buyer (e.g. BUYER01) | See at-risk POs, get a recommended replacement, approve in one click or override with a reason | Buyer view |
| Procurement manager | Approve POs above the threshold, see integrity flags | Manager view |
| Auditor | Trace why each decision was made | Audit trail (Observability traces + decision log) |
| Judges | See Plan, Reason, Act/Tool, Review, Orchestration, Human-in-the-loop, Responsible AI | The 6-minute live demo |

## 4. Judging criteria and how GESIT answers them

| Criterion | Weight | GESIT answer |
|---|---|---|
| Innovation & Originality | 25% | Integrity Guard; predictive detection with SAP-RPT; risk-priced supplier choice proven against baselines |
| Working Solution & Demo | 20% | Live end-to-end run on 3 scenarios, backup video ready |
| Technical Execution & AWS/SAP | 20% | Strands on AgentCore Runtime, Gateway (MCP), Policy (Cedar), Memory, Observability; SAP-RPT on AI Core; Gen AI Hub with masking and grounding |
| Problem-Solution Fit & Impact | 20% | Response time hours to minutes; cost vs baselines from Monte Carlo |
| Presentation & Storytelling | 10% | One concrete story (sugar is late, line stops), one architecture diagram |
| Q&A | 5% | Prepared answers (section 15) |

| Agentic aspect | Where it shows |
|---|---|
| Plan | Supervisor lays out the steps when a disruption is detected |
| Reason | Expected-cost scoring plus LLM explanation of the trade-off |
| Act / Tool | Tools via AgentCore Gateway (MCP): query POs, SAP-RPT prediction, draft PO |
| Review | Evaluation vs baselines, Integrity Guard |
| Orchestration | Strands supervisor on AgentCore Runtime |
| Human-in-the-loop | AgentCore Policy: approval threshold and approved panel |
| Responsible AI | SAP data masking, grounding on contracts, audit trail, justified overrides |

## 5. Architecture

```
Streamlit UI (buyer / manager)
        |
AgentCore Runtime: Strands Supervisor (agents-as-tools / Graph, deterministic path)
   |-- Disruption Detection agent ----> predict_delay_risk --> SAP-RPT (AI Core)
   |-- Supplier Evaluation agent  ----> score_suppliers (Python) + explain --> Gen AI Hub (masking, grounding)
   |-- Action agent               ----> draft_po / submit_for_approval / release_po
   |-- Evaluation agent           ----> record_outcome, reliability update, baselines
   |-- Integrity Guard            ----> record_override, integrity flags
        |
AgentCore Gateway (MCP tools = Lambda) + AgentCore Policy (Cedar, every tool call)
        |
DynamoDB (S/4-shaped tables, decisions, overrides)   S3 (contracts, exports)
AgentCore Memory (session state)   AgentCore Observability (traces)   Secrets Manager (SAP keys)
```

### Tech stack (one job each)

| Layer | Choice | Job |
|---|---|---|
| Dev | Kiro (AWS Docs + Strands MCP), Claude Code | Specs, code |
| Orchestration | Strands Agents, supervisor | Plan and delegate |
| Agent LLM | A Claude model on Amazon Bedrock with strong tool use (pick in week 1, then freeze) | Reasoning loop |
| Hosting | AgentCore Runtime | Run the agents |
| Tools | AgentCore Gateway + Lambda (MCP) | Data access and actions |
| Guardrails | AgentCore Policy (Cedar) | Threshold and panel rules |
| State, audit | AgentCore Memory, AgentCore Observability, DynamoDB, S3 | Session state, traces, records |
| Prediction | SAP-RPT-1.6 (sap-rpt-1-small) on SAP AI Core | P(late) per PO line |
| Explanations | SAP Gen AI Hub orchestration (sap-ai-sdk-gen) | Masked, contract-grounded text for the buyer |
| UI | Streamlit | Buyer and manager views |
| Region | us-west-2 only | Avoid forgotten resources |

Why two LLM paths: Bedrock runs the agent loop (native to Strands/AgentCore); Gen AI Hub writes the human-facing explanation that contains buyer and supplier names, so masking applies there.

Optional, only if time allows: Joule Studio front end (needs SAP BAIP sub-account); CAP OData mock via SAP Build Code.

## 6. Data

Built by the generator in `data_gen/` (see `data_gen/README.md`). Prices are anchored on **PIHPS (Bank Indonesia), market type Pedagang Besar (wholesale), series "Gula Pasir Lokal"**; supplier behaviour is simulated from hidden parameters.

| Table / file | Content |
|---|---|
| LFA1 | 10 fictional suppliers (LIFNR 100101..100110) |
| MAKT | Material RM-GULA-001, Gula Pasir Lokal (GKP) |
| EKKO / EKPO / EKET / EKBE | PO header (BEDAT, ERNAM, LIFNR), item (MENGE, NETPR), promised date (EINDT), goods receipts (BUDAT) |
| market_price_daily | PIHPS anchor used (public data, the agent may use it) |
| quotes_asof | Current quote, lead time, spare capacity per supplier |
| inventory_asof | Stock, daily usage (3,300 kg), safety stock, days of cover, downtime cost |
| scenarios.json | S1 delay notice, S2 price increase, S3 capacity shortfall |
| rpt_po_lines | Features known at order time + label LATE / DELAY_DAYS, SPLIT train/test/holdout_recent/predict |
| `_truth/` | Hidden supplier parameters, buyer bias, outcomes of open POs |

Hard rules:
- **D-1** Agent code, tools and UI must never read `data_gen/out/_truth/` or import `data_gen/world.py`. Only `eval/` may use them.
- **D-2** Keep SAP field names (LIFNR, EBELN, BEDAT, EINDT, NETPR, MENGE, ...) end to end, so "connect to real S/4" means swapping the adapter.
- **D-3** Every number in the deck that comes from simulated data is labelled "simulated". RPT accuracy is reported as "on simulated data vs a classical baseline", never as real-world accuracy.
- **D-4** Assumptions (supplier parameters, downtime cost Rp150 juta/day, GKP not GKR, Idul Fitri dates) live in `data_gen/config.py` and on one assumptions slide.

### Dataset status (generated 6 Oct 2026)

- **Price anchor (real):** downloaded with `fetch_pihps.py` from the PIHPS website endpoint, 2 Sep 2024 to 30 Sep 2026, 543 daily observations of "Gula Pasir Lokal", Rp16,250 to Rp17,600 per kg. Stored in `data/pihps/pihps_long.csv` plus the raw monthly JSON in `data/pihps/raw/`.
- **Market type:** `price_type_id=3` was used. It is inferred to be Pedagang Besar because its prices sit between the retail series (ids 1, 2) and the producer series (id 4). Not yet confirmed against the website table (see section 17).
- **Simulated output:** 434 PO lines (429 closed, 5 open), 10 suppliers, 4 buyers, seed 42, history 2024-10-01 to AS_OF 2026-09-30. `rpt_po_lines` split: 340 train, 74 test, 15 holdout_recent, 5 predict. Two runs with the same seed and PIHPS file gave byte-identical files.
- **Validation (simulated data, `validation_report.md`):** monthly PO price vs anchor correlation 0.956; BUYER03 sends 24% of its POs to 100107 vs 3 to 5% for the other buyers; rainy season late rate 30% vs 19% otherwise; logistic regression AUC 0.694, gradient boosting AUC 0.603.
- **Known gaps against this PRD** (each is an open decision in section 17):
  - S2 price increase came out at +2.7% (Rp18,000 to Rp18,500), below the 3% trigger in FR-DET-2.
  - Supplier 100108 drift is weak in the observed data: late rate 11% before March 2026, 15% after.
  - Lebaran effect is not visible (17% late inside the window, n=18, vs 24% outside); supplier 100109 has only 5 POs.
- **Location:** the generator files currently sit at the repository root and write to `out/`, not `data_gen/` and `data_gen/out/` as section 13 proposes. Rule D-1 applies to `out/_truth/` and `world.py` in the meantime.
- **Running it on Windows:** `python` is not on PATH on the dev machine; use `py -3.11 generate.py` and `py -3.11 validate.py`.

## 7. Functional requirements

IDs are referenced in code comments, tests and commits.

### 7.1 Supervisor (FR-SUP)
- **FR-SUP-1** Given a trigger (scheduled scan or a supplier notice), produce a short plan (steps and which agent runs each) and log it.
- **FR-SUP-2** Run Detection, then Evaluation, then Action, then Integrity/Evaluation as needed; stop and hand back to the human when policy denies an action.
- **FR-SUP-3** Deterministic path (agents-as-tools or Graph). No Swarm.

### 7.2 Disruption Detection (FR-DET)
- **FR-DET-1** For every open PO, call `predict_delay_risk` (SAP-RPT) with the PO's feature row and context rows from history; return P(late).
- **FR-DET-2** Flag a PO when any of: P(late) >= 0.5; a supplier delay notice; a price revision > 3% over the PO price; a capacity shortfall notice.
- **FR-DET-3** For each flag compute stock-out date = today + days of cover, and the gap between the (new) expected arrival and the stock-out date.
- **FR-DET-4** If SAP-RPT is unavailable, fall back to the supplier's reliability posterior (FR-EVA-3) and say so in the output.

### 7.3 Supplier Evaluation (FR-SEL)
- **FR-SEL-1** Candidates = approved-panel suppliers with a valid quote for the material.
- **FR-SEL-2** Scoring is **plain Python, deterministic**, never computed by the LLM. For candidate s and quantity Q:
  - arrival = today + LEAD_DAYS_s
  - p_late = SAP-RPT P(late) for a hypothetical PO row (supplier s, Q, today); fallback Beta posterior mean
  - d_late = supplier's expected delay when late (from history)
  - stockout_days = p_late x max(0, arrival + d_late - stockout_date) + (1 - p_late) x max(0, arrival - stockout_date)
  - expected_cost = Q x NETPR_s + stockout_days x DOWNTIME_COST_PER_DAY
  - If Q > AVAILABLE_KG_NEXT_30D_s: mark "cannot cover full quantity" and evaluate a split order with the next best supplier.
  - Display score = 100 x (lowest expected_cost / expected_cost_s).
- **FR-SEL-3** Output a ranked table with every component, plus the cheapest and the fastest supplier marked.
- **FR-SEL-4** The explanation (via Gen AI Hub, masking on) states in under 120 words why #1 beats the cheapest and the fastest option, using the numbers from FR-SEL-2 only. It must not invent numbers.
- **FR-SEL-5** Explanations may cite contract terms only through Gen AI Hub grounding on the contract documents in S3.

### 7.4 Action (FR-ACT)
- **FR-ACT-1** `draft_po` creates a PO in status DRAFT for the top candidate (or the buyer's choice) with S/4 fields filled.
- **FR-ACT-2** `release_po` is allowed by policy only if NETWR <= Rp150,000,000 and the supplier is on the approved panel. Otherwise the agent calls `submit_for_approval`, and the manager approves in the UI.
- **FR-ACT-3** One-click approve for the buyer when the PO is below the threshold.

Threshold note: routine POs are 3 to 8 t of sugar, about Rp50 to 150 juta. Rp100 juta (the earlier plan) would gate about half of routine POs; Rp150 juta gates the large emergency orders (S1, S3) which is what the demo needs. Keep it a config value.

### 7.5 Integrity Guard (FR-INT)
- **FR-INT-1** If the buyer selects a supplier other than #1, require a written justification (minimum 20 characters, free text) before the draft is created.
- **FR-INT-2** Log every override: timestamp, buyer, recommended supplier, chosen supplier, cost difference, justification.
- **FR-INT-3** Pattern flag over EKKO history: flag (buyer b, supplier s) when b placed at least 5 POs with s, b's share to s is at least 2x the share of the other buyers, and a one-sided binomial test gives p < 0.01. Expected hit in the data: BUYER03 with supplier 100107.
- **FR-INT-4** Flags appear in the manager view with the evidence (counts, shares, overrides). The system flags, it never accuses or blocks the buyer.

### 7.6 Evaluation and Learning (FR-EVA)
- **FR-EVA-1** `record_outcome` after each goods receipt (on time or late, delay days).
- **FR-EVA-2** Reliability per supplier = Beta(alpha, beta), prior Beta(2, 1) (weak, slightly optimistic), updated per receipt with forgetting factor 0.97 per receipt so a deteriorating supplier (100108) shows up within weeks.
- **FR-EVA-3** Expose the posterior mean and 90% interval per supplier; chart it over time for the demo ("what does it learn?").
- **FR-EVA-4** Monte Carlo (in `eval/`, may use `data_gen/world.py`): for each scenario, 1,000 runs each for GESIT's pick, always-cheapest and always-fastest; report mean total cost, mean stock-out days, P(any stock-out).
- **FR-EVA-5** Benchmark SAP-RPT against logistic regression and gradient boosting on the same time split (AUC, Brier score).

## 8. Tool contracts (MCP via AgentCore Gateway)

All return JSON; amounts in IDR, quantities in KG, dates ISO.

| Tool | Input | Output |
|---|---|---|
| get_open_pos | matnr? | list of open PO lines |
| get_po | ebeln | PO header, item, schedule, receipts |
| get_supplier_history | lifnr, days=180 | receipts with delay, reliability posterior |
| get_quotes | matnr | quotes_asof rows |
| get_inventory | matnr | stock, daily usage, days of cover, downtime cost |
| get_market_price | matnr, date | PIHPS anchor price |
| predict_delay_risk | rows[] (rpt_po_lines schema) | P(late) per row, model id, fallback flag |
| score_suppliers | matnr, qty, need_by | ranked table (FR-SEL-2) |
| draft_po | lifnr, matnr, qty, netpr, eindt, reason | ebeln (DRAFT) |
| submit_for_approval | ebeln | approval request id |
| release_po | ebeln | released or denied (policy) |
| record_override | ebeln, recommended, chosen, justification | override id |
| record_outcome | ebeln, gr_date, qty | updated posterior |
| get_integrity_flags | none | flagged buyer and supplier pairs with evidence |

## 9. Policies (Cedar, AgentCore Policy)
- **POL-1** Deny `release_po` when amount > 150,000,000 unless the principal has role manager.
- **POL-2** Deny `draft_po` and `release_po` for suppliers not on the approved panel.
- **POL-3** Deny any tool not on the list above (Cedar is deny-by-default; permit only listed tools).
- **POL-4** Agents can never call approval tools on their own behalf.
Each policy has a test that shows one allow and one deny.

## 10. Responsible AI
- Masking of buyer and supplier names in the Gen AI Hub step; names restored only in the UI.
- No autonomous release above the threshold; human decision is always recorded.
- Every decision traceable: plan, tool calls, scores, explanation, approver (Observability trace + decision log).
- Overrides are allowed but justified; Integrity Guard flags patterns, it does not punish.
- Clear labelling of simulated data and of model limits.

## 11. Non-functional requirements
- **NFR-1** End-to-end decision for one scenario in under 60 seconds (target 20 s).
- **NFR-2** Reproducible: fixed seeds, LLM temperature at or below 0.2, `reset_demo` command restores the database to the scenario start state.
- **NFR-3** Cost per decision measured (tokens and RPT calls) and shown in the deck.
- **NFR-4** No secrets in code or git. SAP AI Core credentials in Secrets Manager or an AgentCore Identity credential provider; local dev uses `.env` (gitignored).
- **NFR-5** All UI text and generated explanations in English.
- **NFR-6** Each demo scenario passes 10 consecutive runs before 26 October.

## 12. Demo script (6 minutes inside the 15-minute slot)
1. Problem story, cost of a stopped line (0:00-2:00).
2. Architecture diagram, AWS and SAP in one picture (2:00-3:00).
3. Live (3:00-9:00):
   a. SAP-RPT scores open POs; S1 PO flagged (supplier 100101, 7-day delay, stock-out in 6 days).
   b. Supervisor plan shown; Evaluation ranks backups; explanation of why #1 beats cheapest and fastest.
   c. Replacement PO above Rp150 juta: Policy denies release, manager approves (human-in-the-loop).
   d. Buyer tries a lower-scored supplier: Integrity Guard asks for justification; manager view shows the BUYER03 / 100107 pattern.
   e. Results table vs baselines (Monte Carlo) and the reliability chart for 100108.
4. Impact numbers (9:00-10:00), then Q&A.
Backup: recorded video of the same run; switch immediately if live fails.

## 13. Proposed repository layout

```
gesit/
  CLAUDE.md  PRD.md  README.md
  data_gen/        synthetic data generator (done; files are still at the repo root, move pending, see section 17)
  adapters/        S/4-shaped data access (CSV/DynamoDB now, OData later)
  tools/           Lambda handlers for the MCP tools (section 8)
  scoring/         expected-cost scoring, reliability posterior (pure Python, unit-tested)
  integrations/sap/  SAP-RPT client, Gen AI Hub orchestration client
  agents/          Strands supervisor and sub-agents, prompts
  policy/          Cedar policies and their tests
  eval/            Monte Carlo vs baselines, RPT vs classical baselines
  ui/              Streamlit app
  infra/           AgentCore / Gateway / Lambda / DynamoDB setup scripts
  scripts/         reset_demo, load_data
  tests/
  docs/            assumptions page, architecture diagram, Q&A prep
```

## 14. Milestones

| Dates | Deliverable | Status |
|---|---|---|
| by 5 Oct | Accounts, budget alert, BAIP request, role split | in progress |
| by 6 Oct | Synthetic data generator | done; dataset generated on real PIHPS prices on 6 Oct (market type id to confirm, see section 17) |
| 5-11 Oct | Core loop in terminal: disruption, scoring, explanation, draft PO (RPT stubbed) | |
| 12-18 Oct | SAP-RPT on AI Core, Gen AI Hub, deploy to AgentCore, Gateway, Policy | |
| 19-25 Oct | Integrity Guard, Monte Carlo eval, reliability update, Streamlit UI. **Freeze 25 Oct** | |
| 26-31 Oct | Reset script, 10x runs, backup video, deck, 3 timed rehearsals in English | |

Mentoring questions: Smartsheet on Mondays 12, 19, 26 Oct before 16:00 WIB.

## 15. Risks

| Risk | Mitigation |
|---|---|
| BAIP / AI Core extended plan not provisioned | RPT Playground public API for development; RPT behind an interface with Beta-posterior fallback (FR-DET-4) |
| S/4 sandbox down (401) | Already out of scope; adapter pattern |
| Live demo failure (network, AWS) | Backup video, reset script, local fallback mode |
| LLM non-determinism | Deterministic scoring, low temperature, numbers only from tools |
| Synthetic data called unrealistic | PIHPS anchor, assumptions slide, validation report, RPT vs classical baseline |
| Scope creep | Freeze 25 Oct; anything not in section 7 goes to the backlog |

## 16. Q&A preparation
- How is this different from SAP's Bid Analysis Agent? (prediction before failure, risk-priced choice, integrity guard, proven vs baselines)
- What does the Evaluation agent actually learn? (reliability posterior per supplier, drift on 100108)
- What if the agent recommends the wrong supplier? (human approval, override with reason, outcome feeds back)
- How would this integrate with a real S/4HANA? (same field names, OData adapter)
- How did you validate the synthetic data? (PIHPS prices, validation report, assumptions)
- How does it scale to hundreds of SKUs and suppliers? (scoring is per SKU and cheap; RPT batches up to 128 rows per call)
- What does a decision cost? (measured, NFR-3)

## 17. Open decisions
- Bedrock model for the agent loop (decide by 9 Oct, then freeze).
- Approval threshold: Rp150 juta proposed (section 7.4), confirm with team.
- Whether cooking oil is added as a second material (only after freeze criteria are met; data_gen supports it).
- Approved-panel field: add a custom field (e.g. LFA1-ZZPANEL) and one off-panel quote to data_gen to demo POL-2.
- S2 trigger: the generated price increase is +2.7%, under the 3% rule in FR-DET-2. Either script S2 to a fixed increase above 3% or lower the threshold.
- Supplier 100108 drift: too weak in the generated data for the reliability chart (demo step 3e). Decide whether to strengthen the drift in `config.py`.
- PIHPS market type: confirm on the website that `price_type_id=3` is Pedagang Besar (Gula Pasir Lokal should read Rp17,500 to Rp17,550 for 21 to 25 Sep 2026).
- Repository layout: move the generator from the repo root into `data_gen/` (section 13), and decide whether to commit `data/pihps/pihps_long.csv` so teammates can regenerate without downloading.

## Changelog
- 2026-10-06: First version, from proposal, feedback plan and briefing; data generator done.
- 2026-10-06: Dataset generated on a real PIHPS download (section 6, Dataset status). Fixed unmatched-glob crash in `pihps_loader.load_pihps`. Added four open decisions (S2 trigger, 100108 drift, PIHPS market type, repository layout). No requirement changed.
