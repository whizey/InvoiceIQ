"use client";

import {FormEvent, useEffect, useState} from "react";

type Kpis={awaiting_review:number;awaiting_exposure:string;approved:number;rejected:number;escalated:number;
  reviewed:number;escalated_exposure:string};
type Summary={kind:"showcase"|"customer";kpis:Kpis;status_breakdown:{status:string;count:number}[];
  weekly_decisions:{week:string;approved:number;escalated:number;rejected:number}[]};
const SUGGESTIONS=["Which overdue invoices should we chase first?","Summarise our overdue exposure.","What is waiting for review?"];

function inr(value:string){
  const n=Number(value);
  if(n>=1e7)return`₹${(n/1e7).toFixed(2)}Cr`;
  if(n>=1e5)return`₹${(n/1e5).toFixed(2)}L`;
  return`₹${Math.round(n).toLocaleString("en-IN")}`;
}

function StatusChart({rows}:{rows:Summary["status_breakdown"]}){
  const max=Math.max(1,...rows.map(r=>r.count));
  return <div className="rp-bars" role="img" aria-label={rows.map(r=>`${r.status}: ${r.count}`).join(", ")}>
    {rows.map((r,i)=><div key={r.status} className="rp-bar-row">
      <span>{r.status}</span>
      <div className="rp-track"><div className={`rp-fill rp-fill-${i}`} style={{width:`${(r.count/max)*100}%`}}/></div>
      <strong>{r.count}</strong>
    </div>)}
  </div>;
}

function WeeklyChart({rows}:{rows:Summary["weekly_decisions"]}){
  const totals=rows.map(r=>r.approved+r.escalated+r.rejected);
  const max=Math.max(1,...totals);
  const W=480,H=170,pad=24,bw=(W-pad*2)/rows.length;
  return <svg viewBox={`0 0 ${W} ${H}`} className="rp-svg" role="img"
    aria-label={`Decisions per week: ${rows.map((r,i)=>`${r.week} ${totals[i]}`).join(", ")}`}>
    <line x1={pad} y1={H-pad} x2={W-pad} y2={H-pad} stroke="#d9d9d6"/>
    {rows.map((r,i)=>{
      let y=H-pad;
      const seg=(n:number,c:string)=>{const h=(n/max)*(H-pad*2);y-=h;return h>0?<rect key={c} x={pad+i*bw+bw*0.2} y={y} width={bw*0.6} height={h} fill={c}/>:null};
      return <g key={r.week}>{seg(r.approved,"#0b0b0b")}{seg(r.escalated,"#c2410c")}{seg(r.rejected,"#a3a3a0")}
        <text x={pad+i*bw+bw/2} y={H-8} fontSize="9" textAnchor="middle" fill="#6b6b6b">{r.week.slice(5)}</text></g>;
    })}
  </svg>;
}

export default function RecoveryPanel(){
  const [data,setData]=useState<Summary|null>(null);
  const [error,setError]=useState("");
  const [question,setQuestion]=useState("");
  const [asking,setAsking]=useState(false);
  const [answer,setAnswer]=useState<{text:string;model:string}|null>(null);
  const [askError,setAskError]=useState("");

  useEffect(()=>{
    const controller=new AbortController();
    fetch("/api/portal/recovery-summary",{cache:"no-store",signal:controller.signal})
      .then(async r=>{const b=await r.json();if(!r.ok)throw new Error(b.detail||"Unable to load recovery summary");setData(b)})
      .catch(e=>{if(!controller.signal.aborted)setError(e instanceof Error?e.message:"Unable to load recovery summary")});
    return()=>controller.abort();
  },[]);

  async function ask(event:FormEvent,preset?:string){
    event.preventDefault();
    const q=(preset??question).trim();
    if(q.length<3||asking)return;
    setAsking(true);setAskError("");setAnswer(null);
    try{
      const r=await fetch("/api/portal/assistant",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({question:q})});
      const b=await r.json();
      if(!r.ok)throw new Error(b.detail||"The assistant could not answer");
      setAnswer({text:b.answer,model:b.model});
    }catch(e){setAskError(e instanceof Error?e.message:"The assistant could not answer")}
    finally{setAsking(false)}
  }

  const k=data?.kpis;
  return <section className="rp" aria-labelledby="rp-title">
    <header><p className="settings-eyebrow">Recovery &amp; approvals</p>
      <h2 id="rp-title">Recovery pipeline</h2>
      <p>Derived from recorded decisions on {data?.kind==="showcase"?"illustrative showcase":"your workspace"} invoices. Latest decision per invoice sets its status.</p></header>
    {error&&<p className="portal-notice-error" role="alert">{error}</p>}
    {!data&&!error&&<p role="status" className="rp-muted">Loading recovery summary…</p>}
    {data&&k&&<>
      <div className="rp-kpis">
        <div><small>AWAITING REVIEW</small><strong>{k.awaiting_review}</strong><span>{inr(k.awaiting_exposure)} overdue and unreviewed</span></div>
        <div><small>APPROVED</small><strong>{k.approved}</strong><span>recovery actions approved</span></div>
        <div><small>ESCALATED</small><strong>{k.escalated}</strong><span>{inr(k.escalated_exposure)} open balance</span></div>
        <div><small>REJECTED</small><strong>{k.rejected}</strong><span>{k.reviewed} reviewed in total</span></div>
      </div>
      <div className="rp-charts">
        <div className="rp-card"><h3>Cases by status</h3><StatusChart rows={data.status_breakdown}/></div>
        <div className="rp-card"><h3>Decisions per week</h3><WeeklyChart rows={data.weekly_decisions}/>
          <p className="rp-legend"><i style={{background:"#0b0b0b"}}/>Approved <i style={{background:"#c2410c"}}/>Escalated <i style={{background:"#a3a3a0"}}/>Rejected</p></div>
      </div>
    </>}
    <div className="rp-card rp-ai">
      <h3>Ask InvoiceIQ AI</h3>
      <p className="rp-muted">Answers use only this workspace&apos;s stored figures. Responses are generated by a language model and should be verified before acting.</p>
      <div className="rp-chips">{SUGGESTIONS.map(s=><button key={s} type="button" disabled={asking} onClick={e=>{setQuestion(s);void ask(e,s)}}>{s}</button>)}</div>
      <form onSubmit={ask}>
        <input value={question} onChange={e=>setQuestion(e.target.value)} maxLength={400} placeholder="Ask about overdue exposure, approvals or priorities" aria-label="Question for InvoiceIQ AI"/>
        <button type="submit" disabled={asking||question.trim().length<3}>{asking?"Thinking…":"Ask"}</button>
      </form>
      {askError&&<p className="portal-notice-error" role="alert">{askError}</p>}
      {answer&&<div className="rp-answer" role="status"><p>{answer.text}</p><small>Model: {answer.model}</small></div>}
    </div>
  </section>;
}
