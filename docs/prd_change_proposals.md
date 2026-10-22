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

**Switch:** `EXCLUDE_FLAGGED_SUPPLIERS = True` in `agents/supplier_evaluation.py`. Set the
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

**Status:** Applied to PRD.md on 20 October 2026.

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

**Status:** Applied to PRD.md on 20 October 2026.

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

**Status:** Applied to PRD.md on 20 October 2026.

**What section 8 said before:** the input and output of each of the 14 tools. Five of the ten
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

**Status:** Applied, option A (PRD.md on 20 October 2026).

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

**Status:** Applied to PRD.md on 20 October 2026.

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

## 9. Proposed PRD change: one world for the demo scenarios (FR-DET-2, FR-SEL-1 to FR-SEL-3, section 12)

**Status:** Applied to PRD.md on 20 October 2026.

**What was wrong.** Detection for trigger S1 saw only the S1 notice, so it flagged only
4500000431. In S1 alone, 100103 (PO 4500000432, P(late) 0.20, no notice) was a valid backup
and was marked "cheapest" (Rp195,467,857 expected, simulated). In `all`, the S2 notice flags
4500000432, so 100103 is excluded and the cheapest is 100105 (Rp258,401,099, simulated). Same
case, two tables.

**Rule now.**
- S1, S2, S3 and `all` all see every notice on file (`agents.run.notices_on_file`), so
  Detection flags 4500000431, 4500000433 and 4500000432 in each. FR-DET-1 and FR-DET-2 already
  ask for every open PO to be checked; a real system would know every notice on file whatever
  woke it up. `scan` still has no notices.
- The scenario only picks the case shown and drafted (`agents.run.cases_for`).
- Supplier Evaluation needs Detection's full flagged list as an explicit argument and fails
  with a clear error without it, or when a flagged PO is missing from it.
- Spare capacity goes to the flagged POs in Detection's urgency order (largest gap after the
  stock-out date, then P(late), then EBELN: 4500000431, 4500000433, 4500000432). For a later
  case the more urgent ones are scored first, only to reserve their capacity. Printed under
  every table.
- Cheapest tie-break on an equal NETPR: fewer expected stock-out days, then more spare
  capacity, then the lower LIFNR (100103 and 100105 both quote Rp17,450). Printed under every
  table.

**What changes for the demo (simulated data).**
- S1: same #1 (100109); 100103 excluded; cheapest now 100105.
- S2 alone now recommends keeping PO 4500000432 (Rp149,600,000), as `all` already did, because
  100109's spare capacity goes to S1 first. Nothing is drafted for S2. This drops the S2 story
  "replace the supplier that raised its price" from section 12 unless the team prefers another
  rule.
- S3 alone now has 100109 with 2,500 kg left (10,000 kg reserved for S1).

**Proposed PRD text.** Add to FR-DET-2: "Every open PO is checked against every notice on file,
whatever the trigger." Add to FR-SEL-2: "When several POs are flagged, spare capacity is given
to them in Detection's urgency order; a case's ranked table does not depend on which cases
are shown." Add to FR-SEL-3: "On an equal price, cheapest goes to fewer expected stock-out
days, then more spare capacity, then the lower LIFNR."

## 10. Supervisor choices where the PRD is silent (FR-SUP-1 to FR-SUP-3, FR-INT-1, NFR-1)

**Status:** built, waiting for team agreement (22 Oct 2026). `agents/supervisor.py`.

- **Orchestration.** A Strands Graph (`strands.multiagent.GraphBuilder`, strands-agents
  1.61.0). Each node is a plain Python step (`StepNode`, a `MultiAgentBase` whose
  `invoke_async` calls the existing agent), each edge is fixed in code, and an edge is followed
  only while no stop rule has fired. No LLM decides the route; agents-as-tools was not used
  because there the supervisor model would pick the next tool.
- **Integrity Guard runs before Action, not after.** FR-SUP-2 lists "Action, then Integrity
  Guard", but FR-INT-1 needs the justification before the draft is created. So when the buyer
  passes `--choose` with an option other than #1, the Integrity Guard checks and logs the
  override first and Action drafts the logged choice (as `agents.integrity` already did). With
  `--choose` naming #1 the step is SKIPPED. The choice applies to the most urgent case shown.
  Proposed FR-SUP-2 text: "Run Detection, then Supplier Evaluation, then Integrity Guard when
  the buyer chooses an option other than #1, then Action; Evaluation and Learning runs offline."
- **Trigger.** A scenario id (S1, S2, S3, all) stands in for a supplier notice, `scan` for a
  scheduled scan; both take the same path.
- **Plan.** Built in code before anything runs: ordered steps, agent, why. It cannot name the
  number of flagged POs before Detection has run, so each step's RESULT (for example "3
  flagged", "1 draft: 4500000435 PENDING_APPROVAL") is filled in when the step ends.
- **Plan log.** One JSON line per run at PLAN_LOG_PATH (default `plan_log.jsonl` next to the
  decision log, gitignored), written when the run ends, also after a failed step. Every step has
  STATUS (DONE, SKIPPED, NOT_RUN, FAILED), SECONDS and RESULT, plus the stop rule and per case
  who decides next. Plan ids `PLAN-<n>`.
- **Stop rules, each a normal outcome.** Nothing flagged: stop after Detection, "no action
  needed". Keeping the current PO ranks #1 for every case: stop after Supplier Evaluation,
  nothing drafted, the old PO line is printed in the outcome instead of an Action section. No
  candidate supplier for a case: that case goes back to the buyer, the others go on; when no
  case has a new order to draft the run stops after Supplier Evaluation. A justification under
  20 characters: stop after the Integrity Guard, nothing drafted. Policy denies release: the
  draft is submitted for approval and the run ends, handed back to the manager; the supervisor
  never waits and never calls `release_po`. A POL-2 block (not reachable today: off-panel
  suppliers are never ranked) is also handed back.
- **PO store.** The supervisor CLI uses the saved PO store (item 11), so EBELNs continue across
  runs; `agents.run` keeps the in-memory store and its numbers.

## 11. Proposed PRD change: manager approval with a reject path (FR-ACT-2, FR-ACT-3, POL-1, POL-4, section 8)

**Status:** built, waiting for team agreement (22 Oct 2026). `tools/action.py`,
`adapters/po_store.py`, `agents/approvals.py`.

**Why.** Mentor feedback (21 Oct): "the component that owns the state of the PO should own the
wait for approval" and "what happens if the manager rejects it". FR-ACT-2 only says the manager
approves.

**What was built.**
- **The Action side owns the PO state, across processes.** The PO store can save to and load
  from a JSON file (PO_STORE_PATH, default `po_store.json` next to the decision log,
  gitignored); new EBELNs continue from the highest one in the file. `clear_po_store` empties
  it for the later `scripts/reset_demo`. The in-memory mode stays for the tests and
  `agents.run`. Each draft keeps the case's ranked table (RANKING, DRAFT_RANK) and its old PO
  line, and every history event has its time (AT).
- **`list_pending_approvals()`**: every PENDING_APPROVAL PO with EBELN, APPROVAL_REQUEST_ID,
  LIFNR, NETWR, REPLACES_EBELN, the draft's reason (rank and expected cost) and the submit time.
- **approve** reuses `release_po` with the manager principal; no new release logic.
- **`reject_po(ebeln, reason)`**: an approval tool. POL-4 denies it to every agent principal;
  only role manager may reject (a buyer is denied under POL-1, since a PENDING_APPROVAL PO is
  one that POL-1 routed to the manager; alternative: a new POL-5); only a PENDING_APPROVAL PO;
  the reason needs at least 20 characters (the FR-INT-1 rule). Status REJECTED, with
  REJECTED_BY, REJECT_REASON and the time in the PO's event list.
- **Wrong status is an error and changes nothing:** approving or rejecting twice, approving a
  REJECTED PO, rejecting a RELEASED PO.
- **After a reject (a normal path).** The rejected draft is never released. The old PO
  (REPLACES_EBELN) is untouched, so the buyer still has it. The case goes back to the buyer with
  the next option from the ranked table saved with the draft: the best row other than the one
  drafted (the next new order, or keeping the current PO when that ranked higher). Nothing is
  scored again and nothing is drafted; choosing that option over #1 later needs the FR-INT-1
  justification. The reject is appended to the decision log as its own entry type
  (`ENTRY_TYPE: REJECT`, id `REJ-<EBELN>-<n>`); override entries are unchanged.
- **Split orders.** Each part is approved or rejected on its own (POL-1 is checked per PO, see
  the Action notes in WORKING.md); a reject names the other part if it still waits.

**S1 example (real SAP-RPT-1.6, 22 Oct, simulated data).** The supervisor drafts 4500000435
(100109, 10,000 kg, NETWR Rp175,500,000), POL-1 denies the buyer's release and it is submitted
as APR-4500000435. `python -m agents.approvals reject 4500000435 --as MANAGER01 --reason
"Price too high for this quarter, ask buyer for option 2"` sets it REJECTED; old PO 4500000431
stays with BUYER01; the next option printed for the buyer is rank 2, 100106, 10,000 kg at
Rp17,500 per kg, arriving 2026-10-04, expected cost Rp211,760,179 (simulated). A second
supervisor run drafts 4500000436, and `approve 4500000436 --as MANAGER01` releases it.

**Manager identity.** The approver is the principal given on the command line (`--as
MANAGER01`, role from `--role`, default manager); the tools take it as an argument and hold no
user id. In the deployed version this identity comes from the UI login (Joule or the fallback
UI), passed by the Gateway as the caller identity (section 8), never typed in.

**Proposed PRD text.** Add to FR-ACT-2: "The manager approves (the PO is released) or rejects
with a reason of at least 20 characters. A rejected draft is never released, the PO it
replaces is not changed, and the case goes back to the buyer with the next option from the same
ranked table, not drafted. The reject is logged." Section 8: add `list_pending_approvals` (input
none; output the POs waiting for approval) and `reject_po` (input ebeln, reason; caller identity
from the Gateway; output rejected, refused or denied, and the next option). POL-3: permit both.
POL-4: `reject_po` is an approval tool. POL-1 (or a new POL-5): only role manager may call
`reject_po`.

## 12. Proposed PRD change: minimum order quantity in quotes and scoring (section 6, FR-SEL-1, FR-SEL-2, section 8)

**Status:** built, waiting for team agreement (22 Oct 2026). `data_gen/config.py`,
`data_gen/generate.py`, `scoring/supplier_score.py`.

**Why.** Mentor question (21 Oct): "what if the backup supplier has a minimum order
quantity?". A split order can leave a small remainder for its second supplier, below what
that supplier accepts.

**Data.** New column `quotes_asof.MINBM` (kg, S/4 EINE-MINBM), after AVAILABLE_KG_NEXT_30D,
from a fixed per-supplier setting in `data_gen/config.py` (`MINBM_KG`, default
`MINBM_DEFAULT_KG`), not from the random generator: 1,000 kg for every supplier, 5,000 kg for
100102 (simulated assumption, D-4). Every other column and every other file is
byte-identical after regenerating; the generated worlds of `eval/worlds.py` get the same
setting. `get_quotes` returns the new field; the section 8 shape is unchanged.

**Rule (one place, `scoring/supplier_score.py`).** For every part of a new order (a full order,
or each of the two parts of a split):
- below MINBM: order MINBM instead when AVAILABLE_KG_NEXT_30D allows it; the extra kg is
  purchase cost at that supplier's NETPR, with no credit for the extra stock (conservative). The
  row shows the ordered kg and a mark such as "MOQ +300 kg";
- AVAILABLE_KG_NEXT_30D below MINBM: the supplier cannot take the order; excluded as a full
  order (new reason BELOW_MOQ, "spare capacity below MOQ"), skipped as a split partner so the
  next partner is tried;
- the keep-the-current-PO row is not affected (an existing PO already met its MOQ);
- capacity reservations and draft POs use the ordered kg, so a draft's MENGE and NETWR include
  the round-up and POL-1 is checked on that NETWR;
- risk cost, P(late), d_late and the downtime cost do not change; a quote without MINBM counts
  as 0, so older files score exactly as before;
- one footer line under every ranked table (`MOQ_RULE_TEXT`); the explanation facts name the
  round-up when #1 has one.

**S3 before and after (real SAP-RPT-1.6, `agents.run all --no-llm`, simulated data).**

| | Rank | Row | Purchase Rp | Expected Rp |
|---|---|---|---|---|
| before | 1 | 100109 2,500 kg + 100102 4,700 kg | 130,355,000 | 158,520,179 |
| before | 2 | 100102 7,200 kg | 132,480,000 | 160,645,179 |
| after | 1 | 100102 7,200 kg | 132,480,000 | 160,645,179 |
| after | 2 | 100109 2,500 kg + 100102 5,000 kg (MOQ +300 kg) | 135,875,000 | 164,040,179 |

The 300 extra kg at Rp18,400 add Rp5,520,000 to the split, so 100102 alone becomes #1 for S3:
one draft of 7,200 kg (NETWR Rp132,480,000, buyer one-click under POL-1) instead of two. S3
now reserves 7,200 kg of 100102 and no 100109, so S2's table gets the 100109 2,500 kg +
100102 5,500 kg split as its best new order (Rp171,231,250); keeping PO 4500000432 stays #1.
S1 changes only by the footer line.

**Monte Carlo (seed 42, 1,000 runs, real SAP-RPT-1.6, simulated).** Tracked world:
GESIT_LEARNING Rp467.6 juta before, Rp469.0 juta after (S3 now the same order as
always-fastest); the other strategies unchanged. Over 11 worlds, GESIT_LEARNING against
always-fastest: cheaper in 6, equal in 2, higher in 3 (mean +Rp7.0 juta) before; cheaper in
3, equal in 5, higher in 3 (mean +Rp8.4 juta) after. Against always-cheapest 9 of 11 and
against keeping the PO 11 of 11 both times. MINBM was not tuned after these results. If
the team agrees, the PRD section 16 Q&A numbers (6, 2, 3, Rp7 juta) need the new values.

**Proposed PRD text.** Section 6 quotes_asof row: "Current quote, lead time, spare capacity,
minimum order quantity (MINBM) per supplier". FR-SEL-1: "A supplier whose spare capacity is
below its MINBM is not a candidate." FR-SEL-2: "An order part below the supplier's MINBM is
raised to MINBM; the extra kilograms are purchase cost, with no credit for the extra stock."
Section 8 `get_quotes`: quotes_asof rows incl. MINBM.

## PRD Changelog draft

Lines to add to the Changelog at the end of `PRD.md` once the team agrees:

- 2026-10-22: FR-SUP-2: Integrity Guard runs before Action when the buyer chooses an option other than #1 (FR-INT-1 needs the justification before the draft); the supervisor is a Strands Graph with fixed edges and logs its plan with seconds per step (item 10).
- 2026-10-22: FR-ACT-2, section 8, POL-1, POL-3, POL-4: the manager can reject a PO waiting for approval with a reason of at least 20 characters; the draft is never released, the old PO is untouched and the buyer gets the next option from the saved ranked table; new tools `list_pending_approvals` and `reject_po`; the PO store survives between processes (item 11).

- 2026-10-15: FR-SEL-1: suppliers with a flagged PO in the same run are excluded by default (switch EXCLUDE_FLAGGED_SUPPLIERS, reason listed per supplier); suppliers with zero spare capacity are excluded. Open: keep 100103 in S2, since a price revision does not delay delivery.
- 2026-10-15: FR-SEL-2: split orders made explicit ("cannot cover full quantity", kilograms add up to Q, never ranked above spare capacity); in a run with several flagged POs, the recommended order reserves spare capacity for later cases. The S1 versus `all` difference for 100109 (12,500 kg spare) was this reservation, now shown in the output.
- 2026-10-22: Section 6, FR-SEL-1, FR-SEL-2, section 8: minimum order quantity (quotes_asof.MINBM, simulated: 1,000 kg, 5,000 kg for 100102); an order part below MINBM is raised to MINBM and the extra kg counts as purchase cost; a supplier with spare capacity below MINBM is skipped (item 12).
