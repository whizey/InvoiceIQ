"use client";
import {useCallback,useEffect,useRef,useState} from "react";
import {useRouter} from "next/navigation";
import Link from "next/link";
import PortalNavigation from "@/components/PortalNavigation";

type Event={at:string;kind:"invoice"|"payment";invoice:string;counterparty:string|null;amount:string;due_date:string|null};
const SPEEDS=[0.5,1,2,5,10] as const;
const rupees=(s:string)=>"₹"+Number(s).toLocaleString("en-IN",{maximumFractionDigits:0});
const shortDate=(iso:string)=>new Date(iso+"T00:00:00Z").toLocaleDateString("en-IN",
  {day:"numeric",month:"short",year:"numeric",timeZone:"UTC"});

/** A new workspace's guided walkthrough: sample invoices and payments written into the real workspace one
 * event at a time, through the real import and scoring pipeline, so a new signup can see the product work
 * before importing their own ledger. Skippable at any point. */
export default function Onboarding(){
  const router=useRouter();
  const [events,setEvents]=useState<Event[]>([]);
  const [total,setTotal]=useState<number|null>(null);
  const [done,setDone]=useState(false);
  const [playing,setPlaying]=useState(false);
  const [speed,setSpeed]=useState<typeof SPEEDS[number]>(2);
  const [error,setError]=useState("");
  const busy=useRef(false);
  const timer=useRef<ReturnType<typeof setInterval>|null>(null);

  const tick=useCallback(async(count:number)=>{
    if(busy.current)return;
    busy.current=true;
    try{
      const response=await fetch(`/api/portal/onboarding-tick?count=${count}`,{method:"POST"});
      if(response.status===401){router.replace("/home");return}
      const payload=await response.json();
      if(!response.ok)throw new Error(payload.detail||"Couldn't load the next sample invoices");
      setEvents(prev=>[...prev,...payload.events]);
      setTotal(payload.total);
      if(payload.done){setDone(true);setPlaying(false)}
    }catch(e){setError(e instanceof Error?e.message:"Couldn't load the next sample invoices");setPlaying(false)}
    finally{busy.current=false}
  },[router]);
  const step=useCallback(()=>{setPlaying(false);void tick(1)},[tick]);

  useEffect(()=>{
    if(timer.current){clearInterval(timer.current);timer.current=null}
    if(!playing)return;
    timer.current=setInterval(()=>{void tick(Math.max(1,Math.round(speed)))},Math.max(250,600/speed));
    return()=>{if(timer.current){clearInterval(timer.current);timer.current=null}};
  },[playing,speed,tick]);

  return <div className="fu-shell">
    <PortalNavigation/>
    <main className="fu-main">
      <p className="settings-eyebrow">Your workspace / Getting started</p>
      <h1 className="fu-title">Watch your workspace come to life</h1>
      <p className="fu-sub" role="status">
        <strong>Sample data</strong> — invoices and payments for a fictional business, written into your
        workspace just like a real import would be, so you can see how follow-ups, cash forecasts and
        reminders work. You can clear it from Settings whenever you&apos;re ready to import your real ledger.
      </p>
      {error&&<p role="alert" className="portal-notice-error">{error}</p>}
      <div style={{display:"flex",gap:10,alignItems:"center",flexWrap:"wrap",margin:"18px 0"}}>
        <button type="button" className="fu-review" onClick={()=>setPlaying(p=>!p)} disabled={done}>
          {playing?"Pause":events.length===0?"Play":"Resume"}</button>
        <button type="button" className="fu-review" onClick={step} disabled={done}>Step</button>
        <label style={{display:"flex",alignItems:"center",gap:6,fontSize:13}}>Speed
          <select value={speed} onChange={e=>setSpeed(Number(e.target.value) as typeof SPEEDS[number])}>
            {SPEEDS.map(s=><option key={s} value={s}>{s}×</option>)}
          </select>
        </label>
        <span className="fu-muted">{events.length}{total!==null?` of ${total}`:""} events{done?" — done":""}</span>
      </div>
      {done?
        <p className="fu-sub">That&apos;s the full sample story.{" "}
          <Link href="/follow-ups" className="fu-link">See your follow-up list →</Link></p>
      :
        <p className="fu-sub"><Link href="/follow-ups" className="fu-link">Skip, I&apos;ll import my own data →</Link></p>}
      <section aria-labelledby="onboarding-feed" className="fu-panel">
        <h2 id="onboarding-feed" className="fu-h2">Activity</h2>
        {events.length===0&&<p className="fu-muted">Press Play or Step to begin.</p>}
        <ol className="fu-list" style={{display:"flex",flexDirection:"column-reverse",gap:0}}>
          {events.slice(-60).map((e,i)=><li key={`${e.kind}-${e.invoice}-${e.at}-${i}`}
            style={{padding:"14px 0",borderTop:"1px solid #ececea"}}>
            <div>
              <div className="fu-head">
                <strong>{e.kind==="invoice"?"Invoice created":"Payment received"}</strong>
                <span className="fu-num">{shortDate(e.at)}</span>
              </div>
              <p style={{margin:"4px 0 0",fontSize:14,color:"#333"}}>{e.counterparty||"—"} · {e.invoice} · {rupees(e.amount)}
                {e.kind==="invoice"&&e.due_date?` · due ${shortDate(e.due_date)}`:""}</p>
            </div>
          </li>)}
        </ol>
      </section>
    </main>
  </div>;
}
