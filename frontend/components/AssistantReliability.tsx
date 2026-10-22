"use client";

import {useEffect, useState} from "react";

type Share={count:number;pct:number};
type Live={window_days:number;answers:number;answered_by:Record<"primary"|"backup"|"rules"|"unconfigured",Share>;
  models:{provider:string;model:string;answers:number}[];figures:Record<"verified"|"repaired"|"trimmed",Share>;
  written_advice:Record<"allowed"|"blocked",Share>;seconds:{p50:number;p95:number}};
type Stat={mean:number;sd:number};
type EvalModel={runs:number;answered_per_run:number[];accuracy:Stat;grounded:Stat;tool_recall:Stat;
  p50_seconds:Stat;p95_seconds:Stat;by_category:Record<string,Stat>};
type Evaluation={fixture:string;cases:number;generated_on?:string;models:Record<string,EvalModel>};
type Tracing={langsmith:boolean;project:string|null;customer_content:"sent"|"redacted";note_search:"hybrid"|"keywords"};
type Reliability={live:Live;eval:Evaluation|null;tracing?:Tracing};

const WINDOWS=[7,30,90];
const CATEGORY_LABEL:Record<string,string>={core:"Core",tricky:"Tricky wording",followup:"Follow-ups",
  injection:"Injection attempts",out_of_scope:"Out of scope",policy:"Policy",notes:"Notes"};
const pct=(s:Stat)=>`${(s.mean*100).toFixed(1)}% ± ${(s.sd*100).toFixed(1)}`;

function Tile({label,value,note}:{label:string;value:string;note?:string}){
  return <div className="rel-tile"><p className="rel-label">{label}</p><p className="rel-value">{value}</p>
    {note&&<p className="rel-note">{note}</p>}</div>;
}

function Bar({parts}:{parts:{label:string;share:Share;tone:string}[]}){
  const total=parts.reduce((n,p)=>n+p.share.count,0);
  return <div>
    <div className="rel-bar" role="img" aria-label={parts.map(p=>`${p.label} ${p.share.pct}%`).join(", ")}>
      {total>0&&parts.filter(p=>p.share.count>0).map(p=><span key={p.label} className={"rel-seg "+p.tone} style={{width:`${p.share.pct}%`}}/>)}
    </div>
    <ul className="rel-legend">{parts.map(p=><li key={p.label}><span className={"rel-dot "+p.tone}/>{p.label}: {p.share.count} ({p.share.pct}%)</li>)}</ul>
  </div>;
}

export default function AssistantReliability(){
  const [days,setDays]=useState(7);
  const [data,setData]=useState<Reliability|null>(null);
  const [error,setError]=useState("");
  useEffect(()=>{
    let live=true;
    fetch(`/api/portal/assistant-reliability?days=${days}`,{cache:"no-store"})
      .then(async r=>{const b=await r.json();if(!r.ok)throw new Error(b.detail||"Could not load reliability");return b})
      .then(b=>{if(live){setData(b);setError("")}})
      .catch(e=>{if(live)setError(e instanceof Error?e.message:"Could not load reliability")});
    return ()=>{live=false};
  },[days]);

  const live=data?.live;
  const evaluation=data?.eval;
  const categories=evaluation?[...new Set(Object.values(evaluation.models).flatMap(m=>Object.keys(m.by_category)))]:[];
  return <div className="rel">
    <section aria-labelledby="rel-live">
      <div className="rel-head">
        <h2 id="rel-live">Live answers in this workspace</h2>
        <div className="rel-window" role="group" aria-label="Time window">
          {WINDOWS.map(w=><button key={w} type="button" aria-pressed={days===w} onClick={()=>setDays(w)}>{w} days</button>)}
        </div>
      </div>
      {error&&<p className="portal-notice-error" role="alert">{error}</p>}
      {!live&&!error&&<p className="rp-muted">Loading…</p>}
      {live&&live.answers===0&&<p className="rp-muted">No questions asked in the last {live.window_days} days. Ask the assistant something and come back.</p>}
      {live&&live.answers>0&&<>
        <div className="rel-tiles">
          <Tile label="Answers" value={String(live.answers)} note={`last ${live.window_days} days`}/>
          <Tile label="Answered by an AI model" value={`${(live.answered_by.primary.pct+live.answered_by.backup.pct).toFixed(1)}%`}
            note={`${live.answered_by.backup.pct}% by a backup model`}/>
          <Tile label="Figures verified" value={`${(live.figures.verified.pct+live.figures.repaired.pct).toFixed(1)}%`}
            note={`${live.figures.repaired.pct}% after one rewrite`}/>
          <Tile label="Response time" value={`${live.seconds.p50.toFixed(1)} s`} note={`median · ${live.seconds.p95.toFixed(1)} s for the slowest 5%`}/>
        </div>
        <h3>Who answered</h3>
        <Bar parts={[{label:"Main model",share:live.answered_by.primary,tone:"t-good"},
          {label:"Backup model",share:live.answered_by.backup,tone:"t-mid"},
          {label:"Rule-based answer",share:live.answered_by.rules,tone:"t-warn"},
          {label:"No model configured",share:live.answered_by.unconfigured,tone:"t-off"}]}/>
        <h3>How the figures fared</h3>
        <Bar parts={[{label:"Verified first time",share:live.figures.verified,tone:"t-good"},
          {label:"Fixed after a rewrite",share:live.figures.repaired,tone:"t-mid"},
          {label:"Unverified sentence removed",share:live.figures.trimmed,tone:"t-warn"}]}/>
        {live.written_advice.blocked.count>0&&<p className="rp-muted">Written suggestions blocked by review policy: {live.written_advice.blocked.count}.</p>}
        <h3>Models used</h3>
        <table className="rel-table"><thead><tr><th scope="col">Provider</th><th scope="col">Model</th><th scope="col">Answers</th></tr></thead>
          <tbody>{live.models.map(m=><tr key={m.provider+m.model}><td>{m.provider}</td><td>{m.model}</td><td>{m.answers}</td></tr>)}</tbody></table>
      </>}
    </section>

    {data?.tracing&&<p className="rp-muted">LangSmith tracing: {data.tracing.langsmith?`on (project ${data.tracing.project}, customer content ${data.tracing.customer_content})`:"off"} · Note search: {data.tracing.note_search}</p>}
    <section aria-labelledby="rel-eval">
      <h2 id="rel-eval">Answer accuracy (evaluation)</h2>
      <p className="rp-muted">Live answers have no answer key, so accuracy is measured offline: {evaluation?.cases??100} questions with
        hand-worked answers (core figures, tricky wording, follow-ups, prompt-injection attempts, out-of-scope and policy cases), each model
        answering alone, repeated across runs. Mean ± standard deviation across runs.</p>
      {!evaluation&&<p className="rp-muted">No evaluation results published yet.</p>}
      {evaluation&&<>
        <div className="rel-scroll"><table className="rel-table">
          <thead><tr><th scope="col">Model</th><th scope="col">Runs</th><th scope="col">Cases answered</th><th scope="col">Accuracy</th>
            <th scope="col">Figures verified</th><th scope="col">Right tools</th><th scope="col">Median time</th></tr></thead>
          <tbody>{Object.entries(evaluation.models).map(([name,m])=><tr key={name}>
            <td>{name.split(":").slice(1).join(":")}</td><td>{m.runs}</td><td>{m.answered_per_run.join(" / ")} of {evaluation.cases}</td>
            <td>{pct(m.accuracy)}</td><td>{pct(m.grounded)}</td><td>{pct(m.tool_recall)}</td><td>{m.p50_seconds.mean.toFixed(1)} s</td></tr>)}</tbody>
        </table></div>
        <h3>Accuracy by kind of question</h3>
        <div className="rel-scroll"><table className="rel-table">
          <thead><tr><th scope="col">Model</th>{categories.map(c=><th key={c} scope="col">{CATEGORY_LABEL[c]??c}</th>)}</tr></thead>
          <tbody>{Object.entries(evaluation.models).map(([name,m])=><tr key={name}><td>{name.split(":").slice(1).join(":")}</td>
            {categories.map(c=><td key={c}>{m.by_category[c]?pct(m.by_category[c]):"–"}</td>)}</tr>)}</tbody>
        </table></div>
        {evaluation.generated_on&&<p className="rp-muted">Evaluation {evaluation.fixture}, run {evaluation.generated_on}.</p>}
      </>}
    </section>
  </div>;
}
