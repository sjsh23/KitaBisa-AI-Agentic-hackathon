# GESIT working log

Where the project stands right now. Requirements live in `PRD.md`; this file only tracks progress against them.

**Last updated: 11 October 2026**

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

Milestone "5-11 Oct, core loop in terminal" (PRD section 14). The Disruption Detection agent runs in the terminal (`python -m agents.run S1`) on the CSV adapter. On 9 Oct it ran end to end on scenario S1 against the real SAP-RPT-1.6 deployment (no fallback) and real Gemini (`gemini-3.5-flash`), and `all` (the three notices together) ran the same way after the output token fix. On 11 Oct S1, S2, S3 and `all` each ran once against the real services, after S3 got an explicit remainder date (GAP +5 for the 7,200 kg remainder, simulated). The ten-run check (NFR-6) has not been run against the real services yet. The other agents do not exist yet.

## Next up (in order)

1. Finish `docs/testing.md` steps 5 to 7 against the real services: ten consecutive S1 runs (NFR-6).
2. Supplier Evaluation agent: `scoring/` expected cost, `get_quotes`, `score_suppliers`, explanation (FR-SEL-1 to FR-SEL-5).
3. Action agent: `draft_po`, `submit_for_approval`, `release_po` (FR-ACT-1 to FR-ACT-3).
4. Local Strands supervisor that runs Detection, Evaluation, Action in order (FR-SUP-1 to FR-SUP-3).
5. Milestone is done when `python -m agents.run S1` prints the plan, the ranked table, an explanation under 120 words (FR-SEL-4) and a draft PO (FR-ACT-1).

## Status by PRD section

| Section | Scope | Status | Evidence or what is missing |
|---|---|---|---|
| 6 | Data and generator | DONE | `data_gen/`, dataset regenerated 8 Oct, `docs/data_dictionary.md`. Dataset size is still an open decision (section 17) |
| 7.1 | Supervisor (FR-SUP) | NOT STARTED | no `agents/` |
| 7.2 | Disruption Detection (FR-DET) | IN PROGRESS | `agents/detection.py`, `scoring/disruption.py`, `integrations/sap/rpt_client.py`, tests pass, S1, S2, S3 and `all` ran against real SAP-RPT-1.6 and Gemini; S3 gap uses the remainder date from `scenarios.json`. Missing: the ten-run check on the real services |
| 7.3 | Supplier Evaluation (FR-SEL) | NOT STARTED | no expected-cost scoring yet |
| 7.4 | Action (FR-ACT) | NOT STARTED | no `tools/` |
| 7.5 | Integrity Guard (FR-INT) | NOT STARTED | the BUYER03 / 100107 pattern is in the data, no detection code |
| 7.6 | Evaluation and Learning (FR-EVA) | IN PROGRESS | reliability posterior in `scoring/reliability.py` (FR-EVA-2, FR-EVA-3 math). Missing: `record_outcome`, chart, `eval/`; classical baselines exist only inside `data_gen/validate.py` |
| 8 | Tool contracts (14 tools) | IN PROGRESS | 4 of 14 as plain Python in `tools/`: `get_open_pos`, `get_inventory`, `get_supplier_history`, `predict_delay_risk` |
| 9 | Policies (POL-1 to POL-4) | NOT STARTED | no `policy/`; panel field `LFA1.ZZPANEL` is ready for POL-2 |
| 10 | Responsible AI (masking, grounding, audit trail) | IN PROGRESS | Detection masks supplier names and drops buyer names before Gemini Flash. Missing: Gen AI Hub masking and grounding, decision log |
| 11 | NFR-1 to NFR-6 | IN PROGRESS | NFR-4: `.env` is gitignored, template in `.env.example`. NFR-2: temperature capped at 0.2 in `agents/llm.py`. Others not started |
| 12 | Demo script, backup video, deck | NOT STARTED | |
| 13 | Repository layout | IN PROGRESS | present: `data_gen/`, `docs/`, `tests/`, `adapters/`, `tools/`, `scoring/`, `integrations/sap/`, `agents/`. Missing: `policy/`, `eval/`, `ui/`, `infra/`, `scripts/` |
| 17 | Open decisions | IN PROGRESS | open: Gemini access path when deployed, approval threshold, dataset size, cooking oil, Joule UI details |

## Milestones (PRD section 14)

| Dates | Deliverable | Status |
|---|---|---|
| by 5 Oct | Accounts, budget alert, BAIP request, role split | IN PROGRESS (not visible in the repository, confirm with the team) |
| by 6 Oct | Synthetic data generator | DONE |
| 5-11 Oct | Core loop in terminal (RPT stubbed) | IN PROGRESS, current milestone (Detection done locally) |
| 12-18 Oct | SAP-RPT, Gen AI Hub, AgentCore, Gateway, Policy | NOT STARTED |
| 19-25 Oct | Integrity Guard, Monte Carlo, reliability update, Joule UI (freeze 25 Oct) | NOT STARTED |
| 26-31 Oct | Reset script, 10x runs, backup video, deck, rehearsals | NOT STARTED |

## Not visible in the repository

Ask the team before assuming any of these: AWS accounts and budget alert, BAIP / AI Core access, the Gemini Flash API key and how the team reaches the model, the SAP-RPT deployment id, role split. The Joule UI needs the BAIP sub-account.

## Done log (newest first)

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
