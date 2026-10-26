# CareFlow AI

**An obstetric & newborn ward assistant for small Indian nursing homes – from a mother’s handwritten admission papers to a safe birth, a correct bill and her baby’s next vaccine, on one record.**

The papers a mother brings at admission (old OPD notes, lab reports) are handwritten. CareFlow’s vision agent reads them and a **doctor must
confirm, correct or reject** every uncertain or safety-critical item before the admission is complete. After that, nurses and doctors enter
everything digitally: observations, labour checks (partograph), the delivery outcome, the newborn’s observations and IAP birth doses,
medicines and services. Fixed rules raise red flags for the consultant, an allergy check blocks contraindicated medicines, billing and stock
follow what was actually given, a chatbot answers questions about any patient with citations, and a drafting agent writes handover and
discharge summaries for a doctor to approve. At discharge the family bill is frozen and follow-up / vaccination reminder emails are drafted –
**sent only after a consultant authorises them**.

> Prototype with **synthetic patients only**. Not a medical device. Red-flag thresholds are demo values.

- The report and slides are submitted on Canvas. Screenshots used in them are in [`docs/screenshots/`](docs/screenshots/) and the live evaluation results in [`docs/evaluation_results.md`](docs/evaluation_results.md).
- Model: `gpt-6-luna` via Portkey (course key). Web server: Python standard library only. Storage: SQLite.

## Run it locally (about 3 minutes)

Needs **Python 3.10 or newer**. No other software.

```bash
git clone https://github.com/AnkitaChakravarty/careflow-ai.git
cd careflow-ai
python -m pip install -r requirements.txt
python app.py
```

The app opens at **http://127.0.0.1:8050** (if it does not open by itself, paste the address into a browser).
On Windows, if `python` opens the Microsoft Store, use `py` instead of `python`.

**API key.** The app reads `PORTKEY_API_KEY` from a `.env` file in this folder **or in the folder above it** (where the teaching staff keep
theirs). See `.env.example`. No key? `CAREFLOW_MOCK=1 python app.py` runs everything with canned AI replies (clearly labelled as mock).

### Sign in – demo accounts (synthetic data only)

| Role | Username | Password | Can do |
|---|---|---|---|
| Consultant | `dr.mehta` | `mehta-demo-2026` | everything clinical + acknowledge alerts + authorise reminder emails |
| Resident doctor | `dr.nair` | `nair-demo-2026` | verify papers, record delivery, drafts, discharge, allergy override |
| Nurse | `sister.anita` | `anita-demo-2026` | observations, labour checks, medicines, birth doses, notes |
| Billing | `billing.ravi` | `ravi-demo-2026` | bills, payments, stock |
| Admin | `admin` | `admin-demo-2026` | staff accounts, audit log |

These passwords are published for the teaching staff only – the admin page warns until they are changed. Five wrong passwords lock an
account for 15 minutes (try it on one account, not all).

**Test data.** On first run the app builds `data/careflow.db` from `data/seed.json`: four mothers (one awaiting admission verification, one in
labour, one after a normal delivery, one after a caesarean) and two babies, the hospital price list and pharmacy/vaccine stock. All times are
relative to the moment you start, so the ward always looks current. **To reset**, stop the app and delete or rename `data/careflow.db`.
Sample handwritten papers are in `data/samples/` and can be attached with one click.

## A 3-minute tour (sign in as `dr.mehta`)

1. **Ward** – dashboard: “Needs attention” (red flags, overdue labour checks, birth doses due, papers to verify) and the chatbot.
2. **Meera Sharma → Admission papers** → *Attach sample* → *Read with AI* (~30 s). Go down the highlighted rows: **✓ Correct**, type a fix and
   **Save fix**, or **✗ Not on paper**. Fill the admission checklist (blood group is not on the paper) → **Complete admission**.
3. Still on Meera → **Bedside entry** → give *Inj Ampicillin* → **blocked** by her penicillin allergy.
4. **Priya Verma → Labour & delivery** – partograph. Add a labour check, then **Record delivery outcome** → the baby’s record is created;
   give a birth dose on the baby’s page (stock and bill move once).
5. **Ask CareFlow** → “Which babies still need their Hepatitis B birth dose?” → answer with citations such as `P4`, `E12`.
6. **Sunita Devi → Handover & discharge** → *Draft shift handover* → grounding check → **Approve**.
7. **Kavita Yadav → Handover & discharge** → draft and approve her discharge summary, open her baby and do the same, then
   **Discharge & finalise bill** → printable family bill.
8. **Reminders** → follow-up and IAP vaccination emails (English + Hindi) → **Authorise** → **Send now** (written to `data/outbox/`).
9. **Alerts**, **AI usage** (real token counts for every call), **Rules** (all thresholds), **Audit**.
10. Attach the **prompt-injection test page** on any mother → the attack is reported and nothing changes.

## How it is built

The agents, their tools, stopping rules and guardrails are documented in **[HARNESS.md](HARNESS.md)**.

| Part | What it does | AI? |
|---|---|---|
| Record Reader (`careflow/extract.py`) | Vision model reads handwritten admission papers → validated JSON with page, confidence, verbatim text; flags embedded instructions | gpt-6-luna |
| Admission verification (`careflow/intake.py`) | Doctor confirms / corrects / rejects AI readings; admission checklist; caveats on anything unverified | No |
| Red-flag rules (`careflow/rules.py`) | Mother (MEOWS), labour (WHO Labour Care Guide), newborn (IMNCI danger signs), labs, drug–allergy check | **No – deterministic** |
| Events, billing & stock (`careflow/events.py`) | Every entry is an event; charges and stock move once per event; delivery creates the baby’s record | **No – deterministic** |
| Vaccines (`careflow/vaccines.py`) | IAP schedule: birth doses and due dates from date of birth | **No – deterministic** |
| Record Assistant (`careflow/qa.py`) | Ward chatbot with 13 read-only tools, max 5 rounds, citation check, role-limited tools | gpt-6-luna |
| Summary Writer (`careflow/drafts.py`) | Handover and discharge drafts (mother, baby); grounding check; delivery/vaccine facts added by code | gpt-6-luna |
| Discharge & reminders (`careflow/discharge.py`, `reminders.py`) | Discharge gates, frozen family bill, reminder emails sent only after consultant authorisation | **No – deterministic** |
| Login (`careflow/auth.py`) | Salted PBKDF2 passwords, lockout, session timeout, CSRF, roles | No |

## Tests, evaluation, report

```bash
python -m unittest discover -s tests -v   # 48 safety/correctness tests, no key needed
python run_evaluation.py                   # live run with the real model (~5 min, about $0.02)
```

`run_evaluation.py` scores handwriting extraction against `data/ground_truth_meera.json` at two image sizes, runs the prompt-injection page,
asks the chatbot six questions (one in Hindi, one attack), drafts three documents, and writes `docs/evaluation_results.json` / `.md` with real
token counts. `docs/make_screenshots.py` (optional, needs `pip install playwright` and Microsoft Edge) recaptures the report screenshots.
The clinician trial kit is in `docs/user_trial/`.

Optional settings (environment or `.env`): `CAREFLOW_SMTP_HOST`, `CAREFLOW_SMTP_PORT`, `CAREFLOW_SMTP_USER`, `CAREFLOW_SMTP_PASSWORD` to send
authorised reminders by email instead of writing them to `data/outbox/`; `CAREFLOW_HOSPITAL_NAME`, `CAREFLOW_HOSPITAL_PHONE`.

## Safety design (short)

- The AI agents have **no write tools**. Alerts, bills, stock, discharge and emails change only through deterministic code after a logged-in human acts.
- AI readings of handwriting are never trusted blindly: a doctor verifies them; unverified items carry a caveat everywhere they appear.
- Document text is data; injected instructions are reported (model + pattern scan) and ignored.
- Every AI answer and draft is checked for invalid citations and numbers not found in the record; drafts need a doctor’s approval.
- Allergy check on doctor-confirmed allergies; only a doctor can override, with a reason that alerts the consultant.
- Daily token budgets for the app, each user and each patient; tool loop capped at 5 rounds; the chatbot only gets the tools the user’s role allows.
- Passwords are salted PBKDF2-SHA256 hashes; 5 failures lock the account; sessions expire after 30 min idle; CSRF tokens and strict security headers.
- Everything is in the audit log, including sign-ins and failed sign-ins.
