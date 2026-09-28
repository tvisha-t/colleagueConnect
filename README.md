# DocSpot

DocSpot is a prototype built for the Impiricus challenge at HackGT 13. It helps a physician reach a colleague who isn't on Impiricus, including "digitally dark" physicians who ignore pharma email, apps, and portals, through the channel they still use: secure fax. The request carries only minimal information. The specialist responds by scanning a QR code on the fax, the patient consents by email, and only then is the patient's information released. Each accepted request is also an optional, separate invitation for the specialist to join Impiricus's verified physician network.

> **Prototype notice:** This is a demonstration, not a production clinical system. Patients, most physicians, and the telehealth and trial data are fictional. Real clinicians from the NPI Registry appear only as recommendations, and their responses in the demo are simulated. No fax provider is connected. Do not enter real patient information.

## The flow

1. **Find a specialist.** Choose a patient and get ranked physicians with reasons (relationship, treatment experience, accepting patients, distance). Rankings never use payments from drug companies.
2. **Minimal fax.** An AI drafts the fax from a general concern only (e.g. "inflammatory arthritis"): no name, date of birth, medications, or labs. A privacy check blocks sending if anything else appears. The physician must review and approve every fax.
3. **The fax arrives.** A simulated practice fax inbox shows the referral getting past the front desk (marketing faxes are discarded). The fax has a scannable QR code.
4. **The specialist responds.** After scanning the QR code, the specialist confirms their identity against their NPI record and accepts, asks for a call, or declines. Joining Impiricus is a separate, optional step with contact preferences (never SMS).
5. **Patient consent.** When the specialist accepts, an authorization form is generated. The referring physician reviews and approves it, and it's emailed to the patient. The patient confirms their date of birth, reads the form, and e-signs.
6. **Gated release.** Only after the patient signs is the approved information released to the specialist. The referring physician gets a notification, and every step is recorded in an audit trail.

## Features

- **Specialty-grouped network map** with the physician at the center and a list view.
- **Find a specialist** with explained recommendations. If `frontend/data/physicians.js` is present, real Atlanta clinicians from the NPI Registry appear as outside-network recommendations (tagged "NPI Registry").
- **Telehealth & clinical trials:** find specialists who offer telehealth or run clinical trials with travel support, for patients far from specialty care. Uses fictional sample data (`frontend/data/access_sample.json`); trial sponsorship and travel support are filters only and never affect ranking.
- **Ask a clinical question:** suggests who is best placed to answer (the AI recommends people, never answers the question). The privacy check is intentionally skipped for questions; the skip is recorded in the audit trail.
- **AI fax drafting** (OpenAI or Anthropic) with guardrails: the model receives only the request purpose and a broad concern; drafts that add numbers or clinical wording not in the input, or leave out the specialty and request, are rejected and retried once. Falls back to a template if the AI is off or slower than about 6 seconds.
- **Two-layer privacy check:** pattern rules (names, dates of birth, record numbers, phone numbers, addresses, medications, labs) plus an optional AI review. Flagged content must be removed before sending. The browser has its own copy of the rules for when the backend is off.
- **QR code on every fax**, generated in the browser.
- **Recipient walkthrough:** fax inbox, QR scan, identity check, response, optional join, and the new member's account.
- **Patient consent workflow** with physician review, email delivery, a date-of-birth gate, e-signature, and gated release (see below).
- **Requests list** grouped by what needs attention, with per-request steps and an audit trail.
- **Every request goes by fax**, including to physicians already in the network (they skip the join step).

## Repository layout

```text
frontend/
  index.html               Entry point (script order matters: main.js must be last)
  css/styles.css           Dark theme and component styles
  data/physicians.js       Real Atlanta clinicians from the NPI Registry (cleaned export)
  data/access_sample.json  Fictional telehealth and clinical-trial data (edit this)
  data/access.js           Generated from access_sample.json (loaded by the browser)
  js/data.js               Demo physicians and patients, NPI and access-data adapters
  js/logic.js              State, recommendations, draft/privacy/consent logic, request lifecycle
  js/views.js              HTML and SVG rendering
  js/main.js               Events, rendering, consent polling, startup
  js/api.js                Backend calls and the browser copy of the privacy rules
  vendor/qrcode.js         qrcode-generator (MIT), draws the fax QR code
backend/
  main.py                  FastAPI app: health, AI draft, privacy check
  consent.py               Consent form, email, patient signing page, status
  ai.py                    OpenAI / Anthropic wrapper
  privacy.py               Pattern-based privacy rules
  requirements.txt         Python dependencies
  .env.example             Configuration template (copy to .env)
pipeline/
  NPI_Processed (2).ipynb  Notebook used to clean the NPI Registry data
```

The frontend is plain HTML, CSS, and JavaScript with no build step.

## Run locally

### 1. Frontend

Open `frontend/index.html` with VS Code's **Live Server** (then go to `http://127.0.0.1:5500/frontend/index.html`), or serve the folder:

```bash
python3 -m http.server 5500 --directory frontend
```

and open <http://localhost:5500>. Opening `index.html` straight from disk also works. The app runs fully without the backend, using templates, browser-side privacy rules, and simulated consent.

> Demo data lives in the open page. Reloading the page resets the demo, so don't reload in the middle of a flow. **Reset demo** (top right) starts over on purpose.

### 2. Backend (optional, needed for AI, email, and the patient signing page)

```bash
cd backend
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env        # then fill in keys (see below)
.venv/bin/uvicorn main:app --reload --port 8000
```

Check it at <http://127.0.0.1:8000/health>. The frontend expects the backend on port 8000. The backend reads `.env` only at startup, so restart it after editing `.env`. If port 8000 is busy: `lsof -ti:8000 | xargs kill`.

### 3. Configuration (`backend/.env`, never committed)

```dotenv
# AI drafting and the AI privacy layer: set ONE (OpenAI is used if both are set)
OPENAI_API_KEY=
# OPENAI_MODEL=gpt-4.1-mini
ANTHROPIC_API_KEY=
# CLAUDE_MODEL=claude-opus-5

# Consent emails
DEMO_PATIENT_EMAIL=          # where every demo patient's consent email goes
RESEND_API_KEY=              # preferred sender (resend.com)
# RESEND_FROM=DocSpot for Dr. Anna Lee <onboarding@resend.dev>
SMTP_USER=                   # Gmail fallback: address + app password
SMTP_APP_PASSWORD=
# PUBLIC_BASE_URL=           # e.g. a tunnel address, so email links open on a phone
```

- **Resend** is the reliable option. Without a verified domain, Resend only delivers to the email address the Resend account was created with, so set `DEMO_PATIENT_EMAIL` to that address.
- **Gmail** needs 2-Step Verification and an app password. New Gmail accounts are often blocked by Gmail's spam checks (DKIM errors), which is why Resend is preferred.
- **No email configured:** the consent form still works. The request shows an **Open patient's form** button instead.
- Only put keys in `backend/.env`. `backend/.env.example` is committed to GitHub.

## Patient consent details

- The authorization form is modeled on the elements HIPAA lists for a valid authorization: who shares, who receives, what is shared, why, an expiration (90 days), the right to revoke, that treatment doesn't depend on signing, and possible re-disclosure. It has **not** been reviewed by a lawyer.
- The email contains no diagnosis or medical detail, only who is asking, why, and a link.
- The patient page releases the form only after the date of birth matches.
- Links point to `http://localhost:8000`, so open the email on the computer running the backend (or set `PUBLIC_BASE_URL`).
- Consent forms are kept in the backend's memory. Restarting the backend clears pending forms.
- If the backend is off, the review screen offers **Simulate sending** and the request offers **Simulate: patient signs**.

Demo patient dates of birth (for the signing page):

| Patient | Name | Date of birth |
| --- | --- | --- |
| M.R. | Maria Reyes | 04/12/1968 |
| J.T. | James Turner | 09/02/1962 |
| A.K. | Alice Kim | 01/30/1955 |
| D.L. | David Lowell | 06/18/1981 |
| S.P. | Sofia Price | 11/05/1992 |
| R.W. | Robert Wade | 03/22/1974 |

## Backend endpoints

| Endpoint | Purpose |
| --- | --- |
| `GET /health` | Backend status and the configured AI provider and model |
| `POST /ai/draft` | AI fax draft from structured, minimal request data, with guardrails |
| `POST /ai/privacy-check` | Pattern rules plus an optional AI review of a draft |
| `POST /consent/preview` | Fill in the authorization form for the physician to review |
| `POST /consent/send` | Create the form and email the patient (requires physician approval) |
| `GET /consent/{token}` | The patient's signing page |
| `POST /consent/{token}/verify` | Check the date of birth and return the form |
| `POST /consent/{token}/sign` | Record the patient's signature or refusal |
| `GET /consent/{token}/status` | Polled by the app to release information once signed |

## Data

- **NPI Registry** (`frontend/data/physicians.js`): 253 Atlanta records cleaned in `pipeline/`. The app keeps the 56 with a fax number and a specialty it uses. Format: [frontend/data/README.md](frontend/data/README.md).
- **Telehealth and clinical trials** (`frontend/data/access_sample.json`): fictional. Its `_meta.future_sources` lists the real datasets a production version would use (ClinicalTrials.gov, CMS, HRSA shortage areas, and licensed physician data providers). After editing the JSON, regenerate `access.js` from `frontend/data/`:

  ```bash
  python3 -c "import json; d=json.load(open('access_sample.json')); open('access.js','w').write('// GENERATED from access_sample.json. Edit the JSON, then regenerate (see data/README.md).\n// Fictional sample data for the telehealth and clinical-trial access feature.\nconst ACCESS_DATA = '+json.dumps(d,indent=1,ensure_ascii=False)+';\n')"
  ```

- **Demo patients and network** (`frontend/js/data.js`): fictional. Keep them fictional.

## Development notes

- Keep the browser privacy rules in `frontend/js/api.js` in step with `backend/privacy.py`.
- The backend allows cross-origin requests so the page can call it from another local port.
- Fax delivery, the recipient's responses, and NPI verification are simulated. The QR code points to a placeholder address.
- Never commit `backend/.env`, API keys, real patient information, or unreviewed exports with personal information.
