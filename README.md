# CareFlow AI

**An obstetric ward assistant that closes the loop from handwritten notes to bedside alerts, handover, discharge and billing.**

A doctor taking over a shift in a small Indian nursing home inherits a stack of handwritten OPD and progress notes.
CareFlow AI reads those notes into a structured record (every field with its source page and a confidence flag), takes
bedside observations from nurses, raises red-flag alerts for the consultant using fixed MEOWS-based rules, answers the
doctor's questions with citations, drafts the shift handover and discharge summary for clinician approval, and keeps the
bill and medicine stock in step with what was actually given.

> Prototype with **synthetic patients only**. Not a medical device. Red-flag thresholds are demo values.

## Run it locally (about 3 minutes)

Needs Python 3.10 or newer.

```bash
git clone <this repo>
cd careflow-ai
python -m pip install -r requirements.txt
python app.py
```

The app opens at **http://127.0.0.1:8050**. (On Windows, if `python` opens the Microsoft Store, use `py` instead.)

**API key.** The app reads `PORTKEY_API_KEY` from a `.env` file in this folder, or in the folder **above** this one
(where the teaching staff keep theirs). See `.env.example`. Model: `gpt-6-luna` via Portkey.

**Test data.** On first run the app creates `data/careflow.db` from `data/seed.json`: three synthetic patients, an INR tariff
and a pharmacy stock list. Delete `data/careflow.db` to reset. The sample handwritten note and a prompt-injection test page
are in `data/samples/` and can be attached from the patient page with one click.

No key? `CAREFLOW_MOCK=1 python app.py` runs the UI with canned AI replies (clearly labelled as mock).

## A 2-minute tour

1. **Ward** → open *Meera Sharma*.
2. **1 · Scanned notes** → *Attach sample* → *Read with AI*. The handwritten note becomes ~30 structured fields with page
   references; uncertain ones (e.g. “? pre-eclampsia”) are flagged for review. BP 160/110 written in the note goes through the
   red-flag rules and raises an alert.
3. **2 · Bedside entry** → enter BP 165/112 → a RED alert is raised instantly. Record *Inj Magnesium sulphate × 4* → stock
   drops by 4 and the bill rises (once, even if you resubmit).
4. **Acting as → Consultant** (top right) → **Consultant alerts** → *Acknowledge*.
5. **4 · Ask the record** → “Any drug allergies?” → answer with citations such as `D1.p1`.
6. **5 · Handover** → *Draft shift handover* → grounding check → approve with a clinician name.
7. Attach the **prompt-injection test page** → the attack is reported and nothing changes.
8. **AI usage & cost** shows real token counts for every call.

## How it is built

| Part | What it does | AI? |
|---|---|---|
| Record Reader (`careflow/extract.py`) | Vision model reads scanned pages → validated JSON with page, confidence, verbatim text; flags embedded instructions | gpt-6-luna |
| Red-flag engine (`careflow/rules.py`) | MEOWS-based thresholds; any red or ≥2 yellow → alert to consultant | **No – deterministic** |
| Billing & inventory (`careflow/events.py`) | Charges and stock move only on recorded administrations/services; idempotency keys stop duplicates | **No – deterministic** |
| Record Assistant (`careflow/qa.py`) | Tool-using Q&A over one patient's record, read-only tools, max 5 rounds, citation check | gpt-6-luna |
| Summary Writer (`careflow/drafts.py`) | Handover (SBAR) and discharge drafts; grounding check on every number and citation; clinician approval | gpt-6-luna |

Web server: Python standard library only (`app.py`). Storage: SQLite.

## Tests and evaluation

```bash
python -m unittest discover -s tests -v   # 19 safety/correctness tests, no key needed
python run_evaluation.py                   # live run with the real model (~3-5 min, < $0.02)
```

`run_evaluation.py` scores handwriting extraction against `data/ground_truth_meera.json` at two image sizes, runs the
prompt-injection page, asks five questions (one is an attack), drafts a handover, and writes
`docs/evaluation_results.json` / `.md` with real token counts.

## Safety design (short)

- The AI has **no write tools**. Alerts, bills, stock and discharge status are changed only by deterministic code after a human action.
- Document text is treated as data; injected instructions are reported (model + pattern scan) and ignored.
- Every AI answer and draft is checked for invalid citations and numbers not found in the record.
- Daily token budget for the app and per patient; tool loop capped at 5 rounds; uploads limited in size and pages.
- Only a Consultant can acknowledge alerts; only a named doctor can approve a draft. Everything is in the audit log.
