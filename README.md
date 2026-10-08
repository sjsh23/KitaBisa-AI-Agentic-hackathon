# GESIT

Multi-agent procurement disruption assistant for the Agentic AI Hackathon 2026
(Track A: Intelligent Supply Chain). When a raw material supplier is late,
raises its price or runs short of capacity, GESIT predicts the risk, ranks
backup suppliers by expected total cost, drafts the replacement PO under
policy guardrails and flags suspicious buyer and supplier patterns.

- Requirements, architecture and milestones: [PRD.md](PRD.md)
- Working and code rules: [CLAUDE.md](CLAUDE.md)
- Synthetic dataset and how to regenerate it: [data_gen/README.md](data_gen/README.md)

All procurement data in this repository is simulated. Only the price anchor
(PIHPS, Bank Indonesia) is real.

## Quick start

```bash
pip install -r requirements.txt
cd data_gen && python generate.py && python validate.py
```
On Windows use `py -3.11` instead of `python`.
