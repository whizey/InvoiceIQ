"use client";

import {FormEvent, useEffect, useState} from "react";

type Note={id:string;invoice:string|null;kind:Kind;body:string;created_at:string|null;sample:boolean};
type Kind="customer_message"|"dispute"|"promise"|"internal";
async function fetchNotes(invoiceId:string):Promise<Note[]>{
  const r=await fetch(`/api/portal/notes?invoice_id=${encodeURIComponent(invoiceId)}`,{cache:"no-store"});
  const b=await r.json();
  if(!r.ok)throw new Error(b.detail||"Notes unavailable");
  return b.notes;
}
const KIND_LABEL:Record<Kind,string>={customer_message:"Customer message",dispute:"Dispute",promise:"Payment promise",internal:"Internal"};

/** Notes on one invoice: read them, and add one. The assistant reads these as data, never as instructions. */
export default function InvoiceNotes({invoiceId}:{invoiceId:string}){
  const [notes,setNotes]=useState<Note[]|null>(null);
  const [kind,setKind]=useState<Kind>("customer_message");
  const [body,setBody]=useState("");
  const [busy,setBusy]=useState(false);
  const [error,setError]=useState("");
  useEffect(()=>{
    let live=true;
    fetchNotes(invoiceId).then(n=>{if(live)setNotes(n)})
      .catch(e=>{if(live)setError(e instanceof Error?e.message:"Notes unavailable")});
    return ()=>{live=false};
  },[invoiceId]);
  async function add(event:FormEvent){
    event.preventDefault();
    if(body.trim().length<3||busy)return;
    setBusy(true);setError("");
    try{
      const r=await fetch("/api/portal/notes",{method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({invoice_id:invoiceId,kind,body:body.trim()})});
      const b=await r.json();
      if(!r.ok)throw new Error(typeof b.detail==="string"?b.detail:"Could not save the note");
      setBody("");setNotes(await fetchNotes(invoiceId));
    }catch(e){setError(e instanceof Error?e.message:"Could not save the note")}
    finally{setBusy(false)}
  }
  return <section className="notes" aria-label="Invoice notes">
    <h3>Notes</h3>
    {error&&<p role="alert" className="portal-notice-error">{error}</p>}
    {notes===null&&!error&&<p className="rp-muted">Loading notes…</p>}
    {notes?.length===0&&<p className="rp-muted">No notes on this invoice yet.</p>}
    {notes&&notes.length>0&&<ul className="notes-list">{notes.map(n=><li key={n.id}>
      <p className="notes-meta"><span className={"notes-kind k-"+n.kind}>{KIND_LABEL[n.kind]}</span>
        {n.sample&&<span className="notes-sample" title="Illustrative sample note">Sample</span>}
        {n.created_at&&<time dateTime={n.created_at}>{new Date(n.created_at).toLocaleDateString()}</time>}</p>
      <p className="notes-body">{n.body}</p>
    </li>)}</ul>}
    <form onSubmit={e=>void add(e)} className="notes-form">
      <label>Kind
        <select value={kind} onChange={e=>setKind(e.target.value as Kind)}>
          {(Object.keys(KIND_LABEL) as Kind[]).map(k=><option key={k} value={k}>{KIND_LABEL[k]}</option>)}
        </select>
      </label>
      <label>Note
        <textarea rows={3} maxLength={1000} value={body} onChange={e=>setBody(e.target.value)}
          placeholder="What did the customer say, promise or dispute?"/>
      </label>
      <button type="submit" disabled={busy||body.trim().length<3}>{busy?"Saving…":"Add note"}</button>
      <p className="rp-muted">The AI assistant reads notes as information, never as instructions.</p>
    </form>
  </section>;
}
