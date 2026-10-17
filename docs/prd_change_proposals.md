# Proposed PRD changes (for the team)

Changes that the code already does, or clarifies, but that `PRD.md` does not say yet.
Nothing here is in `PRD.md`. When the team agrees, copy the text into `PRD.md` and its
Changelog in one change (CLAUDE.md working rules). All numbers are simulated.

## 1. Proposed PRD change: exclude suppliers with a flagged PO (FR-SEL-1)

**Status:** proposed on 15 Oct 2026, waiting for the team.

**What FR-SEL-1 says today:** "Candidates = approved-panel suppliers with a valid quote for the material."

**What the code does:** it also leaves out every supplier that has a flagged PO in the
same detection run. They are not hidden: the table lists them as excluded with the
reason "has a flagged PO in this run (EXCLUDE_FLAGGED_SUPPLIERS)".

**Switch:** `EXCLUDE_FLAGGED_SUPPLIERS = True` in `agents/evaluation.py`. Set the
environment variable `GESIT_EXCLUDE_FLAGGED_SUPPLIERS=false` to turn it off for a run.

**Reason:** a supplier that has just sent a delay notice or a capacity shortfall
notice should not be the one that replaces its own late or short order. Its quote in
`quotes_asof.csv` does not reflect the problem it just reported.

**Open point for the team:** the three scenarios are not the same.

| Scenario | Supplier | Notice | Effect of the rule |
|---|---|---|---|
| S1 | 100101 | delay notice, 7 days | excluded; sensible, its shipment is late |
| S2 | 100103 | price revision +5.9% | excluded, but 100103 still delivers on time (GAP -3); the team may want to keep it as a candidate |
| S3 | 100104 | capacity shortfall | excluded; it has no spare capacity anyway (0 kg) |

If 100103 is kept in S2, a second question follows: should it be scored at its quoted
Rp17,450 per kg (from `quotes_asof.csv`) or at the revised Rp18,700 it asks on PO
4500000432? The code uses the quote today.

**Proposed FR-SEL-1 text:** "Candidates = approved-panel suppliers with a valid quote
for the material and spare capacity above zero. By default, suppliers with a flagged PO
in the same run are excluded and listed with the reason (switch EXCLUDE_FLAGGED_SUPPLIERS)."

## 2. Clarification: split orders and capacity across cases (FR-SEL-2)

**Status:** fixed in code on 15 Oct 2026; PRD text proposed.

**What was reported:** in the S1 run 100109 was #1 for the full 10,000 kg, but in the S2
and S3 cases it was split ("2,500 kg here, 4,700 kg from 100108"). Both looked true at once.

**What it was:** not a hardcoded value and not a wrong column. 100109 has
`AVAILABLE_KG_NEXT_30D` = 12,500 kg. The split appears only in the `all` run, where the
three flagged POs are scored most urgent first and the first case (S1, 10,000 kg) reserves
10,000 kg of 100109's spare capacity, leaving 2,500 kg for the next case. In a separate
S1, S2 or S3 run, 100109 is never split. The table did not show spare capacity or the
reservation, so the result looked contradictory.

**What changed:**
- Every row shows its spare kilograms after earlier reservations (column SPARE kg).
- A row whose spare capacity is below the needed quantity is marked "cannot cover full
  quantity" and scored as a split: its spare kilograms plus the rest from the next best
  supplier, adding up to the full quantity. No supplier is ever ranked for more than it can supply.
- A supplier with zero spare capacity is excluded.
- Each case prints which kilograms were already given to an earlier case in the same run.

**Proposed FR-SEL-2 addition:** "When several POs are flagged in one run, they are scored
most urgent first and the recommended order reserves its supplier's spare capacity for the
rest of the run. A supplier is never ranked for more than its remaining AVAILABLE_KG_NEXT_30D."

## 3. Proposed PRD change: fallback model when the quota is used up (section 17, NFR-2)

**Status:** built on 15 Oct 2026 at the request of a team member; waiting for the team.

**What section 17 says today:** the model is fixed at `gemini-3.5-flash`; change it only with
a team decision, then freeze (NFR-2).

**What happened:** on 15 Oct the free-tier quota of `gemini-3.5-flash` (20 requests per day
per model) was used up by test runs. Gemini returned HTTP 429, and the run looked hung for
more than 10 minutes because Strands retried the 429 six times over about 4 minutes per call.

**What the code does now:** `gemini-3.5-flash` stays the main model. When it returns a 429
for a per-day quota (or asks to wait more than 60 seconds), it is skipped for the rest of the
run and `gemini-2.5-flash` answers. The output names the model above every text and ends
with an "LLM notes" line. If every model is used up, the run fails at once with a clear
message and still prints the ranked tables. `GESIT_LLM_FALLBACK_MODEL=none` switches the
fallback off.

**Added on 16 Oct 2026 (overload):** `gemini-3.5-flash` also returns HTTP 503 "model is
overloaded" for some calls, seemingly at random (Detection and one of three explanations in one `all`
run). This is Google's capacity, not quota. The agent now waits about 2, 5 and 10 s between
tries on the same model (was 2 and 4 s), and if the model is still overloaded,
`gemini-2.5-flash` answers that one call; the next call tries `gemini-3.5-flash` again. The
notes line says how many calls fell back. Worst case when both models stay overloaded:
about 35 s per call, which can push a run past NFR-1's 60 s.

**Why the team should decide:** the fallback writes different wording, so a demo run can
read differently from a rehearsal. The numbers cannot change: they come from the tools and
the explanation check fails any number that is not in them. Two free models still give only
40 requests per day, which is tight for the ten-run check (NFR-6) and rehearsals; billing on
the key is the real fix for the demo.

**Proposed section 17 text:** "The model is fixed at `gemini-3.5-flash`, with `gemini-2.5-flash`
as fallback only when the main model's quota is used up, it runs out of output tokens, or it
is still overloaded (HTTP 5xx) after its retries. The output always names the model that
answered."

## PRD Changelog draft

Lines to add to the Changelog at the end of `PRD.md` once the team agrees:

- 2026-10-15: FR-SEL-1: suppliers with a flagged PO in the same run are excluded by default (switch EXCLUDE_FLAGGED_SUPPLIERS, reason listed per supplier); suppliers with zero spare capacity are excluded. Open: keep 100103 in S2, since a price revision does not delay delivery.
- 2026-10-15: Section 17 and NFR-2: `gemini-2.5-flash` is the fallback model when `gemini-3.5-flash` is out of quota, out of output tokens, or still overloaded (HTTP 5xx) after its retries (overload added 16 Oct); the output names the model that answered.
- 2026-10-15: FR-SEL-2: split orders made explicit ("cannot cover full quantity", kilograms add up to Q, never ranked above spare capacity); in a run with several flagged POs, the recommended order reserves spare capacity for later cases. The S1 versus `all` difference for 100109 (12,500 kg spare) was this reservation, now shown in the output.
