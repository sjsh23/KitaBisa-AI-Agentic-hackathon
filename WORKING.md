# GESIT working log

Where the project stands right now. Requirements live in `PRD.md`; this file only tracks progress against them.

**Last updated: 17 October 2026**

## How to use this file

- Start of a session: read this file, then check it against the repository (folders present, `git log`), because it can be stale.
- After finishing a task: update this file in the same change. That means:
  1. move the task from "Next up" to the top of "Done log" (date, what changed, requirement IDs),
  2. update the status table,
  3. rewrite "Current position" and "Next up" if they changed,
  4. set the "Last updated" date.
- Status values: `DONE`, `IN PROGRESS`, `NOT STARTED`, `BLOCKED` (say on what).
- Keep it short. Decisions and requirement changes go in `PRD.md` and its Changelog, not here.

## Current position

Milestone "5-11 Oct, core loop in terminal" (PRD section 14). The Disruption Detection agent runs in the terminal (`python -m agents.run S1`) on the CSV adapter. On 9 Oct it ran end to end on scenario S1 against the real SAP-RPT-1.6 deployment (no fallback) and real Gemini (`gemini-3.5-flash`), and `all` (the three notices together) ran the same way after the output token fix. On 11 Oct S1, S2, S3 and `all` each ran once against the real services, after S3 got an explicit remainder date (GAP +5 for the 7,200 kg remainder, simulated). On 15 Oct the Supplier Evaluation agent was added: `python -m agents.run S1` now prints the detection report and then, for every flagged PO, a ranked table of backup suppliers by expected cost with an explanation under 120 words. S1 and S2 ran once against the real services (SAP-RPT scored the candidate suppliers without fallback; explanation checks passed; S1 took 33 s). Later on 15 Oct the review fixes went in: spare capacity and splits are shown per row (the S1 versus `all` difference for 100109 was the capacity reservation between cases, not a bug), the exclusion of flagged suppliers became the switch EXCLUDE_FLAGGED_SUPPLIERS with a proposal for the team in `docs/prd_change_proposals.md`, and the explanation gets ready-made phrases plus a stricter check. S1 and `all` then ran against the real services with every explanation check passing (S1 took 29 s). Later the free-tier quota of `gemini-3.5-flash` (20 requests per day) ran out and `all` hung for over 10 minutes; now a used-up model is skipped within a second and `gemini-2.5-flash` answers (`all` took 42 s, all checks passed). On 16 Oct the explanation got the reason #1 beats the cheapest, not only the cost gap (for example "mainly from extra risk cost Rp ... (1.85 expected days without stock against 0.02 for supplier 100109)", simulated); S2 and S3 ran against the real services with the check passing. Later on 16 Oct a Gemini HTTP 503 (overloaded) made some texts "not available" at random; the agent now retries longer and then lets `gemini-2.5-flash` answer that call (not yet run against the real services). On 17 Oct both model texts got a tidy step in code (spacing such as "againts0.23" and misspellings, never numbers), with the fixes listed under each text. The ten-run check (NFR-6) has not been run against the real services yet. Action, Integrity Guard and the supervisor do not exist yet.

## Next up (in order)

1. Team to decide on `docs/prd_change_proposals.md` (exclusion rule, keep 100103 in S2 or not, split wording) and the other choices under "To confirm with the team" below; then copy the agreed text into `PRD.md` and its Changelog.
2. Gemini quota and overload: enable billing on the key, or the ten-run check (NFR-6) and rehearsals will run out of the free tier (20 requests per day per model). Team to confirm the fallback model, now also used on HTTP 503 (`docs/prd_change_proposals.md`, item 3).
3. Finish `docs/testing.md` steps 5 to 7 against the real services: ten consecutive S1 runs (NFR-6), now covering both agents.
4. Action agent: `draft_po`, `submit_for_approval`, `release_po` (FR-ACT-1 to FR-ACT-3).
5. Local Strands supervisor that runs Detection, Evaluation, Action in order (FR-SUP-1 to FR-SUP-3).
6. Milestone is done when `python -m agents.run S1` prints the plan, the ranked table, an explanation under 120 words (FR-SEL-4) and a draft PO (FR-ACT-1).

## Status by PRD section

| Section | Scope | Status | Evidence or what is missing |
|---|---|---|---|
| 6 | Data and generator | DONE | `data_gen/`, dataset regenerated 8 Oct, `docs/data_dictionary.md`. Dataset size is still an open decision (section 17) |
| 7.1 | Supervisor (FR-SUP) | NOT STARTED | no supervisor yet; `agents/run.py` calls Detection, then Evaluation, in a fixed order |
| 7.2 | Disruption Detection (FR-DET) | IN PROGRESS | `agents/detection.py`, `scoring/disruption.py`, `integrations/sap/rpt_client.py`, tests pass, S1, S2, S3 and `all` ran against real SAP-RPT-1.6 and Gemini; S3 gap uses the remainder date from `scenarios.json`. Missing: the ten-run check on the real services |
| 7.3 | Supplier Evaluation (FR-SEL) | IN PROGRESS | `agents/evaluation.py`, `scoring/supplier_score.py`, `scoring/rpt_features.py`, `agents/grounding.py`, `tools/score.py`; FR-SEL-1 to FR-SEL-3 done (spare capacity per row, "cannot cover full quantity" splits, near tie); FR-SEL-4 done with Gemini Flash, phrases without negative amounts, the reason #1 beats the cheapest and the fastest (main driver: risk or purchase cost), masking check before the call, spacing and spelling tidied in code, and an explanation check after it. S1, S2 and `all` ran against real SAP-RPT-1.6 and Gemini. Waiting: team decision on EXCLUDE_FLAGGED_SUPPLIERS (`docs/prd_change_proposals.md`). Missing: Gen AI Hub for the explanation (FR-SEL-4) and contract grounding (FR-SEL-5), the ten-run check |
| 7.4 | Action (FR-ACT) | NOT STARTED | no `tools/` |
| 7.5 | Integrity Guard (FR-INT) | NOT STARTED | the BUYER03 / 100107 pattern is in the data, no detection code |
| 7.6 | Evaluation and Learning (FR-EVA) | IN PROGRESS | reliability posterior in `scoring/reliability.py` (FR-EVA-2, FR-EVA-3 math). Missing: `record_outcome`, chart, `eval/`; classical baselines exist only inside `data_gen/validate.py` |
| 8 | Tool contracts (14 tools) | IN PROGRESS | 7 of 14 as plain Python in `tools/`: `get_open_pos`, `get_inventory`, `get_supplier_history`, `get_quotes`, `get_market_price`, `predict_delay_risk`, `score_suppliers` |
| 9 | Policies (POL-1 to POL-4) | NOT STARTED | no `policy/`; panel field `LFA1.ZZPANEL` is ready for POL-2 |
| 10 | Responsible AI (masking, grounding, audit trail) | IN PROGRESS | Detection and Evaluation send only LIFNR numbers to Gemini Flash, never supplier or buyer names; the explanation is checked for numbers that are not in the facts. Missing: Gen AI Hub masking and grounding, decision log |
| 11 | NFR-1 to NFR-6 | IN PROGRESS | NFR-4: `.env` is gitignored, template in `.env.example` (was missing from the repository, added 15 Oct, names only). NFR-2: temperature capped at 0.2 in `agents/llm.py`; fallback model `gemini-2.5-flash` only on quota or token exhaustion, named in the output (team to confirm). NFR-1, NFR-6: a used-up Gemini quota fails over in under a second instead of hanging; a Gemini 503 (overloaded) is retried for about 17 s, then the fallback model answers that call (team to confirm, `docs/prd_change_proposals.md` item 3). Others not started |
| 12 | Demo script, backup video, deck | NOT STARTED | |
| 13 | Repository layout | IN PROGRESS | present: `data_gen/`, `docs/`, `tests/`, `adapters/`, `tools/`, `scoring/`, `integrations/sap/`, `agents/`. Missing: `policy/`, `eval/`, `ui/`, `infra/`, `scripts/` |
| 17 | Open decisions | IN PROGRESS | open: Gemini access path when deployed, approval threshold, dataset size, cooking oil, Joule UI details |

## Milestones (PRD section 14)

| Dates | Deliverable | Status |
|---|---|---|
| by 5 Oct | Accounts, budget alert, BAIP request, role split | IN PROGRESS (not visible in the repository, confirm with the team) |
| by 6 Oct | Synthetic data generator | DONE |
| 5-11 Oct | Core loop in terminal (RPT stubbed) | IN PROGRESS, late (Detection and Supplier Evaluation done locally; Action and supervisor missing) |
| 12-18 Oct | SAP-RPT, Gen AI Hub, AgentCore, Gateway, Policy | NOT STARTED |
| 19-25 Oct | Integrity Guard, Monte Carlo, reliability update, Joule UI (freeze 25 Oct) | NOT STARTED |
| 26-31 Oct | Reset script, 10x runs, backup video, deck, rehearsals | NOT STARTED |

## Not visible in the repository

Ask the team before assuming any of these: AWS accounts and budget alert, BAIP / AI Core access, the Gemini Flash API key and how the team reaches the model, the SAP-RPT deployment id, role split. The Joule UI needs the BAIP sub-account.

## To confirm with the team (Supplier Evaluation, 15 Oct)

Choices made inside FR-SEL-1 to FR-SEL-4 where the PRD is silent. None changes a requirement; object and they change.

- Suppliers with a flagged PO in the same run are not used as backups. Not in FR-SEL-1: now the switch EXCLUDE_FLAGGED_SUPPLIERS (default on), proposal and the open S2 question (100103 only revised its price and still delivers on time) in `docs/prd_change_proposals.md`.
- A quote with zero spare capacity is excluded; a supplier with less spare capacity than needed is marked "cannot cover full quantity" and scored as a split whose kilograms add up to the quantity. A split's expected stock-out days are the larger of its two parts (conservative). PRD wording proposed in `docs/prd_change_proposals.md`.
- A comparison with #1 under 1 percent apart in expected cost is called a near tie in the explanation.
- d_late (delay when late) is the supplier's own average pulled towards the all-supplier average with a weight of 3 receipts, so one or two late receipts cannot set it alone.
- When several POs are flagged (`all`), cases are scored most urgent first and each #1 reserves its supplier's spare capacity.
- S3 replaces only the late remainder (7,200 kg). Its replacement then costs about Rp126 to 131 juta (simulated), under the Rp150 juta threshold, so S3 will not show the policy block. PRD section 7.4 expects the threshold to gate S1 and S3. Options: replace the full 12,000 kg in S3, or show the policy block with S1 only.
- The newest supplier 100109 (4 goods receipts) comes out #1 in most cases. The table shows the receipt count (GRs) and the explanation says when #1 rests on fewer than 10 receipts.
- The explanation comes from Gemini Flash until SAP Gen AI Hub is connected (FR-SEL-4 names Gen AI Hub). It never cites contract terms, so FR-SEL-5 is not started.

## Done log (newest first)

- 2026-10-17: Spacing and spelling in the model texts (FR-SEL-4, NFR-5). Gemini sometimes glued words and numbers or misspelled ("againts0.23"); the Detection assessment had no text check at all. New `grounding.tidy_text` runs on the assessment and on the explanation before the check: a space between word and number, a space after punctuation, no double or line-end spaces, known misspellings and one-letter-swap typos of words from the prompt and facts corrected. It never changes a number. Fixes are listed under the text (`Text tidied in code: ...`, JSON `REASONING_FIXES`, `EXPLANATION_FIXES`); both prompts also ask for correct spelling and spacing. S1 ran against the real services, explanation check passed (SAP-RPT timed out on that run, so the posterior fallback was used). 124 tests.

- 2026-10-16: Gemini overload fallback (NFR-1, NFR-2, NFR-6). Cause of the random "not available (ServerError 503 UNAVAILABLE)" texts: Google's `gemini-3.5-flash` was overloaded for some calls (not quota), the agent waited only 2 and 4 s between tries, and a 503 never moved on to the fallback model. `agents/llm.py` now waits about 2, 5 and 10 s (up to 25 percent random jitter, timing only), then lets the next model in the chain answer that one call; the next call tries the main model again. The notes line (was "LLM quota", now "LLM notes") counts the calls that fell back. Worst case with both models overloaded: about 35 s per call. Proposal item 3 in `docs/prd_change_proposals.md` and `docs/testing.md` updated (PRD.md not edited). Not yet run against the real services. 117 tests.

- 2026-10-16: Explanation gives the reason #1 beats the cheapest and the fastest, not only the cost difference (FR-SEL-2, FR-SEL-4). `scoring/supplier_score.py` decides the main driver (`main_driver`: the larger extra cost, risk or purchase) in code; `agents/evaluation.py` adds a WHY phrase to each comparison ("mainly from extra risk cost Rp X (A expected days without stock against B for supplier 100109)", or extra purchase cost with both prices per kg), the prompt asks for it, and the explanation check fails when the reason amount (and, for risk, "days without stock") is missing. S2 and S3 ran against real SAP-RPT-1.6 and `gemini-3.5-flash`, both checks passed (S2: 1.85 vs 0.02 days, simulated). 112 tests.

- 2026-10-15: Gemini quota fallback (NFR-1, NFR-2, NFR-6). Cause of the hang: `gemini-3.5-flash` returned HTTP 429 (free tier, 20 requests per day per model) and Strands retried it six times over about 4 minutes per call, three times over. `agents/llm.py` now switches Strands' retry off, reads the 429 (quota id, retry delay), marks a model with a per-day quota or a wait over 60 s as used up for the rest of the run, and moves to the next model in `GESIT_LLM_FALLBACK_MODEL` (default `gemini-2.5-flash`, comma list, `none` = off); short per-minute 429s are waited out (at most 30 s). All models used up: `LlmQuotaExceeded` at once, tables still printed. The CLI prints the fallback chain and an "LLM quota" note (`LLM_NOTES` in JSON). `all` ran in 42 s with every text from `gemini-2.5-flash`, all explanation checks passed. Proposal for the team in `docs/prd_change_proposals.md` item 3 (PRD.md not edited). 107 tests.

- 2026-10-15: Supplier Evaluation review fixes (FR-SEL-1 to FR-SEL-4, D-1, D-3, D-4, NFR-4). Capacity: the 2,500 kg split of 100109 came from the capacity reservation between cases in the `all` run (12,500 kg spare minus 10,000 kg for the S1 case), not from a hardcode or a wrong column; every row now shows SPARE kg, a short row says "cannot cover full quantity" with the split, and each case prints what earlier cases reserved. Exclusion of flagged suppliers is now the switch EXCLUDE_FLAGGED_SUPPLIERS (env `GESIT_EXCLUDE_FLAGGED_SUPPLIERS`), proposal and PRD Changelog draft in `docs/prd_change_proposals.md` (PRD.md not edited). Explanation: facts sent as phrases with a space around every number, direction words instead of negative amounts, near tie under 1 percent, "simulated" required; the check now also fails on negative Rp amounts, a LIFNR glued to a word and missing mentions; a masking check blocks the call if a supplier or buyer name would be sent; the run prints model, temperature and masking (Gemini Flash `gemini-3.5-flash`, 0.2, masked in code; Gen AI Hub not connected). Added `.env.example` (names only). S1 and `all` ran against the real services, all explanation checks passed. 102 tests.

- 2026-10-15: Supplier Evaluation agent (FR-SEL-1 to FR-SEL-4): candidates from the approved panel with a valid quote and spare capacity (FR-SEL-1); expected-cost scoring, split orders, score, cheapest and fastest marks in `scoring/supplier_score.py` (FR-SEL-2, FR-SEL-3); P(late) per candidate from SAP-RPT on a hypothetical PO row built with the `rpt_po_lines` definitions (`scoring/rpt_features.py`), posterior fallback (FR-DET-4); tools `get_quotes`, `get_market_price`, `score_suppliers`; Gemini Flash explanation under 120 words with a check for word count, dashes and numbers not in the facts (`agents/grounding.py`, FR-SEL-4); supplier and buyer names never sent (PRD section 10). Shared Gemini call and retry code moved from `agents/detection.py` to `agents/llm.py`. CLI runs both agents (`--detect-only` for the old output; JSON now has `DETECTION` and `EVALUATION`). `docs/testing.md` updated, including the stale test count and run status. S1 and S2 ran once against the real services. 85 tests.

- 2026-10-11: S3 remainder made explicit: `S3_REMAINDER_DELAY_DAYS = 10` in `data_gen/config.py`, four new S3 fields in `scenarios.json` (no CSV changed). Detection reads `remainder_eindt` and `remainder_kg`, computes the gap for the remainder (S3 GAP +5 for 7,200 kg, was -5), shows both arrival dates, and `all` now orders 4500000431, 4500000433, 4500000432. Docs updated, 46 tests (FR-DET-2, FR-DET-3, D-4).

- 2026-10-09: Fixed `MaxTokensReachedException` on `agents.run all`: the output token budget was 2,048 and Gemini Flash spent it on thinking. Budget raised to 8,192, one retry at 32,768, then the optional `GESIT_LLM_FALLBACK_MODEL` (empty by default, so the fixed model in PRD section 17 is unchanged). The report shows the model that answered. `all` ran against real SAP-RPT-1.6 and Gemini, 44 tests (NFR-2, NFR-6).

- 2026-10-09: Fixed the intermittent Gemini `ServerError`: default model fixed at `gemini-3.5-flash` (the `gemini-flash-latest` alias returned HTTP 503 on two of four runs), three tries on overload or rate-limit errors, and the error text now shows the HTTP code (NFR-2, NFR-6).
- 2026-10-09: Reasoning model changed from gpt-5.4 to Gemini Flash (Strands Gemini provider, `GEMINI_API_KEY`) because of OpenAI rate limits: `agents/llm.py`, tests, `requirements.txt`, `.env.example`, `PRD.md`, `CLAUDE.md`, `docs/testing.md` (NFR-2, NFR-4).
- 2026-10-09: Added `docs/testing.md`, a step-by-step testing guide for Disruption Detection (offline tests, then real SAP-RPT-1.6 and Gemini Flash checks; FR-DET-1 to FR-DET-4, NFR-2, NFR-4, NFR-6).
- 2026-10-09: Disruption Detection agent (FR-DET-1 to FR-DET-4): CSV adapter (D-1, D-2), reliability posterior (FR-EVA-2, FR-EVA-3), flag rules, SAP-RPT-1.6 client with posterior fallback, four tools, gpt-5.4 assessment through Strands, CLI `python -m agents.run`, 41 tests. Reasoning model changed to gpt-5.4 in `PRD.md`. `.env` added to `.gitignore` (NFR-4).
- 2026-10-09: UI changed from Streamlit to SAP Joule in `PRD.md` (sections 3, 4, 5, 13, 14, 15, 17), `README.md` and this file. No FR, POL or NFR changed.
- 2026-10-08: Rewrote the root `README.md` for GitHub readers: flow diagram, reading order, repository map.
- 2026-10-08: Created this working log and the rule to update it after every task (`CLAUDE.md`).
- 2026-10-08: Added `docs/data_dictionary.md`, describing every CSV file and column (PRD section 6).
- 2026-10-08: Regenerated the dataset: S2 scripted to +6%, stronger 100108 drift, `LFA1.ZZPANEL` plus off-panel supplier 100111, PIHPS market type confirmed (FR-DET-2, FR-EVA-3, POL-2).
- 2026-10-08: Moved the generator into `data_gen/` (PRD section 13, D-1).
- 2026-10-06: Synthetic data generator and first dataset on real PIHPS prices.
