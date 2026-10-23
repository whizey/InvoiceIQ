"use client";
import Link from "next/link";
import {useEffect, useState} from "react";

const DISMISSED="invoiceiq-guide-dismissed";
const STEPS=[
  {href:"/workspace-dashboard",title:"See the big picture",text:"How much customers owe this business, how much of it is late, and what came in."},
  {href:"/invoices",title:"Open an invoice",text:"Pick any invoice, read its notes, and record what should happen next."},
  {href:"/assistant",title:"Ask the AI assistant",text:"Ask in plain English, for example “Who should we chase first?”. It looks the figures up itself and double-checks every number."},
  {href:"/assistant/reliability",title:"See how it is checked",text:"How often the assistant's answers were verified, how fast it was, and how accurate it is on a test set."},
];

/** Shown only in the sample workspace: what the visitor is looking at, and a short way through it. */
export default function SampleGuide(){
  const [sample,setSample]=useState(false);
  const [open,setOpen]=useState(false);
  useEffect(()=>{
    let live=true;
    fetch("/api/portal/me",{cache:"no-store"}).then(r=>r.ok?r.json():null).then(me=>{
      if(!live||me?.kind!=="showcase")return;
      setSample(true);
      // The guide opens on the first page a visitor sees, then stays folded into the one-line banner.
      let seen=false;
      try{seen=localStorage.getItem(DISMISSED)==="1";localStorage.setItem(DISMISSED,"1")}catch{/* storage unavailable */}
      setOpen(!seen);
    }).catch(()=>{});
    return ()=>{live=false};
  },[]);
  if(!sample)return null;
  function close(){
    setOpen(false);
    try{localStorage.setItem(DISMISSED,"1")}catch{/* storage unavailable */}
  }
  return <aside className="sample-guide" aria-label="About this sample">
    <p className="sample-line"><strong>Sample business.</strong> You are exploring <em>Kaveri Industrial Supplies</em>, a made-up distributor with 40 customers. All figures are sample data, and nothing you do here sends messages or moves money.
      {" "}<button type="button" className="sample-toggle" aria-expanded={open} onClick={()=>open?close():setOpen(true)}>{open?"Hide the guide":"How this demo works"}</button></p>
    {open&&<ol className="sample-steps">
      {STEPS.map((s,i)=><li key={s.href}><Link href={s.href}><span className="step-n">{i+1}</span><strong>{s.title}</strong><span>{s.text}</span></Link></li>)}
    </ol>}
  </aside>;
}
