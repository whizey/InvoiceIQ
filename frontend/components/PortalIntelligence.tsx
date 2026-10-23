"use client";

import {useCallback, useEffect, useState} from "react";
import {useRouter} from "next/navigation";
import PortalNavigation from "@/components/PortalNavigation";

type ScoredInvoice={id:string;number:string;counterparty:string;probability:string;balance:string};
type Intelligence={workspace:string;kind:"showcase"|"customer";model_version:string;artifact_sha256:string;
  total:number;scored:number;invoices:ScoredInvoice[]};
type Contribution={feature:string;label:string;value:number|null;shap_value:number;direction:string};
type Explanation={invoice_number:string;counterparty:string;probability:string;model_version:string;
  artifact_sha256:string;explanation_method:string;contributions:Contribution[]};
const percent=(value:string)=>`${(Number(value)*100).toFixed(1)}%`;

export default function PortalIntelligence(){
  const router=useRouter();
  const [data,setData]=useState<Intelligence|null>(null);
  const [detail,setDetail]=useState<Explanation|null>(null);
  const [loading,setLoading]=useState(true);
  const [busy,setBusy]=useState(false);
  const [error,setError]=useState("");
  const [success,setSuccess]=useState("");
  const load=useCallback(async(signal?:AbortSignal)=>{
    const response=await fetch("/api/portal/intelligence",{cache:"no-store",signal});
    if(response.status===401){router.replace("/home");return}
    const body=await response.json();
    if(!response.ok)throw new Error(body.detail||"Model intelligence unavailable");
    setData(body);
  },[router]);
  useEffect(()=>{
    const controller=new AbortController();
    const timer=window.setTimeout(()=>{
      void load(controller.signal).catch(e=>{if(!controller.signal.aborted)setError(e instanceof Error?e.message:"Unable to load intelligence")})
        .finally(()=>{if(!controller.signal.aborted)setLoading(false)});
    },0);
    return()=>{window.clearTimeout(timer);controller.abort()};
  },[load]);
  async function explain(id:string){
    setBusy(true);setError("");setDetail(null);
    try{
      const response=await fetch(`/api/portal/intelligence/${id}`,{cache:"no-store"});
      const body=await response.json();
      if(!response.ok)throw new Error(body.detail||"Explanation unavailable");
      setDetail(body);
    }catch(e){setError(e instanceof Error?e.message:"Explanation unavailable")}
    finally{setBusy(false)}
  }
  async function score(){
    setBusy(true);setError("");setSuccess("");
    try{
      const response=await fetch("/api/portal/score",{method:"POST"});
      const body=await response.json();
      if(!response.ok)throw new Error(body.detail||"Scoring failed");
      await load();setDetail(null);
      setSuccess(`${body.scored} invoices scored with ${body.model_version}.`);
    }catch(e){setError(e instanceof Error?e.message:"Scoring failed")}
    finally{setBusy(false)}
  }
  return <div className="settings-page"><PortalNavigation/><main className="settings-main" style={{maxWidth:1240}}>
    <p className="settings-eyebrow">Workspace / Intelligence</p>
    <h1>Model intelligence</h1>
    <p className="settings-intro">Posting-time payment-risk estimates from the verified B2B champion model. These are model outputs, not observed recovery rates or instructions to contact customers.</p>
    {loading&&<p role="status">Loading verified model results…</p>}
    {error&&<div role="alert" className="portal-notice-error">{error} <button onClick={()=>{setError("");setLoading(true);void load().finally(()=>setLoading(false))}}>Retry</button></div>}
    {success&&<p role="status" className="portal-notice-success">{success}</p>}
    {data&&<>
      <section className="portal-intelligence-summary">
        <div><small>WORKSPACE</small><strong>{data.workspace}</strong><span>Invoice portfolio</span></div>
        <div><small>SCORE COVERAGE</small><strong>{data.scored.toLocaleString()} / {data.total.toLocaleString()}</strong><span>Stored scores on invoices</span></div>
        <div><small>VERIFIED MODEL</small><strong>B2B payment-risk model</strong><span>{data.model_version}</span></div>
      </section>
      {data.kind==="customer"&&data.total>0&&<div className="portal-score-action">
        <div><strong>Run verified inference</strong><p>Uses this workspace’s invoice and dated payment history as it stood before each posting. Re-run after importing historical records.</p></div>
        <button type="button" disabled={busy} onClick={score}>{busy?"Scoring…":"Score workspace"}</button>
      </div>}
      {data.total===0&&<div className="portal-empty">No invoices yet. Import company invoices to enable model inference.</div>}
      {data.total>0&&data.scored===0&&<div className="portal-empty">No validated scores are stored for this workspace.</div>}
      {data.invoices.length>0&&<div className="portal-intelligence-layout">
        <section className="portal-intelligence-list" aria-label="Scored invoices">
          <h2>Highest model estimates</h2>
          <p>Showing up to 50 invoices. Percentage means estimated payment risk at posting.</p>
          {data.invoices.map(invoice=><button key={invoice.id} type="button" disabled={busy}
            onClick={()=>void explain(invoice.id)} className="portal-intelligence-row">
            <span><strong>{invoice.number}</strong><small>{invoice.counterparty}</small></span>
            <b>{percent(invoice.probability)}</b>
          </button>)}
        </section>
        <section className="portal-intelligence-detail" aria-label="Model explanation">
          {detail?<>
            <p className="settings-eyebrow">Verified explanation</p>
            <h2>{detail.invoice_number}</h2><p>{detail.counterparty} · {percent(detail.probability)} estimated payment risk</p>
            <p className="portal-model-meta">{detail.explanation_method} · {detail.model_version}<br/>Artifact SHA-256: {detail.artifact_sha256}</p>
            <h3>Largest feature contributions</h3>
            {detail.contributions.slice(0,8).map(item=><div className="portal-contribution" key={item.feature}>
              <span>{item.label}</span><strong>{item.shap_value>=0?"+":""}{item.shap_value.toFixed(3)}</strong>
            </div>)}
            <p className="portal-model-meta">SHAP contributions are in model log odds. Positive values raise the estimate; negative values lower it.</p>
          </>:<div className="portal-empty">Select a scored invoice to inspect its verified feature explanation.</div>}
        </section>
      </div>}
      <p className="portal-model-meta">Model results are retrospective when shown on existing records. They do not prove historical deployment, measured recovery performance, or an external action.</p>
    </>}
  </main></div>;
}
