# GESIT testing guide

Step-by-step checks for what is built so far: the Disruption Detection agent
(FR-DET-1 to FR-DET-4) and the pieces under it. All data is simulated.

Commands are for Windows PowerShell on the dev machine, where Python is started
with `py -3.11`. On macOS or Linux use `python` instead. Run everything from the
repository root.

Steps 1 to 4 need no credentials and no network. Steps 5 to 7 call the real
SAP-RPT-1.6 deployment and the real Gemini Flash endpoint. As of 9 October 2026,
steps 5 to 7 have not been run by anyone yet, so their expected results below
describe what the code should do, not an observed run.

## Step 1: Install

```powershell
py -3.11 -m pip install -r requirements.txt
```

Check:

```powershell
py -3.11 -c "import strands, google.genai, scipy, requests, dotenv; print('ok')"
```

Expected: `ok`. If `strands` is missing, the detection still runs, but the
Gemini Flash assessment is skipped with "strands-agents[gemini] is not installed".

## Step 2: Credentials

Credentials live in `.env`, which is gitignored. `.env.example` is the template
and is meant to be committed, so it must contain variable names only.

```powershell
Copy-Item .env.example .env
```

Then fill in `.env`:

| Variable | Needed for | Notes |
|---|---|---|
| `GEMINI_API_KEY` | Gemini Flash assessment | From Google AI Studio. `GOOGLE_API_KEY` works too |
| `GESIT_LLM_MODEL` | Gemini Flash assessment | Optional. Default `gemini-3.5-flash`, a fixed version |
| `GESIT_LLM_FALLBACK_MODEL` | Gemini Flash assessment | Optional, empty by default. A second Gemini model (for example `gemini-2.5-flash`), tried only when the main model runs out of output tokens twice |
| `AICORE_AUTH_URL` | SAP-RPT-1.6 on AI Core | Must start with `https://`. `/oauth/token` is added by the code |
| `AICORE_CLIENT_ID`, `AICORE_CLIENT_SECRET` | SAP-RPT-1.6 on AI Core | From the AI Core service key |
| `AICORE_BASE_URL` | SAP-RPT-1.6 on AI Core | The `AI_API_URL` of the service key, with or without `/v2` |
| `AICORE_RESOURCE_GROUP` | SAP-RPT-1.6 on AI Core | Default `default` |
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

Expected: `44 passed`. The tests never call the network: SAP-RPT and Gemini Flash are replaced by fakes.

| Test file | What it proves | Requirements |
|---|---|---|
| `tests/test_csv_adapter.py` | SAP field names and units, 5 open POs, 429 receipts, `_truth/` is refused | D-1, D-2 |
| `tests/test_reliability.py` | Prior Beta(2, 1), forgetting factor 0.97, 90% interval | FR-EVA-2, FR-EVA-3 |
| `tests/test_disruption.py` | The four flag rules, the 3% price threshold, stock-out date and gap | FR-DET-2, FR-DET-3 |
| `tests/test_rpt_client.py` | Request body, token caching, P(late) parsing, fallback to the posterior | FR-DET-1, FR-DET-4 |
| `tests/test_detection_agent.py` | Scenarios S1 to S3, urgency order, name masking, survival without the LLM | FR-DET-1 to FR-DET-4, PRD section 10 |

To run one file or one test:

```powershell
py -3.11 -m pytest tests/test_disruption.py -q
py -3.11 -m pytest tests/test_detection_agent.py -k s1 -q
```

## Step 4: Detection without credentials (offline)

This checks the deterministic part and the fallback (FR-DET-4). `--no-llm` skips Gemini Flash.
If `.env` already holds SAP credentials, rename it for this step or the real SAP-RPT is called.

```powershell
py -3.11 -m agents.run S1 --no-llm
```

Expected (simulated data):

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

Then the other triggers:

| Command | Expected flagged POs |
|---|---|
| `py -3.11 -m agents.run scan --no-llm` | none (no fallback P(late) reaches 0.5) |
| `py -3.11 -m agents.run S2 --no-llm` | 4500000432, price revision +5.9% (Rp17,650 to Rp18,700 per kg) |
| `py -3.11 -m agents.run S3 --no-llm` | 4500000433, capacity shortfall, 4,800 of 12,000 kg deliverable on 2026-10-01, 7,200 kg on 2026-10-11 (GAP +5) |
| `py -3.11 -m agents.run all --no-llm` | 4500000431, 4500000433, 4500000432, in that order |

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

## Step 6: Gemini Flash assessment (network)

Needs `GEMINI_API_KEY` in `.env`.

```powershell
py -3.11 -m agents.run S1
```

Pass when the output ends with an `Assessment (gemini-3.5-flash):` block that:

- names PO 4500000431 and supplier 100101 (by number, never by company name),
- says the goods arrive 5 days after the stock-out date 2026-10-06,
- is at most 120 words, in English, with no em dashes or en dashes,
- contains no number that is missing from the table above it,
- does not recommend a replacement supplier.

If it says `not available (reasoning model unavailable: ...)`:

| Message | Fix |
|---|---|
| `GEMINI_API_KEY is not set` | Add the key to `.env` (step 2) |
| `strands-agents[gemini] is not installed` | Step 1 |
| `ClientError` | Wrong or revoked key, an unknown model id in `GESIT_LLM_MODEL`, or the rate limit of the key was reached. Check the key in Google AI Studio and try a fixed id such as `gemini-2.5-flash` |
| `ModelThrottledException` | Rate limit reached; wait a minute and rerun |
| `MaxTokensReachedException` | The model used its whole output budget, mostly on thinking, before finishing the answer. The agent already retries with a larger budget (8,192, then 32,768 tokens). If it still fails, set `GESIT_LLM_FALLBACK_MODEL` in `.env`; the `Assessment (...)` line shows which model answered |
| `ServerError 503 Service Unavailable` | The Gemini model is overloaded. The agent already retries three times; rerun, and check that `GESIT_LLM_MODEL` is not set to `gemini-flash-latest`, which showed this error often on 9 October 2026 |

## Step 7: Full run and repeatability (network)

```powershell
py -3.11 -m agents.run all
py -3.11 -m agents.run S1 --json > "$env:TEMP\gesit_s1.json"
```

Check in the JSON:

- `PREDICTION.FALLBACK` is `false`,
- `STEPS` lists `get_open_pos`, `get_inventory`, `predict_delay_risk`, in that order,
- `FLAGGED_EBELN` is `["4500000431"]` plus any PO whose SAP-RPT `P_LATE` is 0.5 or more,
- `REASONING_ERROR` is `null`.

Then run `py -3.11 -m agents.run S1` ten times (NFR-6). The table and the flagged
list must be the same every time. The assessment wording may vary slightly; its
numbers must not.

## What to report back

After steps 5 to 7, update `WORKING.md` (status of section 7.2, done log) with:

1. SAP-RPT: worked or not, the `P_LATE` of the five open POs, call time.
2. Gemini Flash: worked or not, and which model id was used.
3. Any scenario whose flagged list differs from step 4, and why (a real SAP-RPT `P_LATE` at or above 0.5 adds a "PREDICTED_LATE" flag, which is expected behaviour).

## Not covered yet

Supplier Evaluation, Action, Integrity Guard, the supervisor, Cedar policies and
the Joule UI do not exist yet, so there is nothing to test for them. Add a
section here when each one is built.
