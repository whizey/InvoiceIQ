"use client";
import {useCallback,useEffect,useState} from "react";
import {useRouter} from "next/navigation";
import RecoveryCharts from "@/components/RecoveryCharts";
import ApprovalAlerts from "@/components/ApprovalAlerts";
import PortalNavigation from "@/components/PortalNavigation";
import InvoiceNotes from "@/components/InvoiceNotes";
import LedgerImport from "@/components/LedgerImport";
import Term from "@/components/Term";
import {DECISION_WORDS} from "@/lib/glossary";
import type {PortalFilters} from "@/lib/portal-filters";

type LateReason={days_late:number;kind:string;text:string};
type Invoice={id:string;number:string;counterparty:string;due_date:string;amount:string;paid:string;balance:string;risk_probability:string|null;late_reason:LateReason|null};
type Event={id:string;invoice_id:string;decision:string;note:string;created_at:string};
type Overview={workspace:string;kind:"showcase"|"customer";provenance:string;actions:Event[]};
type InvoicePage={invoices:Invoice[];total:number;page:number;page_size:number};
type Section="invoices"|"cases"|"approvals";
type ListStatus="all"|"overdue"|"awaiting"|"escalated";
const STATUS_WORDS:Record<ListStatus,string>={all:"invoices",overdue:"overdue invoices",awaiting:"overdue invoices awaiting review",escalated:"escalated invoices"};
const PICK:Record<string,ListStatus>={"Awaiting review":"awaiting",Escalated:"escalated"};
const rupees=(s:string)=>"₹"+Number(s).toLocaleString("en-IN",{maximumFractionDigits:2});

export default function PortalOperations({section,initialFilters}:{section:Section;initialFilters:PortalFilters}){
  const router=useRouter();
  const [data,setData]=useState<Overview|null>(null);
  const [invoicePage,setInvoicePage]=useState<InvoicePage|null>(null);
  const [loading,setLoading]=useState(true);
  const [busy,setBusy]=useState(false);
  const [error,setError]=useState("");
  const [query,setQuery]=useState(initialFilters.q);
  const [page,setPage]=useState(initialFilters.page);
  const [sort,setSort]=useState(initialFilters.sort);
  const [status,setStatus]=useState<ListStatus>(section==="cases"?"awaiting":section==="approvals"?"escalated":"all");
  const [reload,setReload]=useState(0);
  const [success,setSuccess]=useState("");
  const [selected,setSelected]=useState<Invoice|null>(null);
  const [choice,setChoice]=useState<"ESCALATE"|"APPROVE"|"REJECT">("APPROVE");
  const [note,setNote]=useState("");
  const load=useCallback(async()=>{
    const response=await fetch("/api/portal/overview",{cache:"no-store"});
    if(response.status===401){router.replace("/home");return}
    const payload=await response.json();
    if(!response.ok)throw new Error(payload.detail||"Workspace unavailable");
    setData(payload);
  },[router]);
  useEffect(()=>{
    let active=true;
    (async()=>{
      try{await load()}
      catch(e){if(active)setError(e instanceof Error?e.message:"Workspace unavailable")}
      finally{if(active)setLoading(false)}
    })();
    return()=>{active=false};
  },[load]);

  useEffect(()=>{
    const url=new URL(window.location.href);
    if(query)url.searchParams.set("q",query);else url.searchParams.delete("q");
    if(page>1)url.searchParams.set("page",String(page));else url.searchParams.delete("page");
    if(sort!=="posting_desc")url.searchParams.set("sort",sort);else url.searchParams.delete("sort");
    window.history.replaceState(window.history.state,"",url);
  },[query,page,sort]);
  useEffect(()=>{
    const controller=new AbortController();
    const timer=window.setTimeout(async()=>{
      try{
        const params=new URLSearchParams({q:query,page:String(page),page_size:"25",sort,status});
        const response=await fetch("/api/portal/invoices?"+params,{cache:"no-store",signal:controller.signal});
        const payload=await response.json();
        if(!response.ok)throw new Error(payload.detail||"Invoices unavailable");
        setInvoicePage(payload);setError("");
      }catch(e){if(!controller.signal.aborted)setError(e instanceof Error?e.message:"Invoices unavailable")}
    },query?250:0);
    return()=>{window.clearTimeout(timer);controller.abort()};
  },[query,page,sort,status,reload]);
  const invoices=invoicePage?.invoices||[];

  async function recordDecision(){
    if(!selected||note.trim().length<3)return;
    setBusy(true);setError("");setSuccess("");
    try{
      const r=await fetch("/api/portal/decisions",{method:"POST",
        headers:{"Content-Type":"application/json"},
        body:JSON.stringify({invoice_id:selected.id,decision:choice,note:note.trim()})});
      const payload=await r.json();
      if(!r.ok)throw new Error(payload.detail||"Decision was not recorded");
      setSelected(null);setNote("");await load();setSuccess("Decision recorded in the audit history.");
    }catch(e){setError(e instanceof Error?e.message:"Decision failed")}
    finally{setBusy(false)}
  }
  const title={invoices:"Invoices",cases:"Recovery reviews",approvals:"Approval queue"}[section];
  return <div style={{minHeight:"100dvh",background:"#f5f5f3",fontFamily:"Arial,sans-serif",color:"#171717"}}>
    <PortalNavigation/>
    <main style={{padding:"32px clamp(18px,4vw,64px)",maxWidth:1600,margin:"auto"}}>
      <p style={{fontSize:11,letterSpacing:2,textTransform:"uppercase",color:"#777"}}>Workspace / {title}</p>
      <h1 style={{fontSize:"clamp(30px,4vw,46px)",letterSpacing:"-.045em",margin:"10px 0"}}>{title}</h1>
      <p style={{color:"#6c6c6c",marginBottom:26,fontSize:13}}>{data?.workspace||"Loading workspace"}</p>
      {loading&&<div role="status" aria-label="Loading records" className="ops-skeleton">
        <div className="sk sk-panel"><span className="sk-line w40"/>{Array.from({length:7},(_,i)=><span key={i} className="sk-line"/>)}</div>
        <div className="sk sk-panel"><span className="sk-line w60"/>{Array.from({length:5},(_,i)=><span key={i} className="sk-line"/>)}</div>
      </div>}
      {error&&<div role="alert" style={{padding:16,background:"#fff0ee",borderRadius:8,color:"#922b1e"}}>{error} <button type="button" onClick={()=>setReload(value=>value+1)}>Retry invoices</button></div>}
      {success&&<p role="status" style={{padding:16,background:"#eff7ef",borderRadius:8,color:"#215a27"}}>{success}</p>}
      {data&&section!=="invoices"&&<ApprovalAlerts refreshKey={data}/>}
      {data&&section!=="invoices"&&<RecoveryCharts view={section} refreshKey={data}
        onPick={label=>{const next=PICK[label]??"overdue";setInvoicePage(null);setStatus(next);setPage(1);
          document.getElementById("invoice-list")?.scrollIntoView({behavior:"smooth"})}}/>}
      {data&&<>
        {section==="invoices"&&<LedgerImport canImport={data.kind==="customer"}
          onImported={()=>{void load();setReload(value=>value+1)}}/>}
        <div className="portal-operations-grid">
          <section style={panel} id="invoice-list">
            <div style={{display:"flex",gap:12,alignItems:"center",justifyContent:"space-between",flexWrap:"wrap"}}>
              <h2 style={{fontSize:19,margin:0}}>{section==="cases"?"Invoices for review":section==="approvals"?"Review pending invoices":"Invoice register"}</h2>
              <input aria-label="Search invoices" placeholder="Search invoice or account" value={query} onChange={e=>{setInvoicePage(null);setQuery(e.target.value);setPage(1)}}
                style={{padding:11,border:"1px solid #ddd",borderRadius:7,minWidth:215}}/>
              <label style={{fontSize:12}}>Show <select aria-label="Show invoices" value={status} onChange={e=>{setInvoicePage(null);setStatus(e.target.value as ListStatus);setPage(1)}} style={{padding:10,marginLeft:6,border:"1px solid #ddd",borderRadius:7}}>
                <option value="all">All invoices</option><option value="overdue">Overdue</option>
                <option value="awaiting">Awaiting review</option><option value="escalated">Escalated</option>
              </select></label>
              <label style={{fontSize:12}}>Sort invoices <select aria-label="Sort invoices" value={sort} onChange={e=>{setInvoicePage(null);setSort(e.target.value);setPage(1)}} style={{padding:10,marginLeft:6,border:"1px solid #ddd",borderRadius:7}}>
                <option value="posting_desc">Newest posting</option><option value="posting_asc">Oldest posting</option>
                <option value="due_asc">Earliest due</option><option value="due_desc">Latest due</option>
                <option value="number_asc">Invoice number</option>
              </select></label>
            </div>
            <p style={{fontSize:12,color:"#777"}} role={invoicePage?undefined:"status"}>{invoicePage
              ?`Showing ${invoices.length} of ${invoicePage.total} ${STATUS_WORDS[status]}${query?" matching the search":""} · INR`
              :`Loading ${STATUS_WORDS[status]}…`}</p>
            <div style={{overflowX:"auto"}}><table className="portal-invoice-table" style={{width:"100%",borderCollapse:"collapse",fontSize:13}}>
              <thead><tr>{([["Invoice",null],["Customer",null],["Due date",null],["Still owed","owed"],["Why it's late","late"],["Late-payment risk","risk"],["Action",null]] as const).map(([s,k])=>
                <th key={s} style={{textAlign:"left",padding:"14px 8px",borderBottom:"1px solid #ddd"}}>{k?<Term k={k}>{s}</Term>:s}</th>)}</tr></thead>
              <tbody>{invoices.map(inv=><tr key={inv.id}>
                <td data-label="Invoice" style={cell}>{inv.number}</td><td data-label="Customer" style={cell}>{inv.counterparty}</td>
                <td data-label="Due date" style={cell}>{inv.due_date}</td><td data-label="Outstanding" style={cell}>{rupees(inv.balance)}</td>
                <td data-label="Why it's late" style={reasonCell}>{inv.late_reason?<><strong>{inv.late_reason.days_late} days overdue.</strong> {inv.late_reason.text}</>:<span style={{color:"#8a8a87"}}>{Number(inv.balance)>0?"Not due yet":"Paid"}</span>}</td>
                <td data-label="Late-payment risk" style={cell}>{inv.risk_probability===null?"Not scored":Math.round(Number(inv.risk_probability)*100)+"% chance of late payment"}</td>
                <td data-label="Action" style={cell}><button onClick={()=>{setSelected(inv);setNote("");}}
                  style={{border:"1px solid #ddd",padding:"8px",borderRadius:6,background:"#fff",cursor:"pointer"}}>
                  Review →</button></td>
              </tr>)}</tbody>
            </table></div>
            {invoicePage?.total===0&&<p style={{padding:20,color:"#666"}}>{query?"No invoices match this search.":status==="all"?"No invoices yet. Import a Tally or Zoho Books export to begin.":`No ${STATUS_WORDS[status]}.`}</p>}
            {invoicePage&&invoicePage.total>invoicePage.page_size&&<div style={{display:"flex",alignItems:"center",gap:12,justifyContent:"flex-end",marginTop:16}}>
              <button disabled={page<=1} onClick={()=>{setInvoicePage(null);setPage(value=>value-1)}}>Previous</button>
              <span>Page {page} of {Math.ceil(invoicePage.total/invoicePage.page_size)}</span>
              <button disabled={page*invoicePage.page_size>=invoicePage.total} onClick={()=>{setInvoicePage(null);setPage(value=>value+1)}}>Next</button>
            </div>}
          </section>
          <section style={panel}>
            <h2 style={{fontSize:19,marginTop:0}}>{selected?"Record a decision":"Review activity"}</h2>
            {selected&&<div style={{display:"grid",gap:12}}>
              <p style={{fontSize:13}}><strong>{selected.number}</strong> · {selected.counterparty}</p>
              {selected.late_reason&&<p className="late-reason"><strong>Why it&apos;s late · {selected.late_reason.days_late} days overdue</strong>{selected.late_reason.text}</p>}
              <label style={{fontSize:13}}>Decision
                <select value={choice} onChange={e=>setChoice(e.target.value as typeof choice)}
                  style={{display:"block",width:"100%",padding:11,marginTop:5}}>
                  <option value="APPROVE">Approve: follow up with the customer</option><option value="ESCALATE">Escalate: pass to a manager</option><option value="REJECT">Reject: no action for now</option>
                </select>
              </label>
              <label style={{fontSize:13}}>Review note
                <textarea rows={3} maxLength={500} minLength={3} value={note} onChange={e=>setNote(e.target.value)}
                  placeholder="Reason for this decision…" style={{display:"block",width:"100%",padding:10,marginTop:5}}/>
              </label>
              <button disabled={busy||note.trim().length<3} onClick={recordDecision}
                style={{background:"#171717",color:"white",border:0,padding:12,borderRadius:7,cursor:"pointer"}}>
                {busy?"Saving…":"Record decision"}</button>
              <button onClick={()=>setSelected(null)} style={{background:"white",padding:10,border:"1px solid #ddd",borderRadius:7}}>Cancel</button>
              <p style={{fontSize:12,color:"#777"}}>Records an internal review only. It will not send communications or move money.</p>
              <InvoiceNotes key={selected.id} invoiceId={selected.id}/>
            </div>}
            {data.actions.length===0&&!selected&&<p style={{fontSize:13,color:"#777"}}>No decisions recorded. Select an invoice to begin.</p>}
            {data.actions.slice(0,30).map(ev=><div key={ev.id} style={{borderTop:"1px solid #eee",padding:"12px 0",fontSize:13}}>
              <strong>{DECISION_WORDS[ev.decision]??ev.decision}</strong><p style={{margin:"6px 0",color:"#666"}}>{ev.note}</p>
              <small style={{color:"#777"}}>{new Date(ev.created_at).toLocaleString()}</small>
            </div>)}
          </section>
        </div>
        <p style={{fontSize:12,color:"#777",marginTop:22}}>
          {data.kind==="showcase"?"Your review decisions are visible only to you.":
          "Only records within your authenticated company workspace are accessible."}
        </p>
      </>}
    </main>
  </div>;
}
const panel:React.CSSProperties={background:"#fff",border:"1px solid #dededb",borderRadius:12,padding:22,minWidth:0,marginBottom:16};
const cell:React.CSSProperties={padding:"12px 8px",borderBottom:"1px solid #ececea",whiteSpace:"nowrap"};
const reasonCell:React.CSSProperties={...cell,whiteSpace:"normal",minWidth:240,maxWidth:380,lineHeight:1.45};
