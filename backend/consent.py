"""Patient consent: form, email, signing page, and status.

Flow: the specialist accepts -> Dr. Lee reviews the auto-filled form -> /consent/send emails the
patient a link -> the patient confirms their date of birth and signs at /consent/{token} ->
the app polls /consent/{token}/status and releases the approved information only once signed.

Email settings live in backend/.env (never committed):
  RESEND_API_KEY=a Resend API key (preferred: properly authenticated, so Gmail delivers it)
  RESEND_FROM=optional sender, default "DocSpot for Dr. Anna Lee <onboarding@resend.dev>"
  SMTP_USER=the Gmail address that sends (fallback, needs an app password)
  SMTP_APP_PASSWORD=the 16-character Gmail app password
  DEMO_PATIENT_EMAIL=where every demo patient's consent email goes
  PUBLIC_BASE_URL=optional, e.g. a tunnel address, so the link works on a phone
Without SMTP settings the form is still created, and the app shows the link instead of emailing it.
"""
import html
import json
import logging
import os
import secrets
import smtplib
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from email.message import EmailMessage
from typing import Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

log = logging.getLogger("consent")
router = APIRouter()
CONSENTS: Dict[str, dict] = {}  # in memory: fine for a demo, cleared when the server restarts

PURPOSE_TEXT = {
    "specialty_evaluation": "a specialist evaluation",
    "telehealth_evaluation": "a telehealth evaluation",
    "clinical_trial_eligibility": "an evaluation that includes checking eligibility for a clinical trial",
}


class Doctor(BaseModel):
    name: str
    specialty: str = ""
    practice: str = ""
    city: str = ""
    npi: str = ""
    fax: str = ""
    phone: str = ""


class ConsentRequest(BaseModel):
    request_id: str
    patient_name: str
    patient_dob: str
    sender: Doctor
    recipient: Doctor
    purpose: str = "specialty_evaluation"
    released: List[str]          # what the patient is agreeing to release, in plain words
    approved_by_physician: bool = False


def _esc(s):
    return html.escape(str(s or ""))


def _base_url(request: Request):
    """Where the patient's link should point: PUBLIC_BASE_URL if set, otherwise the site that received the request."""
    base = os.getenv("PUBLIC_BASE_URL") or str(request.base_url)
    # "localhost" rather than a bare IP address: email filters treat links to raw IPs as phishing
    return base.replace("//127.0.0.1", "//localhost").rstrip("/")


def _mask(email):
    if not email or "@" not in email:
        return None
    name, domain = email.split("@", 1)
    return f"{name[0]}{'*' * max(2, len(name) - 1)}@{domain}"


def form_html(c: ConsentRequest, signed: Optional[dict] = None):
    """The authorization text, modeled on the elements HIPAA lists for a valid authorization."""
    s, r = c.sender, c.recipient
    purpose = PURPOSE_TEXT.get(c.purpose, "a specialist evaluation")
    items = "".join(f"<li>{_esc(x)}</li>" for x in c.released)
    return f"""
<h2>Authorization to share health information</h2>
<p><b>Patient:</b> {_esc(c.patient_name)}</p>
<h3>1. Who is sharing your information</h3>
<p>{_esc(s.name)}, {_esc(s.specialty)}, {_esc(s.practice)}, {_esc(s.city)}. NPI {_esc(s.npi)}.</p>
<h3>2. Who will receive it</h3>
<p>{_esc(r.name)}, {_esc(r.specialty)}{', ' + _esc(r.practice) if r.practice else ''}. NPI {_esc(r.npi) or 'on file'}.</p>
<h3>3. What will be shared</h3>
<ul>{items}</ul>
<h3>4. Why</h3>
<p>To support your care in {_esc(r.specialty)}: {purpose}, requested by {_esc(s.name)} and accepted by {_esc(r.name)}.</p>
<h3>5. How long this lasts</h3>
<p>This authorization expires 90 days after you sign it, or sooner if you revoke it.</p>
<h3>6. Your rights</h3>
<ul>
<li>You may revoke this authorization at any time by contacting {_esc(s.name)}'s office in writing{(' (fax ' + _esc(s.fax) + ')') if s.fax else ''}. Revoking does not undo sharing that already happened.</li>
<li>Your treatment by {_esc(s.name)} does not depend on whether you sign.</li>
<li>Once shared, the receiving physician's office may be able to share the information again, and it may no longer be protected by federal privacy law.</li>
<li>You may ask for a copy of this form.</li>
</ul>
{f'<p class="signed">Signed electronically by <b>{_esc(signed["name"])}</b> on {_esc(signed["at"])}.</p>' if signed else ''}
"""


def _email_ready():
    return bool(os.getenv("DEMO_PATIENT_EMAIL") and (os.getenv("RESEND_API_KEY") or (os.getenv("SMTP_USER") and os.getenv("SMTP_APP_PASSWORD"))))


def _send_resend(to_addr, subject, text_body, html_body):
    """Resend's email API. Without a verified domain, it only delivers to the account owner's address."""
    sender = os.getenv("RESEND_FROM", "DocSpot for Dr. Anna Lee <onboarding@resend.dev>")
    data = json.dumps({"from": sender, "to": [to_addr], "subject": subject, "html": html_body, "text": text_body}).encode()
    req = urllib.request.Request("https://api.resend.com/emails", data=data, method="POST", headers={
        "Authorization": f"Bearer {os.getenv('RESEND_API_KEY')}", "Content-Type": "application/json",
        "User-Agent": "docspot/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as res:
            return True, None
    except urllib.error.HTTPError as e:
        log.warning("Resend error %s: %s", e.code, e.read().decode(errors="replace")[:300])
        return False, "email could not be sent"
    except (urllib.error.URLError, OSError) as e:
        log.warning("Could not reach Resend: %s", e)
        return False, "email could not be sent"


def _send_email(to_addr, subject, text_body, html_body):
    if not to_addr:
        return False, "email not configured"
    if os.getenv("RESEND_API_KEY"):
        return _send_resend(to_addr, subject, text_body, html_body)
    user, pw = os.getenv("SMTP_USER"), os.getenv("SMTP_APP_PASSWORD")
    if not (user and pw and to_addr):
        return False, "email not configured"
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"DocSpot for Dr. Anna Lee <{user}>"
    msg["To"] = to_addr
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=15) as smtp:
            smtp.login(user, pw.replace(" ", ""))
            smtp.send_message(msg)
        return True, None
    except (smtplib.SMTPException, OSError) as e:
        log.warning("Consent email failed: %s", e)
        return False, "email could not be sent"


@router.post("/consent/preview")
def preview(c: ConsentRequest):
    return {"html": form_html(c), "to": _mask(os.getenv("DEMO_PATIENT_EMAIL")),
            "email_ready": _email_ready()}


@router.post("/consent/send")
def send(c: ConsentRequest, request: Request):
    # Step 3 of the spec: the sending physician must approve the form before it goes out.
    if not c.approved_by_physician:
        raise HTTPException(400, "The sending physician must review and approve the consent form first.")
    token = secrets.token_urlsafe(16)
    link = f"{_base_url(request)}/consent/{token}"
    s, r = c.sender, c.recipient
    # The email carries no diagnosis or medical detail: just who, why, and a link to the secure form.
    subject = f"Please review: sharing your health information with {r.name}"
    text = (f"Hello,\n\n{s.name} ({s.practice}) would like to share your health information with {r.name} "
            f"to support your care in {r.specialty}.\n\nReview and sign the secure authorization form:\n{link}\n\n"
            f"For your privacy, the form asks for your date of birth before it opens. Nothing is shared unless you sign.\n\n"
            f"Questions? Call {s.name}'s office{(' at ' + s.phone) if s.phone else ''}.\n\nDocSpot, for {s.practice}")
    body = (f"<p>Hello,</p><p>{_esc(s.name)} ({_esc(s.practice)}) would like to share your health information with "
            f"<b>{_esc(r.name)}</b> to support your care in <b>{_esc(r.specialty)}</b>.</p>"
            f"<p><a href=\"{link}\" style=\"background:#86A8FF;color:#0A1224;padding:10px 16px;border-radius:8px;text-decoration:none;font-weight:700\">Review and sign the form</a></p>"
            f"<p style=\"color:#555\">For your privacy, the form asks for your date of birth before it opens. Nothing is shared unless you sign.</p>"
            f"<p style=\"color:#555\">DocSpot, for {_esc(s.practice)}</p>")
    sent, err = _send_email(os.getenv("DEMO_PATIENT_EMAIL"), subject, text, body)
    CONSENTS[token] = {"req": c, "status": "sent", "created": datetime.now(), "signed": None, "tries": 0}
    return {"token": token, "link": link, "emailed": sent, "to": _mask(os.getenv("DEMO_PATIENT_EMAIL")) if sent else None, "error": err}


@router.get("/consent/{token}/status")
def status(token: str):
    rec = CONSENTS.get(token)
    if not rec:
        raise HTTPException(404, "Unknown consent form (the server may have restarted).")
    return {"status": rec["status"], "signed": rec["signed"]}


class VerifyRequest(BaseModel):
    dob: str


def _dob_ok(rec, dob):
    norm = lambda d: "".join(ch for ch in d if ch.isdigit())
    return norm(dob) == norm(rec["req"].patient_dob)


@router.post("/consent/{token}/verify")
def verify(token: str, v: VerifyRequest):
    """The form (which names the patient) is only sent to the browser after the date of birth matches."""
    rec = CONSENTS.get(token)
    if not rec or rec["status"] != "sent":
        raise HTTPException(404, "This form is no longer available.")
    if not _dob_ok(rec, v.dob):
        rec["tries"] += 1
        raise HTTPException(403, "That date of birth doesn't match our records.")
    return {"html": form_html(rec["req"])}


class SignRequest(BaseModel):
    dob: str
    name: str = ""
    decision: str  # "sign" | "decline"


@router.post("/consent/{token}/sign")
def sign(token: str, s: SignRequest):
    rec = CONSENTS.get(token)
    if not rec:
        raise HTTPException(404, "Unknown consent form.")
    if rec["status"] != "sent":
        return {"status": rec["status"]}
    if not _dob_ok(rec, s.dob):
        rec["tries"] += 1
        raise HTTPException(403, "That date of birth doesn't match our records.")
    if s.decision == "decline":
        rec["status"] = "declined"
        rec["signed"] = {"name": None, "at": datetime.now().strftime("%b %d, %Y %I:%M %p")}
        return {"status": "declined"}
    if not s.name.strip():
        raise HTTPException(400, "Type your full name to sign.")
    rec["status"] = "signed"
    rec["signed"] = {"name": s.name.strip(), "at": datetime.now().strftime("%b %d, %Y %I:%M %p"),
                     "expires": (datetime.now() + timedelta(days=90)).strftime("%b %d, %Y")}
    return {"status": "signed"}


@router.get("/consent/{token}", response_class=HTMLResponse)
def page(token: str):
    """The patient's secure signing page, opened from the email link."""
    rec = CONSENTS.get(token)
    if not rec:
        return HTMLResponse(PAGE.replace("{{BODY}}", "<h2>This link has expired</h2><p>Please contact your doctor's office for a new one.</p>"), 404)
    c = rec["req"]
    if rec["status"] != "sent":
        done = "You signed this authorization. Thank you." if rec["status"] == "signed" else "You chose not to authorize sharing. Nothing was shared."
        return PAGE.replace("{{BODY}}", f"<h2>All set</h2><p>{done}</p>")
    body = f"""
<div id="gate"><h2>Confirm it's you</h2>
<p>{_esc(c.sender.name)} sent you a form about sharing your health information. Enter your date of birth to open it.</p>
<label>Date of birth (MM/DD/YYYY)<input id="dob" inputmode="numeric" autocomplete="bday" placeholder="MM/DD/YYYY"></label>
<button onclick="openForm()">Open form</button><p class="err" id="gerr"></p></div>
<div id="form" hidden><div id="formtext"></div>
<h3>7. Your decision</h3>
<label><input type="checkbox" id="agree"> I have read this form and I authorize the sharing described above.</label>
<label>Type your full name to sign<input id="name" autocomplete="name"></label>
<div class="row"><button onclick="decide('sign')">Sign and authorize</button><button class="ghost" onclick="decide('decline')">I do not authorize</button></div>
<p class="err" id="ferr"></p></div>
<script>
const T={token!r};
async function openForm(){{
  const d=document.getElementById('dob').value.trim(), gerr=document.getElementById('gerr'); gerr.textContent=''; if(!d) return;
  const res=await fetch('/consent/'+T+'/verify',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{dob:d}})}});
  const j=await res.json().catch(()=>({{}}));
  if(!res.ok){{ gerr.textContent=j.detail||'Something went wrong.'; return; }}
  document.getElementById('formtext').innerHTML=j.html; document.getElementById('gate').hidden=true; document.getElementById('form').hidden=false;
}}
async function decide(kind){{
  const err=document.getElementById('ferr'); err.textContent='';
  if(kind==='sign'&&!document.getElementById('agree').checked){{ err.textContent='Check the box to authorize, or choose "I do not authorize".'; return; }}
  const res=await fetch('/consent/'+T+'/sign',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{dob:document.getElementById('dob').value,name:document.getElementById('name').value,decision:kind}})}});
  const j=await res.json().catch(()=>({{}}));
  if(!res.ok){{ if(res.status===403){{ document.getElementById('form').hidden=true; document.getElementById('gate').hidden=false; document.getElementById('gerr').textContent=j.detail||'Date of birth did not match.'; }} else err.textContent=j.detail||'Something went wrong.'; return; }}
  document.body.querySelector('main').innerHTML=j.status==='signed'?'<h2>Thank you</h2><p>You signed the authorization. Your information will be sent to the specialist now.</p>':'<h2>Got it</h2><p>You chose not to authorize sharing. Nothing was shared.</p>';
}}
</script>"""
    return PAGE.replace("{{BODY}}", body)


PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Authorization form | DocSpot</title>
<style>
body{margin:0;font-family:system-ui,-apple-system,'Segoe UI',Roboto,sans-serif;background:#F4F6FA;color:#1B2433;line-height:1.5}
header{background:#08101E;color:#E8EEF7;padding:14px 20px;font-weight:800}header span{color:#91A0B6;font-weight:600}
main{max-width:640px;margin:0 auto;padding:20px 16px 48px}
h2{font-size:22px;margin:8px 0 12px}h3{font-size:15px;margin:18px 0 4px}
label{display:block;margin:14px 0 4px;font-weight:600}
input[type=text],input:not([type]),input[inputmode]{display:block;width:100%;box-sizing:border-box;font:inherit;padding:10px 12px;border:1px solid #C9D1DC;border-radius:8px;margin-top:6px;background:#fff}
input[type=checkbox]{width:18px;height:18px;margin-right:8px;vertical-align:-3px}
button{font:inherit;font-weight:700;background:#2F5BD3;color:#fff;border:0;border-radius:8px;padding:11px 16px;margin-top:14px;cursor:pointer}
button.ghost{background:#fff;color:#1B2433;border:1px solid #C9D1DC}.row{display:flex;gap:8px;flex-wrap:wrap}
.err{color:#B4452F;font-weight:600}.signed{background:#E7F7EF;padding:10px;border-radius:8px}
footer{font-size:12px;color:#6B778A;margin-top:28px}
</style></head><body><header>DocSpot <span>secure authorization</span></header>
<main>{{BODY}}<footer>Prototype for a hackathon demo. This form is modeled on HIPAA authorization requirements and has not been reviewed by a lawyer.</footer></main></body></html>"""
