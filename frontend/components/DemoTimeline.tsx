"use client";
import {useCallback,useEffect,useRef,useState} from "react";
import {useRouter} from "next/navigation";
import PortalNavigation from "@/components/PortalNavigation";

type Event={at:string;kind:"invoice"|"payment";invoice:string;counterparty:string;amount:string;due_date?:string};
type Timeline={workspace:string;generated:boolean;total:number;events:Event[]};
const SPEEDS=[0.5,1,2,5,10] as const;
const rupees=(s:string)=>"₹"+Number(s).toLocaleString("en-IN",{maximumFractionDigits:0});
const shortDate=(iso:string)=>new Date(iso+"T00:00:00Z").toLocaleDateString("en-IN",
  {day:"numeric",month:"short",year:"numeric",timeZone:"UTC"});

/** Replays a showcase workspace's own already-seeded history, in order -- it shows what already happened,
 * at a pace you control, rather than fabricating new events on each visit. */
export default function DemoTimeline(){
  const router=useRouter();
  const [timeline,setTimeline]=useState<Timeline|null>(null);
  const [error,setError]=useState("");
  const [revealed,setRevealed]=useState(0);
  const [playing,setPlaying]=useState(false);
  const [speed,setSpeed]=useState<typeof SPEEDS[number]>(2);
  const timer=useRef<ReturnType<typeof setInterval>|null>(null);
  useEffect(()=>{
    let active=true;
    (async()=>{
      try{
        const response=await fetch("/api/portal/demo-timeline",{cache:"no-store"});
        if(response.status===401){router.replace("/home");return}
        const payload=await response.json();
        if(!response.ok)throw new Error(payload.detail||"The demo isn't available for this workspace");
        if(active)setTimeline(payload);
      }catch(e){if(active)setError(e instanceof Error?e.message:"The demo isn't available for this workspace")}
    })();
    return()=>{active=false};
  },[router]);
  useEffect(()=>{
    if(timer.current){clearInterval(timer.current);timer.current=null}
    if(!playing||!timeline)return;
    timer.current=setInterval(()=>{
      setRevealed(n=>{
        const next=Math.min(timeline.events.length,n+Math.max(1,Math.round(speed)));
        if(next>=timeline.events.length&&timer.current){clearInterval(timer.current);timer.current=null;setPlaying(false)}
        return next;
      });
    },Math.max(120,500/speed));
    return()=>{if(timer.current){clearInterval(timer.current);timer.current=null}};
  },[playing,speed,timeline]);
  const step=useCallback(()=>{
    if(!timeline)return;
    setPlaying(false);
    setRevealed(n=>Math.min(timeline.events.length,n+1));
  },[timeline]);
  const reset=useCallback(()=>{setPlaying(false);setRevealed(0)},[]);
  const shown=timeline?timeline.events.slice(0,revealed):[];
  const done=Boolean(timeline)&&revealed>=((timeline as Timeline|null)?.events.length??0)&&revealed>0;
  return <div className="fu-shell">
    <PortalNavigation/>
    <main className="fu-main">
      <p className="settings-eyebrow">{timeline?.workspace||"Workspace"} / Watch it happen</p>
      <h1 className="fu-title">A replay of this business&apos;s own history</h1>
      {timeline&&<p className="fu-sub" role="status">
        <strong>Synthetic demo data</strong> — every invoice and payment below already happened in
        {" "}{timeline.workspace}&apos;s seeded history and went through the real scoring pipeline when it was
        created; this page only replays it in order, at a pace you control. None of it is live or newly
        generated on this visit.
      </p>}
      {error&&<p role="alert" className="portal-notice-error">{error}</p>}
      {!timeline&&!error&&<p role="status">Loading…</p>}
      {timeline&&<>
        <div style={{display:"flex",gap:10,alignItems:"center",flexWrap:"wrap",margin:"18px 0"}}>
          <button type="button" className="fu-review" onClick={()=>setPlaying(p=>!p)} disabled={done}>
            {playing?"Pause":revealed===0?"Play":"Resume"}</button>
          <button type="button" className="fu-review" onClick={step} disabled={done}>Step</button>
          <button type="button" className="fu-review" onClick={reset} disabled={revealed===0}>Reset</button>
          <label style={{display:"flex",alignItems:"center",gap:6,fontSize:13}}>Speed
            <select value={speed} onChange={e=>setSpeed(Number(e.target.value) as typeof SPEEDS[number])}>
              {SPEEDS.map(s=><option key={s} value={s}>{s}×</option>)}
            </select>
          </label>
          <span className="fu-muted">{revealed} of {timeline.events.length} events{done?" — done":""}</span>
        </div>
        <section aria-labelledby="demo-feed" className="fu-panel">
          <h2 id="demo-feed" className="fu-h2">Activity</h2>
          {shown.length===0&&<p className="fu-muted">Press Play or Step to begin.</p>}
          <ol className="fu-list" style={{display:"flex",flexDirection:"column-reverse",gap:0}}>
            {shown.slice(-60).map((e,i)=><li key={`${e.kind}-${e.invoice}-${e.at}-${i}`}
              style={{padding:"14px 0",borderTop:"1px solid #ececea"}}>
              <div>
                <div className="fu-head">
                  <strong>{e.kind==="invoice"?"Invoice created":"Payment received"}</strong>
                  <span className="fu-num">{shortDate(e.at)}</span>
                </div>
                <p style={{margin:"4px 0 0",fontSize:14,color:"#333"}}>{e.counterparty} · {e.invoice} · {rupees(e.amount)}
                  {e.kind==="invoice"&&e.due_date?` · due ${shortDate(e.due_date)}`:""}</p>
              </div>
            </li>)}
          </ol>
        </section>
      </>}
    </main>
  </div>;
}
