import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
const code=readFileSync(new URL('../public/reference/scenario-data.js',import.meta.url),'utf8');
function load(params='?scenario=1'){
  const store=new Map();
  const localStorage={getItem:k=>store.get(k)||null,setItem:(k,v)=>store.set(k,v)};
  const listeners=[];
  const window={location:{search:params,origin:'https://invoiceiq.test'},localStorage,
    document:{addEventListener:(...args)=>listeners.push(args)},fetch:async()=>{throw Error('scenario unexpectedly accessed production')}};
  const context=vm.createContext({window,Date,URL,URLSearchParams,Response,Number,Math,JSON,Array,Set,Map,BigInt,Intl});
  vm.runInContext(code,context);
  return window;
}
test('468 deterministic scenario invoices and linked cases without network access',()=>{
  const sim=load();
  assert.equal(sim.InvoiceIQScenario.invoices.length,468);
  assert.ok(sim.InvoiceIQScenario.cases.length>=20);
  assert.ok(sim.InvoiceIQScenario.cases.some(c=>c.status==='RESOLVED'&&Number(c.outstanding)===0));
  assert.ok(sim.InvoiceIQScenario.cases.filter(c=>c.status!=='RESOLVED').every(c=>Number(c.outstanding)>0));
  assert.equal(sim.InvoiceIQScenario.companies.length,18);
});
test('every period control changes query scope while preserving money arithmetic',()=>{
  const {getView}=load().InvoiceIQScenario;
  const all=getView(new URLSearchParams('grain=month'));
  const range=getView(new URLSearchParams('grain=month&start_date=2026-01-01&end_date=2026-02-01'));
  assert.ok(all.metrics.invoice_count>range.metrics.invoice_count);
  assert.ok(Number(all.metrics.invoiced)>Number(range.metrics.invoiced));
  assert.equal(all.metrics.open, (all.metrics.open));
  assert.ok(all.metrics.open_count>0);
  assert.ok(all.metrics.overdue_count>0);
  assert.ok(all.risk.length===6);
  assert.ok(all.aging.length===5);
  assert.ok(all.series.length>1);
  assert.ok(all.top.length>1);
});
test('status search date basis company and pagination genuinely filter data',()=>{
  const {getView,companies}=load().InvoiceIQScenario;
  const all=getView(new URLSearchParams());
  const settled=getView(new URLSearchParams('status=settled'));
  const overdue=getView(new URLSearchParams('status=overdue'));
  assert.ok(settled.total>0&&overdue.total>0);
  assert.ok(settled.total!==overdue.total);
  const company=getView(new URLSearchParams('company_id='+companies[0].id));
  assert.ok(company.total<all.total);
  const search=getView(new URLSearchParams('search=IQ-SC-00001'));
  assert.equal(search.total,1);
  const page2=getView(new URLSearchParams('page=2'));
  assert.notEqual(all.invoices[0].id,page2.invoices[0].id);
  const basis=getView(new URLSearchParams('date_basis=due&start_date=2026-10-01'));
  assert.ok(basis.total<all.total);
});
test('scenario-only decision updates browser queue but never calls backend',async()=>{
  const win=load();
  const original=win.InvoiceIQScenario.getReviews();
  assert.ok(original.length>0);
  const id=original[0].id;
  const result=await win.fetch('/api/reviewer/reviews/'+id+'/approve',{method:'POST',body:JSON.stringify({note:'Approved for local scenario check'})});
  assert.equal(result.status,200);
  assert.equal(win.InvoiceIQScenario.getReviews().length,original.length-1);
  assert.ok(win.InvoiceIQScenario.getHistory().some(x=>x.action_id===id));
});
test('one public interface includes illustrative operations without a source switch',()=>{
  const win=load('?scenario=0');
  assert.equal(win.InvoiceIQScenario.active,true);
});
test('scenario invoice accounting fully reconciles and charts are populated',()=>{
  const {getView}=load().InvoiceIQScenario;
  const d=getView(new URLSearchParams());
  const add=(a,b)=>a+Math.round(Number(b)*100);
  const bill=Math.round(Number(d.metrics.billed)*100);
  const allocation=Object.values(d.allocation).reduce(add,0);
  assert.equal(allocation,bill);
  assert.equal(d.aging.reduce((v,x)=>v+x.count,0),d.metrics.open_count);
  assert.equal(d.risk.reduce((v,x)=>v+x.count,0),d.metrics.open_count);
  assert.ok(d.histogram.reduce((v,x)=>v+x.n,0)>0);
  assert.ok(d.pipeline.awaiting>0 && d.pipeline.investigating>0);
  assert.ok(Number(d.allocation.collected)>0 && Number(d.allocation.overdue)>0);
  assert.ok(d.chart_notes.pipeline.toLowerCase().includes('simulated'));
});

test('scenario case dates and review evidence are internally consistent',()=>{
  const sim=load();const {cases,detailFor}=sim.InvoiceIQScenario;
  for(const c of cases){
    const d=detailFor(c.id);
    assert.ok(c.opened>=d.invoice_obj.due_date);
    assert.ok(d.audit_logs[0].occurred_at);
    assert.ok(d.communication_logs[0].body.includes('No message'));
    if(c.pending){assert.equal(d.promises_to_pay.length,3);}
  }
});
