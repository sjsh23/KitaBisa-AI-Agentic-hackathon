# GESIT testing guide

Step-by-step checks for what is built so far: the Disruption Detection agent
(FR-DET-1 to FR-DET-4), the Supplier Evaluation agent (FR-SEL-1 to FR-SEL-4) and
the pieces under them. All data is simulated.

Commands are for Windows PowerShell on the dev machine, where Python is started
with `py -3.11`. On macOS or Linux use `python` instead. Run everything from the
repository root.

Steps 1 to 4 need no credentials and no network. Steps 5 to 7 call the real
SAP-RPT-1.6 deployment and the real reasoning models on SAP Gen AI Hub (GPT-5.4,
fallback Claude Sonnet 4.6, both on SAP AI Core since 20 October 2026; Gemini Flash
before that). Detection ran against the real services on 9 and 11 October 2026,
Supplier Evaluation on 15 October and S1 with GPT-5.4 on 20 October (single runs, see
`WORKING.md`); the ten-run check in step 7 is still open.

## Step 1: Install

```powershell
py -3.11 -m pip install -r requirements.txt
```

Check:

```powershell
py -3.11 -c "import strands, openai, boto3, scipy, requests, dotenv; print('ok')"
```

Expected: `ok`. If `openai` is missing, the detection still runs, but the
assessment is skipped with "strands-agents[openai] is not installed".

## Step 2: Credentials

Credentials live in `.env`, which is gitignored. `.env.example` is the template
and is meant to be committed, so it must contain variable names only.

```powershell
Copy-Item .env.example .env
```

Then fill in `.env`:

| Variable | Needed for | Notes |
|---|---|---|
| `AICORE_AUTH_URL` | SAP-RPT-1.6 and the Gen AI Hub models on AI Core | Must start with `https://`. `/oauth/token` is added by the code |
| `AICORE_CLIENT_ID`, `AICORE_CLIENT_SECRET` | SAP-RPT-1.6 and the Gen AI Hub models | From the AI Core service key |
| `AICORE_BASE_URL` | SAP-RPT-1.6 and the Gen AI Hub models | The `AI_API_URL` of the service key, with or without `/v2` |
| `AICORE_RESOURCE_GROUP` | SAP-RPT-1.6 and the Gen AI Hub models | Default `default`. The `gpt-5.4` and `anthropic--claude-4.6-sonnet` deployments must be RUNNING in this resource group |
| `GESIT_LLM_MODEL` | Assessment and explanation | Optional. Default `gpt-5.4`, the Gen AI Hub model name. The deployment is found by name; on 20 October 2026 it was version `2026-03-05` |
| `GESIT_LLM_FALLBACK_MODEL` | Assessment and explanation | Optional. Empty means `anthropic--claude-4.6-sonnet`; a comma-separated list is allowed; `none` switches the fallback off. A fallback model answers only when the model before it is not deployed, is rate limited, stays overloaded, or runs out of output tokens twice |
| `SAP_RPT_DEPLOYMENT_ID` | SAP-RPT-1.6 on AI Core | The id of the running SAP-RPT deployment |
| `SAP_RPT_API_TOKEN` | Public RPT API (development only) | Alternative to the six AI Core variables |

Before every commit, confirm that no secret is staged (NFR-4):

```powershell
git status --short
git diff --cached --name-only
```

`.env` must never appear in either list, and `.env.example` must hold no real values.

## Step 3: Unit tests (offline)

```powershell
py -3.11 -m pytest -q
```

Expected: `161 passed`. The tests never call the network: SAP-RPT, SAP AI Core and the reasoning models are replaced by fakes.

| Test file | What it proves | Requirements |
|---|---|---|
| `tests/test_csv_adapter.py` | SAP field names and units, 5 open POs, 429 receipts, `_truth/` is refused | D-1, D-2 |
| `tests/test_reliability.py` | Prior Beta(2, 1), forgetting factor 0.97, 90% interval | FR-EVA-2, FR-EVA-3 |
| `tests/test_disruption.py` | The four flag rules, the 3% price threshold, stock-out date and gap | FR-DET-2, FR-DET-3 |
| `tests/test_rpt_client.py` | Request body, token caching, P(late) parsing, fallback to the posterior | FR-DET-1, FR-DET-4 |
| `tests/test_detection_agent.py` | Scenarios S1 to S3, urgency order, name masking, survival without the LLM | FR-DET-1 to FR-DET-4, PRD section 10 |
| `tests/test_supplier_score.py` | Expected-cost formula with hand-checked values (100109 in S1: 0.022 expected days, Rp178,800,000), d_late shrinkage, exclusion reasons, score, cheapest and fastest marks, split orders (capacity at, below and at zero), near tie, comparisons without negative amounts, main driver (risk or purchase) | FR-SEL-1 to FR-SEL-3 |
| `tests/test_rpt_features.py` | Feature rows for hypothetical POs use the same definitions as `rpt_po_lines`; calendar constants match `data_gen/config.py` | FR-SEL-2, D-4 |
| `tests/test_grounding.py` | Word limit, dashes, numbers not in the facts, negative Rp amounts, a LIFNR glued to a word, required mentions, the masking check | FR-SEL-4, D-3, PRD section 10 |
| `tests/test_evaluation_agent.py` | Candidates and exclusions per scenario, the EXCLUDE_FLAGGED_SUPPLIERS switch, replacement quantity, no row above its spare capacity, capacity reserved across cases, facts as phrases, the reason vs the cheapest in S1 to S3 and the check failing without it, masking check blocks the call, survival without the LLM, CLI | FR-SEL-1 to FR-SEL-4, PRD section 10 |

To run one file or one test:

```powershell
py -3.11 -m pytest tests/test_disruption.py -q
py -3.11 -m pytest tests/test_detection_agent.py -k s1 -q
```

## Step 4: Detection and Supplier Evaluation without credentials (offline)

This checks the deterministic part and the fallback (FR-DET-4). `--no-llm` skips the reasoning model.
If `.env` already holds SAP credentials, set `SAP_RPT_MODE` to `none` for this step
(`$env:SAP_RPT_MODE = "none"` in PowerShell) or the real SAP-RPT is called.

The run prints the detection report first, then the supplier evaluation for every
flagged PO, then the draft POs (Action). Add `--detect-only` to print the detection report alone.

```powershell
py -3.11 -m agents.run S1 --no-llm
```

Expected detection part (simulated data):

```
Stock 19,800 kg, 6.0 days of cover, stock-out date 2026-10-06
Delay risk model: beta-posterior-fallback (FALLBACK, SAP-RPT credentials are not configured)
...
EBELN      LIFNR    MENGE kg  EINDT       P(late)  ARRIVAL     GAP  FLAG
4500000430 100107      6,500  2026-10-02     0.30  2026-10-02   -4  -
4500000431 100101     10,000  2026-10-04     0.44  2026-10-11   +5  delay notice, 7 days, new date 2026-10-11
4500000432 100103      8,000  2026-10-03     0.16  2026-10-03   -3  -
4500000433 100104     12,000  2026-10-01     0.21  2026-10-01   -5  -
4500000434 100110      3,000  2026-10-05     0.32  2026-10-05   -1  -
...
Flagged, most urgent first: 4500000431
```

Below the table the GAP line reads "-3 = arrives 3 days before stock runs out, +5 = arrives
5 days after (too late)". The model's assessment gets the gap in words only
(ARRIVAL_VS_STOCKOUT, for example "arrives 3 days before stock runs out"), never as a signed
number, so it should never write "-3 days after stock runs out".

Then the other triggers:

| Command | Expected flagged POs |
|---|---|
| `py -3.11 -m agents.run scan --no-llm` | none (no fallback P(late) reaches 0.5) |
| `py -3.11 -m agents.run S2 --no-llm` | 4500000432, price revision +5.9% (Rp17,650 to Rp18,700 per kg) |
| `py -3.11 -m agents.run S3 --no-llm` | 4500000433, capacity shortfall, 4,800 of 12,000 kg deliverable on 2026-10-01, 7,200 kg on 2026-10-11 (5 days after stock runs out) |
| `py -3.11 -m agents.run all --no-llm` | 4500000431, 4500000433, 4500000432, in that order |

Then the supplier evaluation part (simulated data, posterior fallback, so the
numbers differ from a run with the real SAP-RPT):

| Command | Replacement case(s) | #1 | Cheapest | Fastest |
|---|---|---|---|---|
| `S1` | 4500000431, 10,000 kg | 100109, expected cost Rp179,397,321 | 100103 | 100109 |
| `S2` | 4500000432, 8,000 kg | 100109, Rp144,297,321 | 100101 | 100109 |
| `S3` | 4500000433, 7,200 kg (the late remainder only) | 100109, Rp130,257,321 | 100101 | 100109 |
| `all` | 4500000431, then 4500000433, then 4500000432 | 100109; 100109 split (2,500 kg, plus 4,700 kg from 100102); keep PO 4500000432 (Rp149,600,000) | 100105 | 100109, 100109, 100102 |

Every table also ranks keeping the flagged PO as it is (NOTE "keep PO ... as it is, no new
order", SPARE "-"): its own price (the revised Rp18,700 in S2), its expected arrival and its
own P(late). Proposed rule, `docs/prd_change_proposals.md` item 4.

| Command | Keep row | Rank |
|---|---|---|
| `S1` | 100101, arrives 2026-10-11 (5 days after stock runs out), Rp1,225,740,580 | last |
| `S2` | 100103, Rp18,700, arrives 2026-10-03, no risk cost, Rp149,600,000 | 2nd, Rp5,302,679 above 100109 |
| `S3` | 100104, remainder arrives 2026-10-11, Rp958,761,429 | last |
| `all`, case 4500000432 | 100103, Rp149,600,000 | 1st: 100109's spare kilograms went to the earlier cases |

What to check in every table:

- the header says `EXCLUDE_FLAGGED_SUPPLIERS: True`; this rule is not in FR-SEL-1 yet
  (proposal in `docs/prd_change_proposals.md`). `$env:GESIT_EXCLUDE_FLAGGED_SUPPLIERS = "false"`
  switches it off; then S2 lists 100103 as a candidate,
- the excluded list names 100111 as "off the approved panel" (FR-SEL-1, POL-2),
  100104 and 100110 as "no spare capacity" (unless excluded for a flagged PO), and
  the supplier of every flagged PO as "has a flagged PO in this run, so no new order from it
  (EXCLUDE_FLAGGED_SUPPLIERS)" (its keep row is still ranked),
- rows are sorted by EXPECTED, and EXPECTED = PURCHASE + RISK on every row,
- "cheapest new order" and "fastest new order" never point at the keep row,
- the SPARE kg column is never below the kilograms a row is ranked for. A row with
  less spare capacity than needed says "cannot cover full quantity: split ... kg here +
  ... kg from ...", and the two parts add up to the needed quantity,
- in S1, 100109 has 12,500 kg spare and covers all 10,000 kg without a split,
- in `all`, the second case prints "Spare capacity already given to an earlier case in this
  run: 100109 10,000 kg", so 100109 has 2,500 kg left and is split. This is the reason
  100109 looks different in S1 and in `all`; it is not a bug (see `docs/prd_change_proposals.md`).

Then the action part, `GESIT Action` (FR-ACT-1 to FR-ACT-3, POL-1 to POL-4; simulated data,
posterior fallback). The agent drafts a PO for every #1 (one per supplier for a split),
checks whether the buyer of the replaced PO may release it, and routes it. It never
releases a PO itself.

| Command | Draft(s) | NETWR | What happens next |
|---|---|---|---|
| `S1` | 4500000435, 100109, 10,000 kg | Rp175,500,000 | release by BUYER01 denied by POL-1, submitted as APR-4500000435, waiting for the manager |
| `S2` | 4500000435, 100109, 8,000 kg | Rp140,400,000 | within policy, waiting for BUYER02's one-click approval |
| `S3` | 4500000435, 100109, 7,200 kg | Rp126,360,000 | within policy, waiting for BUYER04's one-click approval |
| `all` | 4500000435 (as S1); 4500000436 and 4500000437 (the 100109 and 100102 split for 4500000433); nothing for 4500000432 | Rp175,500,000; Rp43,875,000 and Rp86,480,000 | manager for the first, buyer one-click for the split; 4500000432 says "keeping the current PO ranks #1, nothing drafted" |

Under every case a line says what happens to the old PO (proposal item 5). GESIT does not
change it; the buyer does it in S/4:

| Command | Old PO line starts with |
|---|---|
| `S1` | "Old PO 4500000431: cancel its 10,000 kg once the replacement is released (it would arrive 2026-10-11, 5 days after stock runs out)" |
| `S2` | "Old PO 4500000432: cancel its 8,000 kg ... (supplier 100103 now asks Rp18,700 per kg instead of Rp17,650)" |
| `S3` | "Old PO 4500000433: reduce from 12,000 to 4,800 kg (the part arriving 2026-10-01)" |
| `all`, case 4500000432 | "Old PO 4500000432: keep it as it is, expected cost Rp149,600,000 against Rp167,349,554 for the best new order, from 100102 (simulated)" |

What to check:

- draft numbers continue after the last PO in EKKO (4500000434), and start again at
  4500000435 on every run (drafts live in memory for one run, NFR-2),
- every draft above Rp150,000,000 says "denied by POL-1" and has an `APR-` request id;
  every draft at or below it says "waiting for the buyer's one-click approval",
- the last line says "Handed back to a human: yes".

Add `--approve` to simulate the human clicks: the manager (MANAGER01) releases what was
routed to it and each buyer releases its own drafts. Every order then says
"Released by ...". The last line says "Handed back to a human: no" for S1, S2 and S3; in
`all` it stays "yes", because the buyer still has to accept the revised price on 4500000432.

Run the same command twice and compare: the output must be identical (NFR-2).

## Step 5: SAP-RPT-1.6 alone (network)

Needs the SAP variables in `.env`. This calls only `predict_delay_risk` for the five open POs.

```powershell
py -3.11 -c "from dotenv import load_dotenv; load_dotenv(); import json; from adapters import CsvAdapter; from tools import predict_delay_risk; a = CsvAdapter(); print(json.dumps(predict_delay_risk(a.rpt_rows(('predict',)), adapter=a), indent=2))"
```

Pass when:

- `"fallback": false` and `"model_id": "sap-rpt-1.6"`,
- there are five predictions, one per EBELN 4500000430 to 4500000434,
- every `P_LATE` is between 0 and 1.

If `"fallback": true`, read `fallback_reason`:

| `fallback_reason` | Likely cause |
|---|---|
| `SAP-RPT credentials are not configured` | `.env` is missing, or one of the six AI Core variables is empty |
| `SAP AI Core token request failed: ...` | `AICORE_AUTH_URL` is malformed (for example a stray character before `https://`) or there is no network |
| `SAP AI Core token request returned HTTP 401` | Wrong client id or secret |
| `SAP-RPT returned HTTP 404` | Wrong `SAP_RPT_DEPLOYMENT_ID` or `AICORE_BASE_URL`, or the deployment is stopped |
| `SAP-RPT returned HTTP 403` | Wrong `AICORE_RESOURCE_GROUP` |
| `SAP-RPT returned HTTP 400` or `422` | The request body does not match the deployment; send the response to the team |
| `SAP-RPT response has no usable class confidence` | The response shape differs from the documented one; `parse_predictions` in `integrations/sap/rpt_client.py` needs adjusting |

Things to record on the first successful call, because they are still assumptions:

- The response gives a class (`LATE` or `ON_TIME`) with a `confidence`. The code reads P(late) as the confidence when the class is `LATE`, and 1 minus the confidence otherwise.
- How long the call takes (NFR-1) and how many rows were sent (414 context rows plus 5 query rows).

## Step 6: Model texts on SAP Gen AI Hub (network)

Needs the `AICORE_*` variables in `.env` (the same service key as SAP-RPT) and RUNNING
`gpt-5.4` and `anthropic--claude-4.6-sonnet` deployments in the resource group.

```powershell
py -3.11 -m agents.run S1
```

Pass when the output ends with an `Assessment (gpt-5.4):` block that:

- names PO 4500000431 and supplier 100101 (by number, never by company name),
- says the goods arrive 5 days after the stock-out date 2026-10-06,
- is at most 120 words, in English, with no em dashes or en dashes,
- contains no number that is missing from the table above it,
- does not recommend a replacement supplier.

If it says `not available (reasoning model unavailable: ...)`:

| Message | Fix |
|---|---|
| `SAP AI Core credentials (AICORE_*) are not set` | Add the AI Core variables to `.env` (step 2) |
| `strands-agents[openai] is not installed` | Step 1 |
| `SAP AI Core token request returned HTTP 401` | Wrong client id or secret, or an expired service key |
| `no RUNNING SAP AI Core deployment for ...` | The model is not deployed in `AICORE_RESOURCE_GROUP`, or `GESIT_LLM_MODEL` holds a wrong name. Check the deployments in SAP AI Launchpad. When only the main model is missing, the fallback answers and the `LLM notes` line names the missing one |
| `every Gen AI Hub model is rate limited: ...` | Every model in the chain still returned HTTP 429 after four tries. Wait a minute and rerun; the ranked tables are still printed |
| `MaxTokensReachedException` | The model used its whole output budget before finishing the answer. The agent already retries with a larger budget (8,192, then 32,768 tokens). If it still fails, the fallback model answers; the `Assessment (...)` line shows which model answered |
| `InternalServerError HTTP 503` (or another 5xx) | Gen AI Hub is overloaded; nothing is wrong with the request. The agent tries each model four times (waits of about 2, 5 and 10 s) and then the next model in the chain, so this message means every model stayed overloaded for that call. Rerun in a minute |

Then the Supplier Evaluation explanation, one per replacement case. Pass when each
`Explanation (gpt-5.4):` block:

- recommends the #1 supplier by LIFNR and says why it beats the cheapest and the fastest
  (or that it is one of them): the cost gap plus the reason, for example "mainly from extra
  risk cost Rp ... (1.85 expected days without stock against 0.02 for supplier 100109)",
  or extra purchase cost with the two prices per kg,
- says "near tie" when #1 and a comparison supplier are less than 1 percent apart in
  expected cost (happens in `all` with the real SAP-RPT),
- mentions the short track record when #1 has fewer than 10 past goods receipts (100109 has 4),
- says the figures are simulated (D-3),
- is followed by `Explanation check: ... passed`. The check fails on 120 words or more
  (FR-SEL-4), em or en dashes, any number not in the facts the model received, a negative
  Rp amount, a LIFNR glued to a word ("from100108"), or a missing required mention
  (simulated, near tie, past deliveries, the reason amount vs the cheapest and fastest,
  days without stock). A `FAILED` check does not stop the run; record
  the text and the listed problems and send them to the team.

Before the check, both the assessment and the explanation are tidied in code: a space
between a word and a number ("againts0.23" becomes "against 0.23"), a space after
punctuation, no double spaces or spaces at a line end, and known misspellings corrected.
Numbers are never changed. When something was fixed, a line `Text tidied in code: ...`
under the text lists the fixes (JSON: `REASONING_FIXES`, `EXPLANATION_FIXES`).

The header line `Explanation call:` shows the model (`gpt-5.4`) and its fallback, the
temperature (0.2 or lower, NFR-2; GPT-5.4 on Gen AI Hub accepted 0.2 on 20 October 2026) and
the masking: names are masked in code and checked before every call; if a supplier or buyer
name would reach the model, the call is not made and the explanation says "masking check
failed". Gen AI Hub serves the model, but its orchestration service (masking and contract
grounding modules) is not used yet.

To test the fallback, point the main model at a name that is not deployed:

```powershell
$env:GESIT_LLM_MODEL = "gpt-not-deployed"; py -3.11 -m agents.run S2; Remove-Item Env:GESIT_LLM_MODEL
```

Pass when the texts come from `anthropic--claude-4.6-sonnet`, the explanation check passes
and the output ends with:

```
LLM notes: gpt-not-deployed unavailable (no RUNNING SAP AI Core deployment for gpt-not-deployed in resource group default). The next model in the chain was tried (see the model name above each text).
```

On 20 October 2026 this run took 27 s and the check passed. Each `Assessment (...)` and
`Explanation (...)` line names the model that wrote it. With `--json` the same notes are
in `LLM_NOTES`.

When the main model answers HTTP 429 (rate limited) or 5xx (overloaded), the agent waits
about 2, 5 and 10 s between tries (about 17 s in all), then the fallback model answers. A
model still rate limited after the retries is skipped for the rest of the run; an
overloaded one only for that call, so one run can mix models. The notes line then says, for
example, `gpt-5.4 overloaded (HTTP 503) on 1 call`. If both models stay overloaded, one call
can take about 35 s before it gives up, so a run with four texts can exceed the 60 s target
of NFR-1.

## Step 7: Full run and repeatability (network)

```powershell
py -3.11 -m agents.run all
py -3.11 -m agents.run S1 --json > "$env:TEMP\gesit_s1.json"
```

The JSON has the parts `DETECTION`, `EVALUATION`, `ACTION`, `POS` (every PO GESIT
created in the run, with its history) and `LLM_NOTES` (with `--detect-only` it is the
detection report alone, as before). Check:

- `DETECTION.PREDICTION.FALLBACK` is `false`,
- `DETECTION.STEPS` lists `get_open_pos`, `get_inventory`, `predict_delay_risk`, in that order,
- `DETECTION.FLAGGED_EBELN` is `["4500000431"]` plus any PO whose SAP-RPT `P_LATE` is 0.5 or more,
- `DETECTION.REASONING_ERROR` is `null`,
- `EVALUATION.CASES[0].SCORING.PREDICTION.FALLBACK` is `false` (SAP-RPT scored the candidates),
- `EVALUATION.CASES[0].EXPLANATION_ERROR` is `null` and `EXPLANATION_CHECK.PASSED` is `true`,
- `ACTION.CASES[0].ORDERS[0].NEXT` is `MANAGER_APPROVAL` when its `NETWR` is above
  Rp150,000,000, otherwise `BUYER_ONE_CLICK`; `POS[0].HISTORY` has no `release_po` event.

Then run `py -3.11 -m agents.run S1` ten times (NFR-6). The detection table, the
flagged list and the ranked supplier table must be the same every time. The
assessment and explanation wording may vary slightly; their numbers must not.
One S1 run took 33 seconds on 15 October 2026 with Gemini Flash and 53 seconds on
20 October 2026 with GPT-5.4 (two SAP-RPT calls, two model calls; NFR-1 allows 60,
target 20). Time the ten runs: if S1 stays near 60 s, report it to the team.

## What to report back

After steps 5 to 7, update `WORKING.md` (status of section 7.2, done log) with:

1. SAP-RPT: worked or not, the `P_LATE` of the five open POs, call time.
2. Gen AI Hub models: worked or not, which model answered each text, and any `LLM notes` line.
3. Supplier Evaluation: the #1, cheapest and fastest supplier per scenario, and every
   explanation check that did not pass.
4. Any scenario whose flagged list differs from step 4, and why (a real SAP-RPT `P_LATE` at or above 0.5 adds a "PREDICTED_LATE" flag, which is expected behaviour).

## Not covered yet

Integrity Guard, the supervisor, Cedar policies on AgentCore and the Joule UI do
not exist yet, so there is nothing to test for them. The Action agent's policies
POL-1 to POL-4 run as local Python (`policy/local_policy.py`, tests in
`tests/test_policy.py`) until AgentCore Policy is set up. The Supplier Evaluation
explanation comes from GPT-5.4 on Gen AI Hub without the orchestration service, so Gen
AI Hub masking and contract grounding (FR-SEL-4, FR-SEL-5) cannot be tested yet. Add a section here when
each one is built.
