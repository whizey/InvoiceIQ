"use client";
import {useCallback,useEffect,useState} from "react";
import Link from "next/link";
import {useRouter} from "next/navigation";
import PortalNavigation from "@/components/PortalNavigation";
import Term from "@/components/Term";
import CashForecast,{type Cash} from "@/components/CashForecast";
import ReminderPanel from "@/components/ReminderPanel";

type Reason={days_late:number;kind:string;text:string};
type Item={id:string;number:string;counterparty:string;due_date:string;balance:string;days_overdue:number;
  chance_late:number;priority:number;summary:string;on_hold:boolean;late_reason:Reason|null};
type Band={band:string;invoices:number;predicted:number;actual:number};
type Scorer={used:"model"|"baseline";model_auc:number|null;baseline_auc:number|null;evaluated:number;
  verdict:string;calibration:Band[];calibration_note:string|null};
type FollowUpData={as_of:string;workspace:string;generated:boolean;scorer:Scorer;cash:Cash;
  totals:{to_chase:number;overdue_amount:string;on_hold:number;due_soon_amount:string};
  chase:Item[];on_hold:Item[];due_soon:Item[]};

const rupees=(s:string)=>"₹"+Number(s).toLocaleString("en-IN",{maximumFractionDigits:0});
const pct=(n:number)=>Math.round(n*100)+"%";
const SHOWN=25;
type Sent={id:string;invoice_id:string;invoice:string;customer:string;channel:string;sent_at:string;
  payment:{paid_on:string;amount:string;days_after:number}|null};
type SentLog={reminders:Sent[];summary:{sent:number;judged:number;followed_by_payment:number}};
const shortDate=(iso:string)=>new Date(iso).toLocaleDateString("en-IN",{day:"numeric",month:"short"});

/** The screen people open every morning: who to chase first, and why. */
export default function FollowUps(){
  const router=useRouter();
  const [data,setData]=useState<FollowUpData|null>(null);
  const [error,setError]=useState("");
  const [all,setAll]=useState(false);
  const [open,setOpen]=useState<string|null>(null);
  const [sent,setSent]=useState<SentLog|null>(null);
  const loadSent=useCallback(async()=>{
    const response=await fetch("/api/portal/reminders",{cache:"no-store"});
    if(response.ok)setSent(await response.json());
  },[]);
  useEffect(()=>{
    let active=true;
    (async()=>{
      const response=await fetch("/api/portal/reminders",{cache:"no-store"}).catch(()=>null);
      if(active&&response?.ok)setSent(await response.json());
    })();
    return()=>{active=false};
  },[]);
  const lastSent=new Map<string,string>();
  for(const r of sent?.reminders||[])if(!lastSent.has(r.invoice_id))lastSent.set(r.invoice_id,r.sent_at);
  useEffect(()=>{
    let active=true;
    (async()=>{
      try{
        const response=await fetch("/api/portal/follow-ups",{cache:"no-store"});
        if(response.status===401){router.replace("/home");return}
        const payload=await response.json();
        if(!response.ok)throw new Error(payload.detail||"Follow-ups unavailable");
        if(active)setData(payload);
      }catch(e){if(active)setError(e instanceof Error?e.message:"Follow-ups unavailable")}
    })();
    return()=>{active=false};
  },[router]);
  const chase=data?(all?data.chase:data.chase.slice(0,SHOWN)):[];
  return <div className="fu-shell">
    <PortalNavigation/>
    <main className="fu-main">
      <p className="settings-eyebrow">{data?.workspace||"Workspace"} / Follow-ups</p>
      <h1 className="fu-title">Who to chase today</h1>
      {data&&<p className="fu-sub">
        <strong>{data.totals.to_chase}</strong> overdue invoices, <strong>{rupees(data.totals.overdue_amount)}</strong> to collect.
        Ranked by amount × chance of paying late × days overdue. As of {data.as_of}.
      </p>}
      {error&&<p role="alert" className="portal-notice-error">{error}</p>}
      {!data&&!error&&<div role="status" aria-label="Loading follow-ups" className="ops-skeleton">
        <div className="sk sk-panel">{Array.from({length:8},(_,i)=><span key={i} className="sk-line"/>)}</div></div>}
      {data&&<CashForecast cash={data.cash} generated={data.generated}/>}
      {data&&<div className="fu-grid">
        <section aria-labelledby="fu-chase" className="fu-panel">
          <h2 id="fu-chase" className="fu-h2">Chase first</h2>
          {data.chase.length===0&&<p className="fu-muted">Nothing overdue. Nobody to chase today.</p>}
          <ol className="fu-list">{chase.map((item,index)=><li key={item.id} className="fu-item">
            <span className="fu-rank" aria-hidden="true">{index+1}</span>
            <div className="fu-body">
              <div className="fu-head"><strong>{item.counterparty}</strong><span className="fu-num">{item.number}</span></div>
              <p className="fu-summary">{item.summary}</p>
              {item.late_reason&&item.late_reason.kind!=="habit"&&<p className="fu-reason">{item.late_reason.text}</p>}
              {lastSent.has(item.id)&&<p className="fu-sent">Reminded {shortDate(lastSent.get(item.id)!)}</p>}
              {open===item.id&&<ReminderPanel invoiceId={item.id} onSent={()=>void loadSent()}/>}
            </div>
            <div className="fu-side">
              <span className="fu-chance" title="Chance this invoice is paid late">{pct(item.chance_late)} likely late</span>
              <button type="button" className="fu-review" aria-expanded={open===item.id}
                onClick={()=>setOpen(v=>v===item.id?null:item.id)}>{open===item.id?"Close":"Remind"}</button>
              <Link className="fu-review" href={`/invoices?q=${encodeURIComponent(item.number)}`}>Review</Link>
            </div>
          </li>)}</ol>
          {data.chase.length>SHOWN&&<button type="button" className="fu-more" onClick={()=>setAll(v=>!v)}>
            {all?"Show the top "+SHOWN:"Show all "+data.chase.length}</button>}
        </section>
        <aside className="fu-aside">
          <section aria-labelledby="fu-soon" className="fu-panel">
            <h2 id="fu-soon" className="fu-h2">Due this week</h2>
            {data.due_soon.length===0?<p className="fu-muted">Nothing falls due in the next 7 days.</p>:
              <ul className="fu-mini">{data.due_soon.slice(0,8).map(item=><li key={item.id}>
                <strong>{item.counterparty}</strong><span>{item.summary}</span><em>{pct(item.chance_late)} likely late</em></li>)}</ul>}
          </section>
          {data.on_hold.length>0&&<section aria-labelledby="fu-hold" className="fu-panel">
            <h2 id="fu-hold" className="fu-h2">On hold: disputed</h2>
            <ul className="fu-mini">{data.on_hold.map(item=><li key={item.id}>
              <strong>{item.counterparty}</strong><span>{rupees(item.balance)} · {item.number}</span><em>{item.late_reason?.text}</em></li>)}</ul>
          </section>}
          {sent&&sent.summary.sent>0&&<section aria-labelledby="fu-sent" className="fu-panel">
            <h2 id="fu-sent" className="fu-h2">Reminders sent</h2>
            {sent.summary.judged>0&&<p className="fu-verdict">{sent.summary.followed_by_payment} of {sent.summary.judged} were followed by a payment within two weeks.</p>}
            <ul className="fu-mini">{sent.reminders.slice(0,6).map(r=><li key={r.id}>
              <strong>{r.customer}</strong><span>{r.invoice} · {r.channel==="whatsapp"?"WhatsApp":"Email"} · {shortDate(r.sent_at)}</span>
              <em>{r.payment?`Paid ${rupees(r.payment.amount)} ${r.payment.days_after} days later`:"No payment yet"}</em></li>)}</ul>
          </section>}
          <section aria-labelledby="fu-how" className="fu-panel">
            <h2 id="fu-how" className="fu-h2">How the <Term k="risk">chance of paying late</Term> is worked out</h2>
            <p className="fu-verdict">{data.scorer.verdict}</p>
            {data.scorer.calibration_note&&<p className="fu-muted">{data.scorer.calibration_note}</p>}
            <Link href="/intelligence" className="fu-link">Compare the model and the simple average →</Link>
          </section>
        </aside>
      </div>}
    </main>
  </div>;
}
