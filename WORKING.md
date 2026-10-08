# GESIT working log

Where the project stands right now. Requirements live in `PRD.md`; this file only tracks progress against them.

**Last updated: 8 October 2026**

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

Milestone "5-11 Oct, core loop in terminal" (PRD section 14). The data work is finished. No agent, scoring, tool or adapter code exists yet.

## Next up (in order)

1. `adapters/`: read the CSVs in `data_gen/out/` (never `_truth/`) with SAP field names (D-1, D-2).
2. `scoring/`: expected cost and Beta reliability posterior, with tests (FR-SEL-2, FR-EVA-2, FR-EVA-3).
3. `tools/`: the PRD section 8 tools as plain Python functions, `predict_delay_risk` stubbed with the posterior fallback (FR-DET-4).
4. Detection rules (FR-DET-1 to FR-DET-3), then a local Strands supervisor (FR-SUP-1 to FR-SUP-3).
5. Milestone is done when `python -m agents.run S1` prints the plan, the ranked table, an explanation under 120 words (FR-SEL-4) and a draft PO (FR-ACT-1).

Small fixes waiting:
- Add `.env` to `.gitignore` (NFR-4).

## Status by PRD section

| Section | Scope | Status | Evidence or what is missing |
|---|---|---|---|
| 6 | Data and generator | DONE | `data_gen/`, dataset regenerated 8 Oct, `docs/data_dictionary.md`. Dataset size is still an open decision (section 17) |
| 7.1 | Supervisor (FR-SUP) | NOT STARTED | no `agents/` |
| 7.2 | Disruption Detection (FR-DET) | NOT STARTED | no `agents/`, no `integrations/sap/` |
| 7.3 | Supplier Evaluation (FR-SEL) | NOT STARTED | no `scoring/` |
| 7.4 | Action (FR-ACT) | NOT STARTED | no `tools/` |
| 7.5 | Integrity Guard (FR-INT) | NOT STARTED | the BUYER03 / 100107 pattern is in the data, no detection code |
| 7.6 | Evaluation and Learning (FR-EVA) | NOT STARTED | no `eval/`; classical baselines exist only inside `data_gen/validate.py` |
| 8 | Tool contracts (14 tools) | NOT STARTED | 0 of 14 implemented |
| 9 | Policies (POL-1 to POL-4) | NOT STARTED | no `policy/`; panel field `LFA1.ZZPANEL` is ready for POL-2 |
| 10 | Responsible AI (masking, grounding, audit trail) | NOT STARTED | depends on Gen AI Hub and the decision log |
| 11 | NFR-1 to NFR-6 | NOT STARTED | NFR-4 gap: `.env` is not in `.gitignore` yet |
| 12 | Demo script, backup video, deck | NOT STARTED | |
| 13 | Repository layout | IN PROGRESS | present: `data_gen/`, `docs/`, `tests/`. Missing: `adapters/`, `tools/`, `scoring/`, `integrations/sap/`, `agents/`, `policy/`, `eval/`, `ui/`, `infra/`, `scripts/` |
| 17 | Open decisions | IN PROGRESS | open: Bedrock model (due 9 Oct), approval threshold, dataset size, cooking oil |

## Milestones (PRD section 14)

| Dates | Deliverable | Status |
|---|---|---|
| by 5 Oct | Accounts, budget alert, BAIP request, role split | IN PROGRESS (not visible in the repository, confirm with the team) |
| by 6 Oct | Synthetic data generator | DONE |
| 5-11 Oct | Core loop in terminal (RPT stubbed) | NOT STARTED, current milestone |
| 12-18 Oct | SAP-RPT, Gen AI Hub, AgentCore, Gateway, Policy | NOT STARTED |
| 19-25 Oct | Integrity Guard, Monte Carlo, reliability update, Streamlit UI (freeze 25 Oct) | NOT STARTED |
| 26-31 Oct | Reset script, 10x runs, backup video, deck, rehearsals | NOT STARTED |

## Not visible in the repository

Ask the team before assuming any of these: AWS accounts and budget alert, BAIP / AI Core access, Bedrock model access in us-west-2, role split.

## Done log (newest first)

- 2026-10-08: Rewrote the root `README.md` for GitHub readers: flow diagram, reading order, repository map.
- 2026-10-08: Created this working log and the rule to update it after every task (`CLAUDE.md`).
- 2026-10-08: Added `docs/data_dictionary.md`, describing every CSV file and column (PRD section 6).
- 2026-10-08: Regenerated the dataset: S2 scripted to +6%, stronger 100108 drift, `LFA1.ZZPANEL` plus off-panel supplier 100111, PIHPS market type confirmed (FR-DET-2, FR-EVA-3, POL-2).
- 2026-10-08: Moved the generator into `data_gen/` (PRD section 13, D-1).
- 2026-10-06: Synthetic data generator and first dataset on real PIHPS prices.
