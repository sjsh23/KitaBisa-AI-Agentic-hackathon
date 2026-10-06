# GESIT project instructions

GESIT is a multi-agent procurement disruption assistant for the Agentic AI
Hackathon 2026 (final demo 31 Oct 2026, in English). The full requirements are
imported below; treat them as the source of truth.

@PRD.md

## Working rules

- Before starting a task, find the requirement IDs it touches (FR-*, POL-*, NFR-*, D-*)
  and mention them in the plan, code comments and commit message.
- If a task conflicts with PRD.md or needs a new requirement, stop and ask.
  When the team agrees, update PRD.md and its Changelog in the same change.
- Stay inside the repository layout in PRD section 13. Do not add top-level folders without asking.
- Feature freeze is 25 Oct 2026. After that, only bug fixes and demo hardening.

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

## Commands

```bash
# synthetic data (needs a PIHPS export in data_gen/data/pihps/)
cd data_gen && python generate.py && python validate.py
# tests
pytest -q
```
Add new commands here when you create them (reset, deploy, UI).

## Writing style for docs, UI text and the deck

- English, plain and concrete. Label every simulated number as simulated.
- Do not use em dashes or en dashes; use commas, colons or parentheses.
