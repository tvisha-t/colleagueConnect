// api.js
// Talks to the DocSpot backend (backend/, FastAPI + Claude). Every call has a short
// timeout and a non-AI fallback, so the demo keeps working when the backend is off.
// The API key lives only on the backend; nothing here needs it.
'use strict';

// Local development: the page comes from Live Server (port 5500) or straight from disk, and the backend runs on port 8000.
// Deployed: the backend serves this page itself, so API calls go to the same site.
const API_BASE = (location.protocol==='file:'||location.port==='5500') ? 'http://127.0.0.1:8000' : '';
const AI = {status:'unknown', model:null};   // status: 'ai' (key set) | 'patterns' (backend on, no key) | 'off'

async function apiPost(path, body, ms){
  const ctl=new AbortController(), timer=setTimeout(()=>ctl.abort(),ms);
  try{
    const res=await fetch(API_BASE+path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body),signal:ctl.signal});
    if(!res.ok) throw new Error('HTTP '+res.status);
    return await res.json();
  } finally { clearTimeout(timer); }
}
async function checkHealth(){
  try{ const res=await fetch(API_BASE+'/health',{signal:AbortSignal.timeout(2000)}); const j=await res.json(); AI.status=j.ai?'ai':'patterns'; AI.model=j.model; }
  catch(e){ AI.status='off'; AI.model=null; }
  return AI.status;
}

/* ---------- AI draft ---------- */
// Returns {text, model} from Claude, or null so the caller uses the template instead.
async function aiDraft(req){
  if(AI.status==='off') return null;
  try{ const j=await apiPost('/ai/draft',req,6000); return j.text?j:null; }
  catch(e){ return null; }
}

/* ---------- Patient consent ---------- */
// These need the backend. Callers handle a thrown error (backend off) themselves.
const consentPreview = payload => apiPost('/consent/preview', payload, 6000);
const consentSend = payload => apiPost('/consent/send', payload, 20000);
async function consentStatus(token){
  const res=await fetch(API_BASE+'/consent/'+token+'/status',{signal:AbortSignal.timeout(4000)});
  if(!res.ok) throw new Error('HTTP '+res.status);
  return res.json();
}

/* ---------- Privacy check ---------- */
// mode: 'ai' (pattern rules + Claude) | 'patterns' (backend, no key) | 'offline' (rules in this file)
const PRIVACY_LABEL = {
  ai:'AI-assisted privacy check',
  patterns:'Local privacy rules',
  offline:'Local privacy rules (backend unavailable)'
};
const BROAD_CONCERN_LABELS = {
  inflammatory_arthritis:'inflammatory arthritis', type_2_diabetes:'type 2 diabetes',
  atrial_fibrillation:'atrial fibrillation', kidney_disease:'kidney disease',
  plaque_psoriasis:'plaque psoriasis', chronic_migraine:'chronic migraine',
  crohns_disease:"crohn's disease", specialty_concern:'a concern requiring specialty care'
};
async function privacyCheck(req){
  if(AI.status!=='off'){
    try{ const j=await apiPost('/ai/privacy-check',req,10000); return {flags:j.flags||[], mode:j.mode}; }
    catch(e){ /* fall through to the local rules */ }
  }
  return {flags:localPatternCheck(req), mode:'offline'};
}

// Browser copy of backend/privacy.py. Keep the two in step.
function localPatternCheck({text,patient,approved,topic,allowed}){
  approved=new Set(approved||[]);
  const norm=s=>String(s||'').replace(/\s+/g,' ').trim().toLowerCase().replace(/[’‘]/g,"'");
  const rx=s=>s.replace(/[.*+?^${}()|[\]\\]/g,'\\$&');
  const allowedDigits=(allowed||[]).map(a=>String(a).replace(/\D/g,'').slice(-10));
  const flags=[], seen=new Set();
  const add=(snippet,reason,kind)=>{ const k=norm(snippet)+'|'+kind; if(snippet&&!seen.has(k)){ seen.add(k); flags.push({text:snippet,reason,kind}); } };
  const findPhrase=p=>{ if(!p||p.trim().length<2) return null; const m=text.match(new RegExp('(?<!\\w)'+rx(p.trim())+'(?!\\w)','i')); return m?m[0]:null; };
  const each=(re,fn)=>{ for(const m of text.matchAll(re)) fn(m[0]); };

  // 1. Patient identifiers
  if(patient){
    if(patient.initials){const hit=findPhrase(patient.initials);if(hit)add(hit,'Patient initials are not permitted in the fax.','identifier');}
    const name=patient.name||'';
    [name,...name.split(/\s+/)].forEach(part=>{
      if(part.length<3) return;
      for(const m of text.matchAll(new RegExp('(?<!\\w)'+rx(part)+'(?!\\w)','gi'))){
        if(!/\bDr\.?\s*$/.test(text.slice(0,m.index))){ add(m[0],'Patient name. Identifiers are released only after the patient consents.','identifier'); break; }
      }
    });
    if(patient.dob&&findPhrase(patient.dob)) add(findPhrase(patient.dob),'Patient date of birth. Released only after the patient consents.','identifier');
    const mrn=String(patient.mrn||'').replace(/\D/g,'');
    if(mrn.length>=3){ const m=text.match(new RegExp('(?<!\\d)'+mrn+'(?!\\d)')); if(m) add(m[0],'Looks like the patient’s medical record number.','identifier'); }
  }
  each(/\b(?:MRN|medical record(?: number)?)\b[\s:#.]*[\w…-]*/gi, s=>add(s.trim(),'Medical record number. Don’t include record numbers in the fax.','identifier'));
  each(/\b\d{1,2}[/-]\d{1,2}[/-](?:\d{4}|\d{2})\b/g, s=>add(s,'A full date could be a date of birth. Use age instead.','identifier'));
  each(/\b\d{3}-\d{2}-\d{4}\b/g, s=>add(s,'Looks like a Social Security number.','identifier'));

  // 2. Contact details that aren't the sender's or recipient's own
  each(/\(?\b\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}\b/g, s=>{ if(!allowedDigits.includes(s.replace(/\D/g,'').slice(-10))) add(s,'Phone or fax number that isn’t yours or the recipient’s. It could identify the patient.','contact'); });
  each(/\b[\w.+-]+@[\w-]+\.[\w.]+\b/g, s=>{ if(!(allowed||[]).map(norm).includes(norm(s))) add(s,'Email address. It could identify the patient.','contact'); });
  each(/\b\d{1,6}[ \t]+(?:[A-Za-z0-9.]+[ \t]+){0,4}(?:St|Street|Ave|Avenue|Rd|Road|Blvd|Boulevard|Drive|Ln|Lane|Way|Ct|Court|Pkwy|Parkway|Hwy|Highway|Pl|Place)\b\.?/g, s=>add(s.trim(),'Street address. It could identify the patient.','contact'));

  // Only the selected broad concern may appear. Demographics, medications, labs,
  // and any more detailed diagnosis are never faxed, even if a client marks them approved.
  if(patient){
    if(patient.age){const m=text.match(new RegExp('\\b'+patient.age+'[- ]?(?:year|yo\\b|y/o)','i'));if(m)add(m[0],'Patient age is not permitted in this fax.','unapproved');}
    const sexLabel=patient.sex==='F'?'female':patient.sex==='M'?'male':'';
    if(sexLabel){const hit=findPhrase(sexLabel);if(hit)add(hit,'Patient sex is not permitted in this fax.','unapproved');}
    [['meds',patient.meds,'Medication information is not permitted in this fax.'],['labs',patient.labs,'Lab results are not permitted in this fax.']].forEach(([field,items,reason])=>{
      (items||[]).forEach(item=>{const candidates=[item,item.split(/\s(?=\d)|,/)[0].trim()];for(const c of candidates){const hit=findPhrase(c);if(hit){add(hit,reason,'unapproved');break;}}});
    });
    const allowedConcern=norm(BROAD_CONCERN_LABELS[patient.general_concern]||'');
    (patient.dx||[]).forEach(item=>{
      const general=item.split(/[,;]|\b(?:suspected|about|new|with)\b/i)[0].trim(), fullHit=findPhrase(item), generalHit=findPhrase(general);
      if(fullHit&&norm(item)!==norm(general)) add(fullHit,'Detailed diagnosis information is not permitted; share only the selected general concern.','unapproved');
      if(generalHit&&norm(general)!==allowedConcern) add(generalHit,'Only the selected general concern may be shared.','unapproved');
      if(generalHit&&!approved.has('dx')) add(generalHit,'The general concern was not approved for sharing.','unapproved');
    });
  }
  // Drop a flag when a longer flag already covers it ("Reyes" inside "Maria Reyes")
  return flags.filter(f=>!flags.some(g=>g!==f&&g.text.length>f.text.length&&g.text.toLowerCase().includes(f.text.toLowerCase())));
}
