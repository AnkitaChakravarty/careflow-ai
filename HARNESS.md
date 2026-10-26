# AI Agent Harness

This harness controls the three AI agents in CareFlow AI. All of them use `gpt-6-luna` through Portkey, read the ward record
through code, and **none of them can change anything**: alerts, bills, stock, discharge and emails are changed only by deterministic
code after a logged-in human acts. Every model call is logged with real token counts (AI usage page), and every action is in the audit log.

| Agent | Prompt lives in | Called by | Output |
|---|---|---|---|
| **Record Reader** (vision) | `SYSTEM` in `careflow/extract.py` | “Read with AI” on an admission paper | Validated JSON: every field with page, confidence and verbatim text |
| **Record Assistant** (tool-using chat) | `SYSTEM` in `careflow/qa.py` | “Ask CareFlow” chat (dashboard, patient page, `/assistant`) | Short answer with a citation after every fact |
| **Summary Writer** | `SYSTEM` and `KINDS` in `careflow/drafts.py` | “Draft shift handover / discharge summary” | Sectioned JSON draft with citations, waiting for a doctor’s approval |

The agents never call each other. Ordinary code and people orchestrate them: the Reader’s output waits for a doctor’s verification;
the Assistant and the Writer only read the verified record.

## 1. Tools

Only the **Record Assistant** has tools (`TOOLS` in `careflow/qa.py`; the code behind them is in `careflow/record.py`). All are
**read-only** SQL views of the ward record. Every item they return carries a citation ID: `P5` patient record, `E12` ward entry,
`A3` alert, `D2.p1` admission paper page.

| Tool | What it does | Input | Returns |
|---|---|---|---|
| `list_patients` | All mothers and babies on the ward | `include_discharged` (optional) | patient_id, name, bed, phase, pending alerts |
| `get_ward_due_tasks` | Everything due now, computed by code (same list as the dashboard) | none | Birth doses not given, overdue labour checks, papers to verify, alerts awaiting review |
| `get_ward_alerts` | Red-flag alerts across the ward | `pending_only` (optional) | Level, category, reasons, acknowledgement |
| `get_patient_overview` | Demographics, doctor-confirmed allergies, blood group, G/P, phase, babies or mother | `patient_id` | Overview + verification status of AI-read papers |
| `get_admission_papers` | Fields the AI read from handwritten papers | `patient_id`, `section` | Values with “verified” or “UNVERIFIED AI reading” (rejected readings are never returned) |
| `get_observations` | Latest maternal vital signs or newborn observations | `patient_id`, `last_n` (1–50) | Observations, oldest first |
| `get_labour` | Partograph entries | `patient_id` | Dilatation, contractions, fetal heart, liquor, moulding |
| `get_delivery` | Delivery outcome | `patient_id` | Mode, outcome, sex, weight, APGAR, blood loss |
| `get_vaccinations` | Baby’s birth doses and next IAP visits | `patient_id` | Given/due/overdue doses, due dates |
| `get_medicines_given` | Medicines and vaccines actually administered | `patient_id` | Nursing administration records |
| `get_notes_labs_services` | Notes, lab results, services | `patient_id` | Entries with author and time |
| `get_patient_alerts` | Alerts for one patient | `patient_id` | Alerts and who acknowledged them |
| `get_bill_summary` | Running bill | `patient_id` | Bill lines and total — **only offered to roles that may see bills** (not nurses) |

The **Record Reader** and **Summary Writer** have **no tools**: the Reader receives page images, the Writer receives the patient’s
record as JSON (built by `record.full`).

## 2. Stopping Rules

- **Chat:** at most **5 tool rounds** per question (`MAX_AGENT_STEPS`); then it stops and asks for a narrower question.
  Duplicate tool calls in one round are skipped. Questions are cut to 1,000 characters; only the last 6 chat messages are sent as history;
  each tool result is cut to 20,000 characters; answers are capped at 1,500 output tokens.
- **Record Reader:** one model call per document (6,000 output tokens max); if the JSON is invalid it retries **once**, then stops with an error.
  Uploads are limited to 15 MB and 6 pages; images are downscaled (longest side 1,600 px by default, `CAREFLOW_IMAGE_MAX_SIDE`).
  Re-uploading an identical file reuses the earlier reading with **no model call**.
- **Summary Writer:** one model call per draft (3,000 output tokens max).
- **Budgets** (`careflow/llm.py`, checked before every call): 300,000 tokens per day for the whole app, 150,000 per user, 120,000 per patient.
  When a budget is reached, AI features pause and manual entry keeps working.
- Function tools run with `reasoning_effort="none"` (required by the model on this endpoint, and cheaper).
- If the provider’s safety filter blocks a request, the chat returns a refusal instead of an error; nothing is changed.

## 3. Guardrails

- **No write access.** No agent can create, change, send or delete anything. State changes (alerts, bills, stock, delivery, discharge,
  emails) happen only in deterministic code (`careflow/events.py`, `discharge.py`, `reminders.py`) after a logged-in human acts.
- **Verify before trust.** Every uncertain or safety-critical AI reading (name, blood group, allergies, diagnoses, medicines, lab results,
  obstetric dates) must be confirmed, corrected or rejected by a doctor before the admission is complete. Unverified readings carry a
  caveat in the record, the chatbot and the drafts.
- **Documents are data.** The Reader copies any instructions it finds in a paper into `embedded_instructions` instead of following them;
  a regex scan double-checks (`INJECTION_PATTERNS`). Tool results are also treated as data by the Assistant.
- **No invented facts.** The Reader must not add unwritten findings; “?” diagnoses stay queries. Code checks every citation in chat answers
  and every citation **and number** in drafts against the record and highlights anything unsupported. Delivery details, birth doses and IAP
  vaccination dates in discharge papers are filled in by code, not by the model.
- **Information, not orders.** The Assistant gives no diagnoses, doses or treatment decisions; it refers clinical decisions to the consultant.
- **Least privilege.** The chatbot only gets the tools the user’s role may see; roles are enforced on the server for every action
  (`PERMISSIONS` in `careflow/auth.py`).
- **Fixed safety rules.** Red flags (MEOWS, WHO Labour Care Guide, newborn danger signs, labs) and the drug–allergy check are deterministic
  (`careflow/rules.py`), never decided by AI. Only a doctor can override an allergy block, with a reason that alerts the consultant.
- **Humans sign off.** Drafts need a doctor’s approval; alerts need a consultant’s acknowledgement; reminder emails are sent only after a
  consultant authorises each one.
- **Accounts.** Salted PBKDF2-SHA256 passwords (600,000 iterations), lockout after 5 failures for 15 minutes, 30-minute idle timeout,
  CSRF tokens, strict security headers.

## 4. Records of each run

- Every model call: feature, patient, user, input / cached / output tokens, cost, latency, success (`usage` table → AI usage page).
- Every human action: sign-ins (and failures), events, verifications, approvals, acknowledgements, discharges, reminders (`audit` table → Audit page).
- Tests for these rules: `tests/test_safety.py` (48 tests, no API key needed). Live measurements: `run_evaluation.py` → `docs/evaluation_results.md`.
