"use client";

import {FormEvent, useRef, useState} from "react";
import {useSpeechToText, useTextToSpeech} from "@/lib/speech";

type Proposal={invoice:string;invoice_id:string;decision:"APPROVE"|"REJECT"|"ESCALATE";reason:string};
type Verification={figures_checked:number;repaired:boolean;removed:string[];blocked_proposals:number};
type Turn={role:"user"|"assistant";content:string;proposals?:Proposal[];verification?:Verification;tools?:string[]};
const DECISION_LABEL={APPROVE:"Approve recovery",REJECT:"Reject",ESCALATE:"Escalate"};

function checkNote(v?:Verification,tools?:string[]){
  if(!v)return "";
  const parts=[tools?.length?`Looked up: ${[...new Set(tools)].map(t=>t.replace(/_/g," ")).join(", ")}`:"",
    v.figures_checked?`${v.figures_checked} figure${v.figures_checked===1?"":"s"} checked against your records`:"",
    v.removed.length?`${v.removed.length} unverified claim${v.removed.length===1?"":"s"} removed`:""];
  return parts.filter(Boolean).join(" · ");
}
const SUGGESTIONS=[
  "Which overdue invoices should we chase first?",
  "How have collections trended over the last six months?",
  "Which customers pay late most often?",
  "What falls due in the next 30 days?",
  "If our largest debtor paid today, how much would still be overdue?",
  "What is waiting for review?",
];

export default function AssistantChat(){
  const [turns,setTurns]=useState<Turn[]>([]);
  const [question,setQuestion]=useState("");
  const [asking,setAsking]=useState(false);
  const [error,setError]=useState("");
  const [model,setModel]=useState("");
  const end=useRef<HTMLDivElement>(null);
  const viaVoice=useRef(false);
  const tts=useTextToSpeech();
  const [speakingIndex,setSpeakingIndex]=useState<number|null>(null);
  const [applied,setApplied]=useState<Record<string,string>>({});
  const stt=useSpeechToText((text,final)=>{
    setQuestion(text);
    if(final&&text.length>=3){viaVoice.current=true;void ask(null,text)}
  });

  async function ask(event:FormEvent|null,preset?:string){
    event?.preventDefault();
    const q=(preset??question).trim();
    if(q.length<3||asking)return;
    const history=turns.slice(-6).map(({role,content})=>({role,content}));
    setAsking(true);setError("");setQuestion("");
    setTurns(t=>[...t,{role:"user",content:q}]);
    try{
      const r=await fetch("/api/portal/assistant",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({question:q,history})});
      const b=await r.json();
      if(!r.ok)throw new Error(b.detail||"The assistant could not answer");
      setTurns(t=>[...t,{role:"assistant" as const,content:b.answer,proposals:b.proposals,verification:b.verification,tools:b.tools_used}].slice(-20));
      setModel(b.fallback_used?`${b.model} (fallback)`:b.model);
      if(viaVoice.current&&tts.supported){tts.speak(b.answer);setSpeakingIndex(history.length+1)}
    }catch(e){setError(e instanceof Error?e.message:"The assistant could not answer")}
    finally{viaVoice.current=false;setAsking(false);setTimeout(()=>end.current?.scrollIntoView({behavior:"smooth",block:"end"}),50)}
  }

  async function apply(p:Proposal){
    const key=p.invoice_id+p.decision;
    setApplied(a=>({...a,[key]:"Saving…"}));
    try{
      const r=await fetch("/api/portal/decisions",{method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({invoice_id:p.invoice_id,decision:p.decision,note:p.reason.slice(0,500)})});
      const b=await r.json().catch(()=>({}));
      setApplied(a=>({...a,[key]:r.ok?"Saved":(b.detail||"Could not save")}));
    }catch{setApplied(a=>({...a,[key]:"Could not save"}))}
  }

  return <div className="ai-chat">
    <div className="ai-thread" aria-live="polite">
      {turns.length===0&&<div className="ai-empty">
        <h2>Ask about your receivables</h2>
        <p>The assistant looks up this workspace&apos;s balances, ageing, payment history, customer track records, model risk and review decisions, and every figure it states is checked against those records. It can suggest a review decision; nothing is saved until you apply it.</p>
        <div className="rp-chips">{SUGGESTIONS.map(s=><button key={s} type="button" disabled={asking} onClick={()=>void ask(null,s)}>{s}</button>)}</div>
      </div>}
      {turns.map((t,i)=><div key={i} className={t.role==="user"?"rp-msg rp-user":"rp-msg rp-answer"}><p>{t.content}</p>
        {t.role==="assistant"&&tts.supported&&<button type="button" className="ai-listen" aria-label={tts.speaking&&speakingIndex===i?"Stop reading":"Read answer aloud"}
          onClick={()=>{if(tts.speaking&&speakingIndex===i){tts.stop();setSpeakingIndex(null)}else{tts.speak(t.content);setSpeakingIndex(i)}}}>
          {tts.speaking&&speakingIndex===i?"■ Stop":"▶ Listen"}</button>}
        {t.proposals?.map(p=>{const state=applied[p.invoice_id+p.decision];return <div key={p.invoice_id+p.decision} className="ai-proposal">
          <p><strong>Suggested: {DECISION_LABEL[p.decision]} {p.invoice}</strong> · {p.reason}</p>
          <button type="button" disabled={!!state} onClick={()=>void apply(p)}>{state||"Apply"}</button>
        </div>})}
        {t.role==="assistant"&&checkNote(t.verification,t.tools)&&<p className="rp-muted ai-check">{checkNote(t.verification,t.tools)}</p>}</div>)}
      {asking&&<div className="rp-msg rp-answer" role="status"><p>Analysing your workspace…</p></div>}
      {(error||stt.error)&&<p className="portal-notice-error" role="alert">{error||stt.error}</p>}
      <div ref={end}/>
    </div>
    <form onSubmit={e=>void ask(e)} className="ai-form">
      {stt.supported&&<button type="button" className={stt.listening?"ai-mic ai-mic-on":"ai-mic"} aria-pressed={stt.listening}
        aria-label={stt.listening?"Stop voice input":"Ask by voice"} disabled={asking||stt.transcribing} onClick={()=>stt.listening?stt.stop():(tts.stop(),stt.start())}>
        {stt.transcribing?"Transcribing…":stt.listening?"● Listening — tap to stop":"🎤 Speak"}</button>}
      <input value={question} onChange={e=>setQuestion(e.target.value)} maxLength={600} aria-label="Question for InvoiceIQ AI"
        placeholder="Ask about overdue exposure, approvals or priorities"/>
      <button type="submit" disabled={asking||question.trim().length<3}>{asking?"Thinking…":"Ask"}</button>
      {turns.length>0&&<button type="button" className="ai-clear" disabled={asking} onClick={()=>{tts.stop();setTurns([]);setError("")}}>New chat</button>}
    </form>
    <p className="rp-muted">{model?`Model: ${model} · `:""}Responses are generated by a language model; verify figures before acting.</p>
  </div>;
}
