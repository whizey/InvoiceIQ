"use client";
import {useEffect,useRef,useState} from "react";
import {formatInrCompact} from "@/lib/format-inr";

type Week={week_start:string;expected:number;low:number;high:number};
type Period={from:string;to:string;expected:number;actual:number};
export type Cash={weeks:Week[];next_4_weeks:{expected:number;low:number;high:number};open_balance:number;
  track_record:Period[];accuracy:{periods:number;min_ratio:number|null;max_ratio:number|null}};

const day=(iso:string)=>new Date(iso+"T00:00:00Z").toLocaleDateString("en-IN",{day:"numeric",month:"short",timeZone:"UTC"});

/** Expected collections per week with their range, and how the same forecast did in recent weeks. */
export default function CashForecast({cash,generated}:{cash:Cash;generated:boolean}){
  const [hover,setHover]=useState<number|null>(null);
  const box=useRef<HTMLDivElement>(null);
  const [W,setW]=useState(720);
  useEffect(()=>{
    const el=box.current;if(!el)return;
    const observer=new ResizeObserver(([entry])=>setW(Math.max(280,Math.round(entry.contentRect.width))));
    observer.observe(el);return()=>observer.disconnect();
  },[]);
  const H=200,pad={l:48,b:24,t:8},gap=Math.min(14,W/40);
  const top=Math.max(1,...cash.weeks.map(w=>w.high));
  const band=(W-pad.l)/cash.weeks.length, bar=band-gap;
  const y=(v:number)=>pad.t+(H-pad.t-pad.b)*(1-v/top);
  const latest=cash.track_record[0];
  const {min_ratio:lo,max_ratio:hi,periods}=cash.accuracy;
  return <section className="fu-panel cf" aria-labelledby="cf-title">
    <div className="cf-head">
      <div><h2 id="cf-title" className="fu-h2">Cash expected</h2>
        <p className="cf-big">{formatInrCompact(cash.next_4_weeks.expected)} <span>in the next 4 weeks</span></p>
        <p className="fu-muted">Likely between {formatInrCompact(cash.next_4_weeks.low)} and {formatInrCompact(cash.next_4_weeks.high)}, of {formatInrCompact(cash.open_balance)} still owed.</p></div>
      {latest&&<p className="cf-track">Four weeks ago this forecast said <strong>{formatInrCompact(latest.expected)}</strong>; <strong>{formatInrCompact(latest.actual)}</strong> came in.
        {periods>=4&&lo!=null&&hi!=null&&<> Over the last {periods} four-week periods, actual collections were {Math.round(lo*100)}–{Math.round(hi*100)}% of the forecast.</>}</p>}
    </div>
    {generated&&<p className="fu-muted">This workspace&apos;s invoices are generated with a fixed payment habit per customer, so this track record mostly shows the forecast fitting data built to be fittable. It isn&apos;t evidence the method works on a real business; open the real 2015 ledger from the home page to see a noisier, more honest version of this same number.</p>}
    <div className="cf-chart" ref={box}>
      <svg width={W} height={H} viewBox={`0 0 ${W} ${H}`} role="img" aria-label="Expected collections per week with likely range" onMouseLeave={()=>setHover(null)}>
        {[0,0.5,1].map(f=><g key={f}><line x1={pad.l} x2={W} y1={y(top*f)} y2={y(top*f)} className="cf-grid"/>
          <text x={pad.l-6} y={y(top*f)+4} className="cf-axis" textAnchor="end">{formatInrCompact(top*f)}</text></g>)}
        {cash.weeks.map((w,i)=>{const x=pad.l+i*band+gap/2, cx=x+bar/2;
          return <g key={w.week_start} onMouseEnter={()=>setHover(i)} onFocus={()=>setHover(i)} tabIndex={0}
            aria-label={`Week of ${day(w.week_start)}: ${formatInrCompact(w.expected)} expected, ${formatInrCompact(w.low)} to ${formatInrCompact(w.high)}`}>
            <rect x={x-gap/2} y={pad.t} width={band} height={H-pad.t-pad.b} fill="transparent"/>
            <path d={`M${x},${y(0)} V${y(w.expected)+4} q0,-4 4,-4 h${bar-8} q4,0 4,4 V${y(0)} Z`}
              className={hover===i?"cf-bar cf-on":"cf-bar"}/>
            <line x1={cx} x2={cx} y1={y(w.low)} y2={y(w.high)} className="cf-range"/>
            <line x1={cx-5} x2={cx+5} y1={y(w.high)} y2={y(w.high)} className="cf-range"/>
            <line x1={cx-5} x2={cx+5} y1={y(w.low)} y2={y(w.low)} className="cf-range"/>
            {(W>=560||i%2===0)&&<text x={cx} y={H-6} className="cf-axis" textAnchor="middle">{day(w.week_start)}</text>}
          </g>;})}
      </svg>
      {hover!=null&&<div className="cf-tip" style={{left:`${Math.min(80,Math.max(20,((pad.l+hover*band+band/2)/W)*100))}%`}} role="status">
        <strong>Week of {day(cash.weeks[hover].week_start)}</strong>
        <span>{formatInrCompact(cash.weeks[hover].expected)} expected</span>
        <span>likely {formatInrCompact(cash.weeks[hover].low)} – {formatInrCompact(cash.weeks[hover].high)}</span></div>}
    </div>
    <details className="cf-table"><summary>Show as a table</summary>
      <table><thead><tr><th>Week of</th><th>Expected</th><th>Likely range</th></tr></thead>
        <tbody>{cash.weeks.map(w=><tr key={w.week_start}><td>{day(w.week_start)}</td><td>{formatInrCompact(w.expected)}</td>
          <td>{formatInrCompact(w.low)} – {formatInrCompact(w.high)}</td></tr>)}</tbody></table></details>
  </section>;
}
