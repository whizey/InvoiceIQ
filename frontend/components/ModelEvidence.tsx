"use client";
import {useEffect,useState} from "react";
import {useRouter} from "next/navigation";
import PortalNavigation from "@/components/PortalNavigation";
import publicEvidence from "@/lib/evidence/baseline-vs-model.json";

type Band={band:string;invoices:number;predicted:number;actual:number};
type Scorer={used:"model"|"baseline";model_auc:number|null;baseline_auc:number|null;evaluated:number;
  late_share:number|null;verdict:string;calibration_by_scorer:{model:Band[];baseline:Band[]}};
type FollowUps={scorer:Scorer;generated:boolean};

const pct=(n:number)=>Math.round(n*100)+"%";
const auc=(n:number|null)=>n==null?"—":n.toFixed(2);

function Calibration({title,bands}:{title:string;bands:Band[]}){
  return <figure className="me-cal">
    <figcaption>{title}</figcaption>
    <table><thead><tr><th>We said</th><th>Actually late</th><th>Invoices</th></tr></thead>
      <tbody>{bands.map(b=><tr key={b.band}><td>{pct(b.predicted)}</td>
        <td><span className="me-bar" style={{width:Math.max(2,Math.round(b.actual*120))}}/>{pct(b.actual)}</td>
        <td>{b.invoices.toLocaleString("en-IN")}</td></tr>)}</tbody></table>
  </figure>;
}

/** Whether the model earns its place over a simple per-customer average, here and on public data. */
export default function ModelEvidence(){
  const router=useRouter();
  const [scorer,setScorer]=useState<Scorer|null>(null);
  const [generated,setGenerated]=useState(false);
  const [error,setError]=useState("");
  useEffect(()=>{
    let active=true;
    (async()=>{
      try{
        const response=await fetch("/api/portal/follow-ups",{cache:"no-store"});
        if(response.status===401){router.replace("/home");return}
        const payload=await response.json() as FollowUps;
        if(!response.ok)throw new Error((payload as unknown as {detail?:string}).detail||"Model comparison unavailable");
        if(active){setScorer(payload.scorer);setGenerated(payload.generated)}
      }catch(e){if(active)setError(e instanceof Error?e.message:"Model comparison unavailable")}
    })();
    return()=>{active=false};
  },[router]);
  const test=publicEvidence.splits.test;
  return <div className="fu-shell">
    <PortalNavigation/>
    <main className="fu-main">
      <p className="settings-eyebrow">Workspace / Model</p>
      <h1 className="fu-title">Model or simple average?</h1>
      <p className="fu-sub">The simple average is each customer&apos;s share of late payments over their last few settled
        invoices. The model is used only when it ranks late payers better on your own history.</p>
      {error&&<p role="alert" className="portal-notice-error">{error}</p>}
      <div className="me-grid">
        <section className="fu-panel" aria-labelledby="me-yours">
          <h2 id="me-yours" className="fu-h2">Your last six months</h2>
          {!scorer&&!error&&<p role="status" className="fu-muted">Comparing on your invoices…</p>}
          {scorer&&generated&&<p className="fu-muted">This workspace&apos;s invoices are generated with a fixed payment
            habit per customer, so whichever score wins here mostly reflects that, not how either method would do on
            a real business. The public-data panel on the right, and the real 2015 ledger workspace, are the fairer
            comparisons.</p>}
          {scorer&&<>
            <div className="me-scores">
              <div className={scorer.used==="model"?"me-used":""}><small>Model</small><strong>{auc(scorer.model_auc)}</strong><span>AUC</span></div>
              <div className={scorer.used==="baseline"?"me-used":""}><small>Simple average</small><strong>{auc(scorer.baseline_auc)}</strong><span>AUC</span></div>
            </div>
            <p className="fu-verdict">{scorer.verdict}</p>
            <p className="fu-muted">{scorer.evaluated.toLocaleString("en-IN")} invoices due in the last six months with a known outcome
              {scorer.late_share!=null?`; ${pct(scorer.late_share)} of them were paid late`:""}. AUC is the chance a late payer is ranked above an on-time one: 0.5 is a coin flip, 1.0 is perfect.</p>
            <Calibration title={`Calibration: ${scorer.used==="model"?"model":"simple average"}`} bands={scorer.calibration_by_scorer[scorer.used]}/>
          </>}
        </section>
        <section className="fu-panel" aria-labelledby="me-public">
          <h2 id="me-public" className="fu-h2">Public B2B invoices</h2>
          <div className="me-scores">
            <div className="me-used"><small>Model</small><strong>{test.model.roc_auc.toFixed(2)}</strong><span>AUC</span></div>
            <div><small>Simple average</small><strong>{test.baseline.roc_auc.toFixed(2)}</strong><span>AUC</span></div>
          </div>
          <p className="fu-verdict">On {test.rows.toLocaleString("en-IN")} real invoices it had never seen (posted Dec 2015 – Jan 2016),
            the model ranked late payers clearly better than the simple average.</p>
          <p className="fu-muted">Its probabilities are not well calibrated, so treat them as a ranking, not exact odds:</p>
          <Calibration title="Calibration: model, public test period" bands={test.model.calibration}/>
        </section>
      </div>
      <section className="fu-panel me-research" aria-labelledby="me-research">
        <h2 id="me-research" className="fu-h2">Research: purchase-to-pay timing</h2>
        <p className="fu-muted">Survival models trained on the public BPI Challenge 2019 purchase-to-pay event log estimate how long
          a process item takes to clear. They are research on process timing, not estimates of when your customers will pay.</p>
      </section>
    </main>
  </div>;
}
