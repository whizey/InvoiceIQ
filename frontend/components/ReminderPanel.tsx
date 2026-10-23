"use client";
import {useState} from "react";

type Policy={allowed:boolean;blocked:string|null;escalate:string|null};
type Draft={body:string;source:string;rejected:string[]};
type Channel="email"|"whatsapp";
type Language="english"|"hinglish";

function split(body:string){
  const match=body.match(/^Subject:\s*(.+)\n+([\s\S]*)$/);
  return match?{subject:match[1].trim(),text:match[2].trim()}:{subject:"Payment reminder",text:body};
}

/** Draft a reminder, edit it, send it yourself, then log it. The policy decides whether contact is allowed. */
export default function ReminderPanel({invoiceId,onSent}:{invoiceId:string;onSent:()=>void}){
  const [channel,setChannel]=useState<Channel>("whatsapp");
  const [language,setLanguage]=useState<Language>("english");
  const [policy,setPolicy]=useState<Policy|null>(null);
  const [draft,setDraft]=useState<Draft|null>(null);
  const [body,setBody]=useState("");
  const [busy,setBusy]=useState(false);
  const [error,setError]=useState("");
  const [logged,setLogged]=useState(false);
  async function makeDraft(){
    setBusy(true);setError("");setDraft(null);
    try{
      const response=await fetch("/api/portal/reminder-draft",{method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({invoice_id:invoiceId,channel,language})});
      const payload=await response.json();
      if(!response.ok)throw new Error(payload.detail||"Draft failed");
      setPolicy(payload.policy);setDraft(payload.draft);setBody(payload.draft?.body||"");
    }catch(e){setError(e instanceof Error?e.message:"Draft failed")}
    finally{setBusy(false)}
  }
  async function markSent(){
    setBusy(true);setError("");
    try{
      const response=await fetch("/api/portal/reminders",{method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({invoice_id:invoiceId,channel,language,body,draft_source:draft?.source||"template"})});
      const payload=await response.json();
      if(!response.ok)throw new Error(payload.detail||"Could not log the reminder");
      setLogged(true);onSent();
    }catch(e){setError(e instanceof Error?e.message:"Could not log the reminder")}
    finally{setBusy(false)}
  }
  const {subject,text}=split(body);
  const open=channel==="whatsapp"?`https://wa.me/?text=${encodeURIComponent(text)}`:
    `mailto:?subject=${encodeURIComponent(subject)}&body=${encodeURIComponent(text)}`;
  return <div className="rm">
    <div className="rm-options" role="group" aria-label="Reminder options">
      {(["whatsapp","email"] as Channel[]).map(c=><button key={c} type="button" aria-pressed={channel===c}
        className={channel===c?"rm-chip rm-on":"rm-chip"} onClick={()=>{setChannel(c);setDraft(null)}}>{c==="whatsapp"?"WhatsApp":"Email"}</button>)}
      <span className="rm-sep" aria-hidden="true"/>
      {(["english","hinglish"] as Language[]).map(l=><button key={l} type="button" aria-pressed={language===l}
        className={language===l?"rm-chip rm-on":"rm-chip"} onClick={()=>{setLanguage(l);setDraft(null)}}>{l==="english"?"English":"Hinglish"}</button>)}
      <button type="button" className="rm-draft" disabled={busy} onClick={makeDraft}>{busy&&!draft?"Drafting…":draft?"Draft again":"Draft reminder"}</button>
    </div>
    {error&&<p role="alert" className="portal-notice-error">{error}</p>}
    {policy&&!policy.allowed&&<p role="status" className="rm-blocked">{policy.blocked}</p>}
    {policy?.escalate&&<p className="rm-escalate">{policy.escalate}</p>}
    {draft&&!logged&&<>
      <textarea aria-label="Reminder text" className="rm-text" rows={channel==="email"?8:5} value={body} onChange={e=>setBody(e.target.value)}/>
      <p className="rm-source">{draft.source==="template"?"Standard template":"Drafted by "+draft.source}. Every amount, date and invoice number is from this invoice
        {draft.rejected.length>0?` (a draft that mentioned ${draft.rejected.slice(0,2).join(", ")} was rejected)`:""}. Edit before sending.</p>
      <div className="rm-actions">
        <a className="rm-send" href={open} target="_blank" rel="noopener noreferrer">{channel==="whatsapp"?"Open in WhatsApp":"Open in email"}</a>
        <button type="button" onClick={()=>void navigator.clipboard?.writeText(body)}>Copy</button>
        <button type="button" disabled={busy||body.trim().length<10} onClick={markSent}>I sent it</button>
      </div>
    </>}
    {logged&&<p role="status" className="portal-notice-success">Logged. The next reminder for this invoice can go in three days; payment that follows within two weeks is tracked.</p>}
  </div>;
}
