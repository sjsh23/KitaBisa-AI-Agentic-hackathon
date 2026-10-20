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

**Update 20 Oct 2026:** item 4 answers most of this. Keeping PO 4500000432 at Rp18,700 is
now ranked in every S2 table, whatever this switch says, so the rule only decides whether
100103 may also get a second, new order at its quoted Rp17,450.

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

**Status:** superseded on 20 Oct 2026. The team moved the reasoning model to GPT-5.4 on SAP
Gen AI Hub with Claude Sonnet 4.6 on the same Gen AI Hub as fallback; `PRD.md` section 17
and its Changelog now say so. The same fallback rules apply (not deployed, rate limited,
overloaded after retries, out of output tokens twice). The text below is kept as history.

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

## 4. Proposed PRD change: rank "keep the current PO" next to the backups (FR-SEL-1, FR-SEL-3, FR-ACT-1)

**Status:** built on 20 Oct 2026 after a team review question; the team member chose "ranked
option". Waiting for the team to confirm the PRD text.

**The question (judges are likely to ask it):** why replace PO 4500000432 at all? It still
arrives 3 days before stock runs out; only the price went up (+5.9%). The output had no
"keep the old PO" comparison.

**What we found (simulated):** keeping it costs 8,000 kg x Rp18,700 = Rp149,600,000 with no
risk cost (even a late delivery, 2.7 days on average for 100103, lands before the stock-out
date). In `S2` alone the best backup, 100109, costs Rp143,078,571 with SAP-RPT (Rp144,297,321
on the fallback), so replacing still wins, by about Rp5 to 6.5 juta. In `all`, 100109's spare
capacity already went to the S1 and S3 cases, the best backup costs Rp167 to 175 juta, and
keeping the PO is about Rp18 to 25 juta cheaper. Before this change GESIT drafted a
replacement anyway.

**What the code does now:** every ranked table has one extra row, OPTION
`KEEP_CURRENT_PO`: the flagged PO as it stands, costed with the same FR-SEL-2 formula. Its
price is the PO price, or the revised price after a price increase; its arrival is the
expected arrival from detection (the delay notice's new date, the remainder's date for a
shortfall); its P(late) is the PO's own prediction from Detection (SAP-RPT, or the posterior
fallback). It needs no spare capacity and reserves none. The cheapest and fastest marks
(the FR-SEL-4 baselines) are given among new orders only. If the keep row ranks #1, the
explanation recommends keeping the PO and the Action agent drafts nothing; the case goes back
to the buyer, who accepts the revised price in S/4 or negotiates it. If a new order wins, the
explanation also says why it beats keeping the PO. In S1 and S3 the keep row ranks last
(arrival 5 days after stock runs out, Rp959 juta to Rp1.2 miliar expected cost, simulated),
which answers "why not just wait" with a number.

**Tool contract:** `score_suppliers` gets an optional input `current_po` (EBELN, LIFNR, NETPR,
ARRIVAL, P_LATE). The output adds `OPTION` and `KEEP_EBELN` per row, plus
`RECOMMENDED_OPTION` and `KEEP`. Inputs and outputs keep SAP field names (D-2).

**Proposed FR-SEL-1 addition:** "Keeping the flagged PO as it is (its own price, revised if
the supplier raised it, its expected arrival and its P(late)) is ranked as one more option."

**Proposed FR-SEL-3 addition:** "Cheapest and fastest are marked among new orders only."

**Proposed FR-ACT-1 addition:** "If keeping the flagged PO ranks #1, no PO is drafted and the
case is handed back to the buyer."

## 5. Proposed PRD change: say what happens to the old PO (FR-ACT-1, section 8)

**Status:** built on 20 Oct 2026 after a team review question; the team member chose "say it,
don't change it". Waiting for the team to confirm.

**The question:** PO 4500000431 is replaced and PO 4500000433 keeps only 4,800 kg, but the
output did not say whether the old POs are cancelled or reduced.

**What the code does now:** the Action output has one line per case (`OLD_PO` in JSON) built in
code from the numbers:

| Case | Old PO line (simulated) |
|---|---|
| S1, delay notice | cancel the 10,000 kg of 4500000431 once the replacement is released (it would arrive 2026-10-11, 5 days after stock runs out) |
| S2, price increase, new order wins | cancel the 8,000 kg of 4500000432 (100103 now asks Rp18,700 per kg instead of Rp17,650) |
| S2 in `all`, keep wins | keep 4500000432; the buyer accepts the revised price or negotiates it |
| S3, capacity shortfall | reduce 4500000433 from 12,000 to 4,800 kg (the part arriving 2026-10-01); the 7,200 kg remainder moves to the new order |

GESIT does not change the old PO itself and the line says so: section 8 has no tool for it,
so the buyer does it in S/4 after the replacement is released. A `change_po` tool would add
a row to section 8 and to POL-3's allowed list; not proposed for the demo.

**Sentence for the deck:** "When GESIT replaces a PO it says what happens to the old one:
cancel it (S1), or cut it to the part that still arrives on time (S3, 12,000 to 4,800 kg).
The buyer makes that change in S/4; GESIT only drafts the new PO."

**Proposed FR-ACT-1 addition:** "Every draft states what happens to the PO it replaces: cancel
it, reduce it to the quantity that still arrives on time, or keep it. The buyer makes the change
in S/4; GESIT does not change the old PO."

## 6. Proposed PRD change: tool inputs and outputs the code needs (section 8)

**Status:** found on 20 Oct 2026 when the PRD was checked against the code; waiting for the team.

**What section 8 says today:** the input and output of each of the 14 tools. Five of the ten
tools built so far take more than that, or return more.

| Tool | Section 8 today | Added in the code | Why |
|---|---|---|---|
| `score_suppliers` | input matnr, qty, need_by | `exclude_lifnr`, `reserved_kg`, `current_po`, `today` (all optional) | suppliers with a flagged PO are not backups (item 1); spare capacity already promised to an earlier case in the same run (item 2); "keep the current PO" as a ranked option (item 4); the as-of date, by default the inventory date |
| `predict_delay_risk` | output P(late) per row, model id, fallback flag | `fallback_reason`, `retries` | FR-DET-4 asks the output to say why the fallback was used; network retries since 20 Oct (NFR-6) |
| `draft_po` | input lifnr, matnr, qty, netpr, eindt, reason | `ernam`, `replaces`, `bedat` (optional) | ERNAM is the buyer the PO is for (the buyer of the PO it replaces); REPLACES_EBELN links the draft to the old PO (item 5); BEDAT defaults to today |
| `submit_for_approval`, `release_po` | input ebeln | the caller (user and role) | POL-1 needs the role (buyer or manager) and POL-4 needs to know whether an agent is calling |
| `get_supplier_history` | input lifnr, days=180 | `today` (optional) | the as-of date the 180 days count back from |

The caller in the last two rows is a function argument only while the policies run as local
Python. On AgentCore the Gateway passes the caller's identity to Cedar, so it would not be a
tool input there; the PRD row can say "caller identity (from the Gateway)".

Not proposed: the `adapter`, `store` and `client` arguments in `tools/`. They are the data
access of the local build (CSV files, in-memory POs, the SAP-RPT client) and disappear when
the tools become Lambda handlers behind the Gateway.

**Proposed section 8 rows:**

| Tool | Input | Output |
|---|---|---|
| get_supplier_history | lifnr, days=180, today? | receipts with delay, reliability posterior |
| predict_delay_risk | rows[] (rpt_po_lines schema) | P(late) per row, model id, fallback flag and reason, network retries |
| score_suppliers | matnr, qty, need_by, today?, exclude_lifnr?, reserved_kg?, current_po? | ranked table (FR-SEL-2), with the keep-the-current-PO row when current_po is given |
| draft_po | lifnr, matnr, qty, netpr, eindt, reason, ernam, replaces?, bedat? | ebeln (DRAFT) |
| submit_for_approval | ebeln; caller identity (from the Gateway) | approval request id |
| release_po | ebeln; caller identity (from the Gateway) | released or denied (policy) |

## 7. Proposed rename: two different agents are called "Evaluation" (sections 4, 5, 7.1, 16)

**Status:** found on 20 Oct 2026 when the PRD was checked against the code; waiting for the team.

**The clash:** the PRD has a Supplier Evaluation agent (FR-SEL, ranks backup suppliers) and an
Evaluation agent (FR-EVA, Evaluation and Learning: reliability update, baselines). In the code,
`agents/evaluation.py` with class `EvaluationAgent` is the Supplier Evaluation agent, and the run
JSON calls its report `EVALUATION`. When the FR-EVA agent is built there is no clear name left
for it. The PRD itself is ambiguous in FR-SUP-2: "Run Detection, then Evaluation, then Action,
then Integrity/Evaluation as needed" uses the word for both agents in one sentence.

**Option A (recommended): rename the code, the PRD stays.** `agents/evaluation.py` becomes
`agents/supplier_evaluation.py`, `EvaluationAgent` becomes `SupplierEvaluationAgent`, the JSON
key `EVALUATION` becomes `SUPPLIER_EVALUATION`, and `tests/test_evaluation_agent.py`,
`docs/testing.md` and `WORKING.md` follow. The FR-EVA agent then gets `agents/evaluation.py`,
matching the PRD's "Evaluation agent". No requirement changes; about an hour of mechanical work
with the tests as a check. Anyone reading `--json` output must use the new key.

**Option B: rename the FR-EVA agent in the PRD to "Learning agent".** Section 5 diagram,
section 4 table (Review row), section 7.6 heading and section 16 Q&A ("What does the Learning
agent actually learn?"). No code change, but "Evaluation" stays the name of the backup ranking,
which judges may confuse with "evaluation vs baselines".

**Proposed FR-SUP-2 wording (either option):** "Run Detection, then Supplier Evaluation, then
Action, then Integrity Guard and Evaluation and Learning as needed; stop and hand back to the
human when policy denies an action."

## 8. Clarifications: Integrity Guard choices where the PRD is silent (FR-INT-1 to FR-INT-3, section 8)

**Status:** built on 20 Oct 2026 (`scoring/integrity.py`, `tools/integrity_tools.py`,
`agents/integrity.py`); waiting for the team. None changes a rule; object and they change.

- **FR-INT-1 covers "keep the current PO" too.** The PRD says "a supplier other than #1". Since
  keeping the flagged PO is ranked (item 4), choosing a new order when keeping ranked #1, or
  keeping when a new order ranked #1, also needs the 20-character justification. Options are
  named by LIFNR or "keep PO <EBELN>" in the log.
- **Only ranked options can be chosen.** A supplier that was excluded (off the panel, no spare
  capacity, a flagged PO in this run) has no expected cost, so it is rejected before the
  override is logged. POL-2 therefore never fires in this path; it still guards `draft_po`.
- **FR-INT-3 when no other buyer uses the supplier.** share_others = 0 gives no ratio; the code
  counts it as meeting the "at least 2x" rule when k > 0 (p is then 0). It does not occur in the
  current data.
- **FR-INT-3 test count.** Every buyer and supplier in EKKO is one test, k = 0 included: 40
  tests (4 buyers x 10 suppliers), 1 flagged (BUYER03 with 100107, simulated). At p < 0.01 about
  0.4 would pass the p-value rule by chance; the output says so.
- **Section 8, `record_override`.** Input is ebeln, recommended, chosen, justification as in the
  PRD, plus the Evaluation case (the expected costs are copied from its ranked rows, never sent
  by a model) and the buyer (from the caller identity once behind the Gateway). The override id
  is `OVR-<EBELN>-<n>`. `get_integrity_flags` also returns the number of pairs tested and
  override counts per buyer and per chosen option.
- **Storage.** The decision log is a JSONL file (DECISION_LOG_PATH, default
  `./decision_log.jsonl`, gitignored) until DynamoDB is set up (PRD section 5), with the same
  function signatures.

## PRD Changelog draft

Lines to add to the Changelog at the end of `PRD.md` once the team agrees:

- 2026-10-15: FR-SEL-1: suppliers with a flagged PO in the same run are excluded by default (switch EXCLUDE_FLAGGED_SUPPLIERS, reason listed per supplier); suppliers with zero spare capacity are excluded. Open: keep 100103 in S2, since a price revision does not delay delivery.
- 2026-10-20: FR-SEL-1, FR-SEL-3, FR-ACT-1: keeping the flagged PO as it is is ranked as one more option with the same expected-cost formula; cheapest and fastest are marked among new orders only; nothing is drafted when keeping ranks #1. `score_suppliers` gets the optional input `current_po` (section 8).
- 2026-10-20: FR-ACT-1: every case says what happens to the old PO (cancel, reduce to the on-time part, or keep); the buyer makes the change in S/4, GESIT does not.
- 2026-10-15: FR-SEL-2: split orders made explicit ("cannot cover full quantity", kilograms add up to Q, never ranked above spare capacity); in a run with several flagged POs, the recommended order reserves spare capacity for later cases. The S1 versus `all` difference for 100109 (12,500 kg spare) was this reservation, now shown in the output.
- 2026-10-20: Section 8: tool inputs and outputs brought in line with the code: `get_supplier_history` today?; `predict_delay_risk` returns the fallback reason and network retries; `score_suppliers` takes today?, exclude_lifnr?, reserved_kg?, current_po?; `draft_po` takes ernam, replaces?, bedat?; `submit_for_approval` and `release_po` use the caller identity from the Gateway (POL-1, POL-4).
- 2026-10-20: FR-SUP-2 names Supplier Evaluation and Evaluation and Learning apart; the code's Supplier Evaluation agent is renamed `agents/supplier_evaluation.py` (option A) or the FR-EVA agent becomes the Learning agent (option B).
- 2026-10-20: FR-INT-1: the justification rule also applies to choosing or leaving "keep the current PO"; only ranked options can be chosen. FR-INT-3: share_others = 0 counts as meeting the ratio rule; every buyer and supplier pair in EKKO counts as one test (40 on the current data, 1 flagged, simulated). Section 8: `record_override` also takes the Evaluation case and the buyer; `get_integrity_flags` returns the number of pairs tested and override counts.
