"use client";
import {useEffect,useState} from "react";

type Alert={id:string;invoice_id:string;subject:string|null;reason:string|null;status:"OPEN"|"RESOLVED";resolved_decision:string|null;created_at:string|null};

// Alerts are raised by escalation events and closed by decision events (see the event pipeline docs).
export default function ApprovalAlerts({refreshKey}:{refreshKey:unknown}){
  const [alerts,setAlerts]=useState<Alert[]|null>(null);
  const [open,setOpen]=useState(0);
  useEffect(()=>{
    let active=true;
    const load=async()=>{
      try{
        const r=await fetch("/api/portal/alerts",{cache:"no-store"});
        if(!r.ok)return;
        const payload=await r.json();
        if(active){setAlerts(payload.alerts);setOpen(payload.open)}
      }catch{/* the panel is supplementary; the page works without it */}
    };
    void load();
    const timer=window.setInterval(load,5000);  // events may arrive through a broker a moment later
    return()=>{active=false;window.clearInterval(timer)};
  },[refreshKey]);
  if(alerts===null)return null;
  return <section aria-label="Approval alerts" style={{background:"#fff",border:"1px solid #e3e3df",borderRadius:12,padding:"20px 22px",marginBottom:22}}>
    <div style={{display:"flex",justifyContent:"space-between",alignItems:"baseline",gap:12,flexWrap:"wrap"}}>
      <h2 style={{fontSize:18,margin:0}}>Approval alerts</h2>
      <span style={{fontSize:12,color:"#777"}}>{open} open · raised when an invoice is escalated, closed when a decision is recorded</span>
    </div>
    {alerts.length===0&&<p style={{fontSize:13,color:"#777",marginBottom:0}}>No alerts yet. Escalate an invoice below to raise one.</p>}
    <ul style={{listStyle:"none",padding:0,margin:"12px 0 0",display:"grid",gap:8}}>
      {alerts.slice(0,6).map(a=><li key={a.id} style={{display:"flex",justifyContent:"space-between",gap:12,flexWrap:"wrap",
        padding:"10px 12px",borderRadius:8,background:a.status==="OPEN"?"#fff7e8":"#f1f6f1",fontSize:13}}>
        <span><strong>{a.subject||"Invoice"}</strong>{a.reason&&<span style={{color:"#666"}}> — {a.reason}</span>}</span>
        <span style={{color:a.status==="OPEN"?"#8a5a00":"#2f6b36",fontWeight:600}}>
          {a.status==="OPEN"?"Needs review":`Resolved · ${a.resolved_decision}`}</span>
      </li>)}
    </ul>
  </section>;
}
