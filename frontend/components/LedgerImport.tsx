"use client";
import {useState} from "react";

type Skip={reason:string;count:number;rows:number[]};
type Report={source:string;headers:string[];mapping:Record<string,string|null>;total_rows:number;ready:number;
  skipped:Skip[];warnings:string[];missing:string[];preview:Record<string,string>[];imported?:number;scored?:number};

const FIELDS:[string,string,boolean][]=[["invoice_number","Invoice number",true],["customer","Customer",true],
  ["invoice_date","Invoice date",true],["total","Invoice amount",true],["due_date","Due date",false],
  ["balance","Balance still owed",false],["paid","Amount paid",false],["payment_date","Payment date",false],
  ["terms_days","Credit days",false],["status","Status",false]];

async function toBase64(file:File){
  const bytes=new Uint8Array(await file.arrayBuffer());
  let binary="";
  for(let i=0;i<bytes.length;i+=0x8000)binary+=String.fromCharCode(...bytes.subarray(i,i+0x8000));
  return btoa(binary);
}

/** Tally and Zoho Books exports in: preview what will import and what will be skipped, then import. */
export default function LedgerImport({onImported,canImport}:{onImported:()=>void;canImport:boolean}){
  const [file,setFile]=useState<{name:string;content:string}|null>(null);
  const [mapping,setMapping]=useState<Record<string,string|null>>({});
  const [report,setReport]=useState<Report|null>(null);
  const [busy,setBusy]=useState(false);
  const [error,setError]=useState("");
  const [done,setDone]=useState("");

  async function send(action:"import-preview"|"import-file",payload:{name:string;content:string},overrides:Record<string,string|null>){
    const response=await fetch("/api/portal/"+action,{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({filename:payload.name,content:payload.content,mapping:overrides})});
    const body=await response.json();
    if(!response.ok)throw new Error(typeof body.detail==="string"?body.detail:"The file could not be read");
    return body as Report;
  }
  async function choose(selected:File){
    setBusy(true);setError("");setDone("");setReport(null);setMapping({});
    try{
      if(selected.size>2_500_000)throw new Error("The file is larger than 2.5 MB; export a shorter period");
      const payload={name:selected.name,content:await toBase64(selected)};
      setFile(payload);
      const result=await send("import-preview",payload,{});
      setReport(result);setMapping(result.mapping);
    }catch(e){setError(e instanceof Error?e.message:"The file could not be read")}
    finally{setBusy(false)}
  }
  async function remap(field:string,header:string){
    if(!file)return;
    const next={...mapping,[field]:header||null};
    setMapping(next);setBusy(true);setError("");
    try{setReport(await send("import-preview",file,next))}
    catch(e){setError(e instanceof Error?e.message:"Preview failed")}
    finally{setBusy(false)}
  }
  async function confirm(){
    if(!file||!report)return;
    setBusy(true);setError("");
    try{
      const result=await send("import-file",file,mapping);
      setDone(`Imported ${result.imported} invoices and scored ${result.scored}.`+
        (result.skipped.length?` Skipped ${result.skipped.reduce((n,s)=>n+s.count,0)}.`:""));
      setReport(null);setFile(null);onImported();
    }catch(e){setError(e instanceof Error?e.message:"Import failed")}
    finally{setBusy(false)}
  }
  async function example(format:"zoho"|"tally"){
    const response=await fetch("/api/portal/example-export?format="+format,{cache:"no-store"});
    const body=await response.json();
    if(!response.ok){setError(body.detail||"Example unavailable");return}
    const url=URL.createObjectURL(new Blob([body.content],{type:"text/csv"}));
    const link=Object.assign(document.createElement("a"),{href:url,download:body.filename});
    link.click();URL.revokeObjectURL(url);
  }
  return <section className="li-panel" aria-labelledby="li-title">
    <h2 id="li-title" className="fu-h2">Import from Tally or Zoho Books</h2>
    <p className="fu-muted">Upload a Zoho Books invoice export or Tally&apos;s Bills Receivable report (CSV or Excel, up to 2,000 invoices).
      Nothing is saved until you confirm. Example files: <button type="button" className="li-link" onClick={()=>example("zoho")}>Zoho Books</button> · <button type="button" className="li-link" onClick={()=>example("tally")}>Tally</button></p>
    <input aria-label="Choose an export file" type="file" accept=".csv,.txt,.xlsx,.xlsm,text/csv" disabled={busy}
      onChange={e=>{const selected=e.target.files?.[0];if(selected)void choose(selected);e.target.value=""}}/>
    {busy&&<p role="status" className="fu-muted">Reading the file…</p>}
    {error&&<p role="alert" className="portal-notice-error">{error}</p>}
    {done&&<p role="status" className="portal-notice-success">{done}</p>}
    {report&&<div className="li-report">
      <p className="li-headline"><strong>{report.source}</strong> export · {report.total_rows.toLocaleString("en-IN")} rows ·{" "}
        <strong>{report.ready.toLocaleString("en-IN")} ready to import</strong>
        {report.skipped.length>0&&<> · {report.skipped.reduce((n,s)=>n+s.count,0).toLocaleString("en-IN")} skipped</>}</p>
      {report.missing.length>0&&<p role="alert" className="portal-notice-error">Choose a column for: {report.missing.join(", ")}.</p>}
      {report.skipped.length>0&&<ul className="li-skips">{report.skipped.map(s=><li key={s.reason}>
        <strong>{s.count} {s.count===1?"row":"rows"} skipped: {s.reason}</strong>
        <span> (row {s.rows.join(", ")}{s.count>s.rows.length?", …":""})</span></li>)}</ul>}
      {report.warnings.map(w=><p key={w} className="li-warning">{w}</p>)}
      <details className="li-mapping" open={report.missing.length>0}>
        <summary>Columns used</summary>
        <div className="li-grid">{FIELDS.map(([field,label,required])=><label key={field}>{label}{required?" *":""}
          <select value={mapping[field]??""} disabled={busy} onChange={e=>void remap(field,e.target.value)}>
            <option value="">Not in this file</option>
            {report.headers.filter(Boolean).map(h=><option key={h} value={h}>{h}</option>)}
          </select></label>)}</div>
      </details>
      {canImport?<button type="button" className="li-confirm" disabled={busy||report.ready===0} onClick={confirm}>
        Import {report.ready.toLocaleString("en-IN")} invoices</button>:
        <p className="fu-muted"><a href="/signup">Create a company workspace</a> to import these invoices.</p>}
    </div>}
  </section>;
}
