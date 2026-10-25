"use client";
import {useCallback,useEffect,useRef,useState} from "react";
import Link from "next/link";
import PortalNavigation from "@/components/PortalNavigation";

type Event={at:string;kind:"invoice"|"payment";invoice:string;counterparty:string|null;amount:string;due_date:string|null};
const SPEEDS=[0.5,1,2,5,10] as const;
const rupees=(s:string)=>"₹"+Number(s).toLocaleString("en-IN",{maximumFractionDigits:0});
const shortDate=(iso:string)=>new Date(iso+"T00:00:00Z").toLocaleDateString("en-IN",
  {day:"numeric",month:"short",year:"numeric",timeZone:"UTC"});

/** A visitor's own isolated, ephemeral copy of Kaveri's deterministic story, written into the database one
 * event at a time as it plays -- through the real import and scoring pipeline every other invoice goes
 * through -- instead of replaying data that already existed. Starting over discards this session's workspace;
 * idle sessions are swept automatically, so nothing here persists beyond the visit. */
export default function LiveDemo(){
  const [workspace,setWorkspace]=useState("");
  const [events,setEvents]=useState<Event[]>([]);
  const [total,setTotal]=useState<number|null>(null);
  const [done,setDone]=useState(false);
  const [playing,setPlaying]=useState(false);
  const [speed,setSpeed]=useState<typeof SPEEDS[number]>(2);
  const [error,setError]=useState("");
  const [starting,setStarting]=useState(true);
  const busy=useRef(false);
  const timer=useRef<ReturnType<typeof setInterval>|null>(null);
  const session=useRef(0);

  const start=useCallback(async()=>{
    const mySession=++session.current;
    if(timer.current){clearInterval(timer.current);timer.current=null}
    setPlaying(false);setError("");setStarting(true);setEvents([]);setTotal(null);setDone(false);
    try{
      const response=await fetch("/api/portal/demo-start",{method:"POST"});
      const payload=await response.json();
      if(!response.ok)throw new Error(payload.detail||"The live demo isn't available right now");
      if(mySession!==session.current)return;
      setWorkspace(payload.workspace||"");
    }catch(e){if(mySession===session.current)setError(e instanceof Error?e.message:"The live demo isn't available right now")}
    finally{if(mySession===session.current)setStarting(false)}
  },[]);
  useEffect(()=>{(async()=>{await start()})()},[start]);

  const tick=useCallback(async(count:number)=>{
    if(busy.current)return;
    busy.current=true;
    const mySession=session.current;
    try{
      const response=await fetch(`/api/portal/demo-tick?count=${count}`,{method:"POST"});
      const payload=await response.json();
      if(!response.ok)throw new Error(payload.detail||"The live demo stopped unexpectedly");
      if(mySession!==session.current)return;
      setEvents(prev=>[...prev,...payload.events]);
      setTotal(payload.total);
      if(payload.done){setDone(true);setPlaying(false)}
    }catch(e){if(mySession===session.current){setError(e instanceof Error?e.message:"The live demo stopped unexpectedly");setPlaying(false)}}
    finally{busy.current=false}
  },[]);
  const step=useCallback(()=>{setPlaying(false);void tick(1)},[tick]);

  useEffect(()=>{
    if(timer.current){clearInterval(timer.current);timer.current=null}
    if(!playing||starting)return;
    timer.current=setInterval(()=>{void tick(Math.max(1,Math.round(speed)))},Math.max(250,600/speed));
    return()=>{if(timer.current){clearInterval(timer.current);timer.current=null}};
  },[playing,speed,starting,tick]);

  return <div className="fu-shell">
    <PortalNavigation/>
    <main className="fu-main">
      <p className="settings-eyebrow">{workspace||"Live demo"} / Watch it happen live</p>
      <h1 className="fu-title">A new, live copy of Kaveri&apos;s story</h1>
      <p className="fu-sub" role="status">
        <strong>Synthetic demo data, created just for this visit</strong> — this is your own private, isolated
        workspace. Each invoice and payment below is written into a real database row the moment you reveal
        it, and scored by the real pipeline right then, not pre-computed. It follows the same deterministic
        story every time (not random), and this workspace is discarded automatically after your visit.
        {" "}<Link href="/demo" className="fu-link">Prefer a fixed replay instead? →</Link>
      </p>
      {error&&<p role="alert" className="portal-notice-error">{error}</p>}
      {starting&&<p role="status">Opening a fresh workspace…</p>}
      {!starting&&<>
        <div style={{display:"flex",gap:10,alignItems:"center",flexWrap:"wrap",margin:"18px 0"}}>
          <button type="button" className="fu-review" onClick={()=>setPlaying(p=>!p)} disabled={done}>
            {playing?"Pause":events.length===0?"Play":"Resume"}</button>
          <button type="button" className="fu-review" onClick={step} disabled={done}>Step</button>
          <button type="button" className="fu-review" onClick={()=>void start()}>Start over</button>
          <label style={{display:"flex",alignItems:"center",gap:6,fontSize:13}}>Speed
            <select value={speed} onChange={e=>setSpeed(Number(e.target.value) as typeof SPEEDS[number])}>
              {SPEEDS.map(s=><option key={s} value={s}>{s}×</option>)}
            </select>
          </label>
          <span className="fu-muted">{events.length}{total!==null?` of ${total}`:""} events{done?" — done":""}</span>
        </div>
        <section aria-labelledby="live-feed" className="fu-panel">
          <h2 id="live-feed" className="fu-h2">Activity</h2>
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
      </>}
    </main>
  </div>;
}
