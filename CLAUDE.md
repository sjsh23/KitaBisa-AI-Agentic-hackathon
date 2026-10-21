# GESIT project instructions

GESIT is a multi-agent procurement disruption assistant for the Agentic AI
Hackathon 2026 (final demo 31 Oct 2026, in English). The full requirements are
imported below; treat them as the source of truth.

@PRD.md

Current progress (what is done, what is next) is tracked in the working log, imported below.

@WORKING.md

## Working rules

- Before starting a task, read `WORKING.md` to see where the project stands.
- After finishing a task, update `WORKING.md` in the same change: done log, status table,
  current position, next up and the "Last updated" date.
- Before starting a task, find the requirement IDs it touches (FR-_, POL-_, NFR-_, D-_)
  and mention them in the plan, code comments and commit message.
- If a task conflicts with PRD.md or needs a new requirement, stop and ask.
  When the team agrees, update PRD.md and its Changelog in the same change.
- Stay inside the repository layout in PRD section 13. Do not add top-level folders without asking.
- Feature freeze is 25 Oct 2026. After that, only bug fixes and demo hardening.
- for CLAUDE and CLAUDE CODE, NO pushing to repository, only commits with message is approved

## Code rules

- Python 3.11+. Type hints, small pure functions, pytest tests next to each module in `tests/`.
- Supplier scoring and reliability math live in `scoring/` as deterministic Python.
  The LLM only explains numbers it receives from tools; it never computes or invents them.
- Never read `data_gen/out/_truth/` or import `data_gen/world.py` outside `eval/` (rule D-1).
- Keep SAP field names (LIFNR, EBELN, BEDAT, EINDT, NETPR, MENGE, NETWR) in data models and tool I/O (D-2).
- Amounts are IDR integers, quantities KG, dates ISO `YYYY-MM-DD`.
- No secrets in code or git. Read credentials from environment variables locally (`.env`, gitignored)
  and from Secrets Manager in AWS. Never print keys in logs.
- AWS region is always `us-west-2`.
- Demo determinism: fixed seeds, LLM temperature at or below 0.2, and `scripts/reset_demo` must restore the scenario start state.
- Agent orchestration: Strands supervisor with agents-as-tools or Graph. Do not use Swarm.
- Models: SAP-RPT-1.6 for delay prediction, GPT-5.4 on SAP Gen AI Hub (Strands OpenAI provider) for agent reasoning,
  Claude Sonnet 4.6 on the same Gen AI Hub as fallback. All three run on SAP AI Core.

## Commands

```bash
# synthetic data (needs a PIHPS export in data_gen/data/pihps/)
cd data_gen && python generate.py && python validate.py
# tests
pytest -q
# Disruption Detection, then Supplier Evaluation, then Action (draft POs routed by policy),
# in the terminal: S1, S2, S3, scan or all
# (add --no-llm, --json, --approve to simulate the buyer and manager clicks,
# or --detect-only for Detection alone)
python -m agents.run S1
# Integrity Guard: the buyer's own choice, justification rule, override log, pattern flags
# (no --choose = take #1; --choose <LIFNR> or keep; log at DECISION_LOG_PATH, default ./decision_log.jsonl)
python -m agents.integrity S1 --buyer BUYER01 --choose 100108 --justification "text of 20+ characters"
# Monte Carlo against baselines (FR-EVA-4): GESIT, always-cheapest, always-fastest, keep the PO,
# outcomes from data_gen/world.py (eval/ only, D-1); --json PATH writes the numbers
python -m eval.monte_carlo --scenario all --runs 1000 --seed 42
# robustness (simulated): pre-declared extra worlds (generator seeds 1 to 10, temporary folders),
# downtime cost sweep (decisions fixed and re-made), --baseline-table rpt|same|both, --summary
python -m eval.monte_carlo --scenario all --worlds 1,2,3,4,5,6,7,8,9,10 --runs 1000 --seed 42 --summary --json PATH
python -m eval.monte_carlo --scenario all --runs 1000 --seed 42 --downtime-sweep 50000000,100000000,150000000,300000000
```

Credentials for SAP AI Core (SAP-RPT-1.6 and the Gen AI Hub models) go in `.env` (see `.env.example`).
Add new commands here when you create them (reset, deploy, UI).

## Writing style for docs, UI text and the deck

- English, plain and concrete. Label every simulated number as simulated.
- Do not use em dashes or en dashes; use commas, colons or parentheses.
