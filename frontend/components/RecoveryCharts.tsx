"use client";

import {useEffect, useState} from "react";

type Kpis={awaiting_review:number;awaiting_exposure:string;approved:number;rejected:number;escalated:number;
  reviewed:number;escalated_exposure:string;case_exposure:string};
type Summary={kind:"showcase"|"customer";kpis:Kpis;status_breakdown:{status:string;count:number;amount:string}[];
  weekly_decisions:{week:string;approved:number;escalated:number;rejected:number}[]};
export type RecoveryView="dashboard"|"cases"|"approvals";

function inr(value:string|number){
  const n=Number(value);
  if(n>=1e7)return`₹${(n/1e7).toFixed(2)}Cr`;
  if(n>=1e5)return`₹${(n/1e5).toFixed(2)}L`;
  return`₹${Math.round(n).toLocaleString("en-IN")}`;
}

const STATUS_COLOR:Record<string,string>={"Awaiting review":"#d4d4d1",Approved:"#0b0b0b",Escalated:"#c2410c",Rejected:"#a3a3a0"};

function BarChart({rows,money}:{rows:{label:string;value:number}[];money?:boolean}){
  const max=Math.max(1,...rows.map(r=>r.value));
  return <div className="rp-bars" role="img" aria-label={rows.map(r=>`${r.label}: ${money?inr(r.value):r.value}`).join(", ")}>
    {rows.map(r=><div key={r.label} className="rp-bar-row">
      <span>{r.label}</span>
      <div className="rp-track"><div className="rp-fill" style={{width:`${(r.value/max)*100}%`,background:STATUS_COLOR[r.label]??"#0b0b0b"}}/></div>
      <strong>{money?inr(r.value):r.value}</strong>
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

/** KPI cards and charts derived from recorded decisions; `refreshKey` reloads after a new decision. */
export default function RecoveryCharts({view,refreshKey}:{view:RecoveryView;refreshKey?:unknown}){
  const [data,setData]=useState<Summary|null>(null);
  const [error,setError]=useState("");
  useEffect(()=>{
    const controller=new AbortController();
    fetch("/api/portal/recovery-summary",{cache:"no-store",signal:controller.signal})
      .then(async r=>{const b=await r.json();if(!r.ok)throw new Error(b.detail||"Unable to load recovery summary");setData(b);setError("")})
      .catch(e=>{if(!controller.signal.aborted)setError(e instanceof Error?e.message:"Unable to load recovery summary")});
    return()=>controller.abort();
  },[refreshKey]);

  const k=data?.kpis;
  const status=data?.status_breakdown??[];
  const kpis=!k?[]:view==="approvals"
    ?[["AWAITING DECISION",k.awaiting_review,`${inr(k.awaiting_exposure)} overdue and unreviewed`],
      ["APPROVED",k.approved,"recovery actions approved"],["ESCALATED",k.escalated,`${inr(k.escalated_exposure)} open balance`],
      ["REJECTED",k.rejected,`${k.reviewed} decided in total`]]
    :view==="cases"
    ?[["OPEN CASES",k.awaiting_review,"overdue invoices without a decision"],["UNDER REVIEW",k.escalated,"escalated cases"],
      ["EXPOSURE IN CASES",inr(k.case_exposure),"open balance on decided invoices"],["CLOSED OUT",k.approved+k.rejected,"approved or rejected"]]
    :[["AWAITING REVIEW",k.awaiting_review,`${inr(k.awaiting_exposure)} overdue and unreviewed`],
      ["APPROVED",k.approved,"recovery actions approved"],["ESCALATED",k.escalated,`${inr(k.escalated_exposure)} open balance`],
      ["REJECTED",k.rejected,`${k.reviewed} reviewed in total`]];
  return <section className={view==="dashboard"?"rp":"rp rp-embedded"} id="recovery-pipeline" aria-labelledby={`rp-title-${view}`}>
    <header><p className="settings-eyebrow">{view==="dashboard"?"Recovery & approvals":"Overview"}</p>
      <h2 id={`rp-title-${view}`}>{view==="approvals"?"Decision overview":view==="cases"?"Case overview":"Recovery pipeline"}</h2>
      <p>Derived from recorded decisions on {data?.kind==="showcase"?"illustrative showcase":"your workspace"} invoices. The latest decision per invoice sets its status.</p></header>
    {error&&<p className="portal-notice-error" role="alert">{error}</p>}
    {!data&&!error&&<p role="status" className="rp-muted">Loading recovery summary…</p>}
    {data&&<>
      <div className="rp-kpis">{kpis.map(([label,value,note])=><div key={String(label)}>
        <small>{label}</small><strong>{value}</strong><span>{note}</span></div>)}</div>
      <div className="rp-charts">
        {view==="cases"
          ?<div className="rp-card"><h3>Open balance by status</h3><BarChart money rows={status.map(s=>({label:s.status,value:Number(s.amount)}))}/></div>
          :<div className="rp-card"><h3>{view==="approvals"?"Decision outcomes":"Cases by status"}</h3>
            <BarChart rows={(view==="approvals"?status.slice(1):status).map(s=>({label:s.status,value:s.count}))}/></div>}
        <div className="rp-card"><h3>Decisions per week</h3><WeeklyChart rows={data.weekly_decisions}/>
          <p className="rp-legend"><i style={{background:"#0b0b0b"}}/>Approved <i style={{background:"#c2410c"}}/>Escalated <i style={{background:"#a3a3a0"}}/>Rejected</p></div>
      </div>
    </>}
  </section>;
}
