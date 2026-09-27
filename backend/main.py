"""DocSpot backend: AI features for the e-fax workflow (OpenAI or Claude).

Run from the backend/ folder:  uvicorn main:app --reload --port 8000
The API key lives in backend/.env (never in the browser).
"""
import re
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

from typing import List, Literal, Optional  # noqa: E402

from fastapi import FastAPI  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from pydantic import BaseModel, ConfigDict, Field  # noqa: E402

import ai  # noqa: E402
from privacy import pattern_check  # noqa: E402
from consent import router as consent_router  # noqa: E402

app = FastAPI(title="DocSpot AI")
# Local prototype: the page is opened from Live Server or straight from disk
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.include_router(consent_router)


@app.get("/health")
def health():
    return {"ok": True, "ai": ai.available(), "provider": ai.provider(), "model": ai.model()}


# ---------- Draft ----------

class Person(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    specialty: str = ""
    practice: str = ""
    city: str = ""
    npi: str = ""


class DraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["referral", "connect"]
    purpose: Literal["specialty_evaluation", "telehealth_evaluation", "clinical_trial_eligibility", "connection_invitation"]
    concern: Optional[Literal[
        "inflammatory_arthritis", "type_2_diabetes", "atrial_fibrillation",
        "kidney_disease", "plaque_psoriasis", "chronic_migraine", "crohns_disease", "specialty_concern",
    ]] = None
    channel: Literal["fax", "in-app"] = "fax"
    recipient: Person
    recipient_on_impiricus: bool = False
    sender: Person
    # No free-text topic or patient record is accepted by the AI draft endpoint.


DRAFT_SYSTEM = """Write concise, warm, collegial physician-to-physician referral or connection correspondence. The sender reviews every draft before sending.
Start with the recipient greeting on its own line and a blank line. Write only the body, without a sign-off; the app adds the signature and response instructions.
Under 120 words. For a referral, use only the recipient specialty and the supplied broad concern. Politely ask for an assessment and recommendations for appropriate next steps. For a connection request, invite a professional connection.
Never include or infer a patient name, initials, ID, age, date, address, medication, lab, symptom, history, or detailed diagnosis. Never add clinical advice or treatment recommendations. Use no facts beyond the structured request. Plain text, no markdown."""

TYPE_ASK = {
    "referral": "a referral asking the recipient to evaluate the patient",
    "connect": "an invitation to connect on DocSpot, a secure way for physicians to share referrals and questions",
}
PURPOSE_TEXT = {
    "specialty_evaluation": "specialty evaluation",
    "telehealth_evaluation": "telehealth evaluation",
    "clinical_trial_eligibility": "clinical trial eligibility information",
    "connection_invitation": "a professional connection",
}
CONCERN_TEXT = {
    "inflammatory_arthritis": "inflammatory arthritis",
    "type_2_diabetes": "type 2 diabetes",
    "atrial_fibrillation": "atrial fibrillation",
    "kidney_disease": "kidney disease",
    "plaque_psoriasis": "plaque psoriasis",
    "chronic_migraine": "chronic migraine",
    "crohns_disease": "Crohn's disease",
    "specialty_concern": "a concern requiring specialty care",
}
def _numbers(s):
    return set(re.findall(r"\d+(?:\.\d+)?", s))


# Words that describe a patient's condition. A draft may use one only if the physician's
# information already contains it; otherwise the AI made it up ("experiencing symptoms consistent with...").
CLINICAL_WORDS = re.compile(
    r"\b(symptom\w*|experienc\w*|consistent with|present(?:s|ed|ing)? with|complain\w*|report(?:s|ed|ing)?|"
    r"worsen\w*|improv\w*|progress\w*|flare\w*|histor\w*|severe|severity|mild|chronic|acute|"
    r"pain\w*|swell\w*|stiff\w*|fatigue\w*|persistent|uncontrolled|poorly controlled|tolerat\w*|"
    r"side effects?|adverse|respond\w* (?:to|well|poorly)|onset|likely|suggest\w*|indicat\w*)\b",
    re.I,
)


def _invented_clinical(body, source):
    src = source.lower()
    found = []
    for m in CLINICAL_WORDS.finditer(body):
        word = m.group(0).lower()
        stem = re.sub(r"(s|es|ed|ing|ly)$", "", word.split()[0])[:6]
        if stem not in src and word not in found:
            found.append(word)
    return found


def _draft_problem(body, source, req):
    """Guardrails. Returns why a draft is unacceptable, or None if it's fine."""
    if req.type == "referral":
        if req.recipient.specialty.strip().lower() not in body.lower():
            return "did not mention the recipient specialty"
        concern = CONCERN_TEXT.get(req.concern or "")
        if not concern or concern.lower() not in body.lower():
            return "did not mention the selected broad concern"
        if not re.search(r"\b(assess\w*|evaluation|evaluate\w*)\b", body, re.I):
            return "did not request a specialty assessment"
        if not re.search(r"\b(recommend\w*|next steps?)\b", body, re.I):
            return "did not request recommendations for next steps"
        if "please" not in body.lower():
            return "did not phrase the request courteously"
    extra = _numbers(body) - _numbers(source)
    if extra:
        return f"added numbers not in the input ({', '.join(sorted(extra))})"
    invented = _invented_clinical(body, source)
    if invented:
        return f"described the patient beyond the given information ({', '.join(invented)})"
    return None


def _signature(s: Person):
    return f"Thank you,\n{s.name}, {s.specialty}\n{s.practice}, {s.city}\nNPI {s.npi}"


def _format_salutation(body):
    body = body.strip()
    match = re.match(r"^([^,\r\n]+,)[ \t]*(?:\r?\n[ \t]*)?(.*)$", body, re.S)
    if match and match.group(2):
        return f"{match.group(1)}\n\n{match.group(2).lstrip()}"
    return body


@app.post("/ai/draft")
def draft(req: DraftRequest):
    if req.type not in TYPE_ASK:
        return {"text": None, "reason": "unknown type"}
    if req.type == "connect" and (req.purpose != "connection_invitation" or req.concern):
        return {"text": None, "reason": "connection invitations require a connection purpose and no patient concern"}
    if req.type == "referral" and (req.purpose == "connection_invitation" or not req.concern):
        return {"text": None, "reason": "referrals require an evaluation purpose and broad concern"}
    if not ai.available():
        return {"text": None, "reason": "no API key"}
    user = (
        f"Write {TYPE_ASK[req.type]}.\n"
        f"Delivery: {'secure fax' if req.channel == 'fax' else 'secure in-app message'}\n"
        f"Request purpose: {PURPOSE_TEXT[req.purpose]}.\n"
        + (f"Recipient specialty: {req.recipient.specialty}.\n" if req.type == "referral" else "")
        + (f"Broad concern: {CONCERN_TEXT[req.concern]}.\n" if req.concern else "")
        + f"From: {req.sender.name}, {req.sender.specialty}, {req.sender.practice}\n"
        f"To: {req.recipient.name}, {req.recipient.specialty}\n"
        "No patient identifiers or detailed patient information are provided."
    )
    # The frontend waits about 6s. If a draft fails a guardrail and there's time left, retry once with the reason.
    start, prompt, problem = time.monotonic(), user, None
    for attempt in range(2):
        left = 5.5 - (time.monotonic() - start)
        if attempt and left < 2:
            break
        body = ai.ask_text(DRAFT_SYSTEM, prompt, timeout=left)
        if not body:
            return {"text": None, "reason": "AI unavailable or too slow"}
        problem = _draft_problem(body, user, req)
        if not problem:
            break
        prompt = (f"{user}\n\nYour previous draft was rejected because it {problem}. "
                  "Rewrite it using only the facts above, in their own wording.")
    if problem:
        return {"text": None, "reason": f"draft {problem}"}
    # Every request goes by fax, so every draft ends with how to reply
    closing = "\n\nTo reply, scan the QR code on this fax. No account needed."
    return {"text": f"{_format_salutation(body)}{closing}\n\n{_signature(req.sender)}", "model": ai.model()}


# ---------- Privacy check ----------

class Patient(BaseModel):
    """Mock patient record from the prototype (fictional data only)."""
    model_config = ConfigDict(extra="forbid")
    name: Optional[str] = None
    initials: Optional[str] = None
    dob: Optional[str] = None
    mrn: Optional[str] = None
    age: Optional[int] = None
    sex: Optional[str] = None
    dx: List[str] = Field(default_factory=list)
    meds: List[str] = Field(default_factory=list)
    labs: List[str] = Field(default_factory=list)
    general_concern: Optional[Literal[
        "inflammatory_arthritis", "type_2_diabetes", "atrial_fibrillation",
        "kidney_disease", "plaque_psoriasis", "chronic_migraine", "crohns_disease", "specialty_concern",
    ]] = None


class PrivacyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str
    patient: Optional[Patient] = None
    approved: List[str] = Field(default_factory=list)  # currently only the broad concern can be approved
    topic: str = ""
    allowed: List[str] = Field(default_factory=list)  # sender/recipient contact details


PRIVACY_SYSTEM = """Review a physician-to-physician fax draft. Your only job is to flag text that must not be sent; never give clinical advice.
Always flag patient names, initials, date of birth, record number, age, contact details, and any unique identifier.
The only patient clinical information allowed is the selected broad concern supplied as approved. Always flag medications, doses, labs, symptoms, history, detailed diagnoses, or other patient-specific clinical details, even if the draft or topic includes them.
Do not treat free-text topic as permission to include patient information. Do not flag the physicians' professional names, practice details, signature, or recipient details.
Each flag's text must be copied exactly from the draft. Return an empty list if no prohibited text appears."""

PRIVACY_SCHEMA = {
    "type": "object",
    "properties": {
        "flags": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"text": {"type": "string"}, "reason": {"type": "string"}},
                "required": ["text", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["flags"],
    "additionalProperties": False,
}

def _approved_summary(req):
    """Share only the selected broad concern with the privacy-model prompt."""
    p, lines = req.patient, []
    if p and "dx" in req.approved and p.general_concern:
        lines.append("Selected broad concern: " + CONCERN_TEXT[p.general_concern])
    not_approved = ["all patient identifiers", "age and sex", "detailed diagnoses", "medications", "labs"] if p else []
    return lines, not_approved


@app.post("/ai/privacy-check")
def privacy_check(req: PrivacyRequest):
    patient = req.patient.model_dump() if req.patient else None
    flags = [dict(f, source="pattern") for f in pattern_check(req.text, patient, req.approved, req.topic, req.allowed)]
    used_ai = False
    if ai.available():
        lines, not_approved = _approved_summary(req)
        user = (
            f"Topic context (not permission to include patient details): {req.topic or 'none'}\n"
            f"Approved broad concern:\n" + ("\n".join(lines) or "none") + "\n"
            f"Not approved for sharing: {', '.join(not_approved) or 'nothing'}\n\n"
            f"Draft:\n<<<\n{req.text}\n>>>"
        )
        out = ai.ask_json(PRIVACY_SYSTEM, user, PRIVACY_SCHEMA, timeout=8)
        if out is not None:
            used_ai = True
            have = {f["text"].lower() for f in flags}
            for f in out.get("flags", []):
                t = (f.get("text") or "").strip()
                # Keep only flags that quote the draft exactly and that the patterns didn't already catch
                if t and t in req.text and t.lower() not in have and not any(t.lower() in h or h in t.lower() for h in have):
                    flags.append({"text": t, "reason": f.get("reason", "Flagged by the AI check."), "kind": "ai", "source": "ai"})
                    have.add(t.lower())
    return {"flags": flags, "mode": "ai" if used_ai else "patterns"}


# Serve the app itself, so one deployed service hosts both the page and the API.
# Mounted last: the API routes above take priority over these files.
FRONTEND = Path(__file__).resolve().parent.parent / "frontend"
if FRONTEND.is_dir():
    app.mount("/", StaticFiles(directory=FRONTEND, html=True), name="frontend")
