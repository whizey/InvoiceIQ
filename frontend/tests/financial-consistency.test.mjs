import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
const source = file => readFileSync(new URL('../public/reference/'+file,import.meta.url),'utf8');
function boot(backend = null){
  const entries=new Map(),calls=[];
  const mock = async resource=>{
    calls.push(String(resource));
    if(backend) return backend(new URL(String(resource),'https://invoiceiq.local'));
    throw Error('Unexpected historic API request');
  };
  const window={location:{origin:'https://invoiceiq.local',pathname:'/',search:''},
    document:{addEventListener(){}},fetch:mock,
    localStorage:{getItem:key=>entries.get(key)||null,setItem:(key,value)=>entries.set(key,value)}};
  const env=vm.createContext({window,Date,URL,URLSearchParams,Response,Number,Math,JSON,Array,Set,Map,BigInt,Intl,Blob});
  vm.runInContext(source('scenario-data.js'),env);
  vm.runInContext(source('unified-data.js'),env);
  return {window,calls};
}
const sum = (items,key) => items.reduce((t,item)=>t+Number(item[key]||0),0);
function near(a,b,label){assert.ok(Math.abs(Number(a)-Number(b))<=0.02,`${label}: ${a} != ${b}`);}
const original=boot().window.InvoiceIQScenario;
const today=new Date(); today.setUTCHours(0,0,0,0);
const days=n=>new Date(today.getTime()+n*86400000).toISOString().slice(0,10);
const rangeStart=days(-45),rangeEnd=days(0),priorDate=days(-30),recentStart=days(-89);
const filters=[
  '', 'status=overdue','status=settled','status=cancelled','status=written_off',
  'status=partial','status=open','risk=high','risk=medium','risk=low',
  'search=IQ-SC-00001','company_id='+original.companies[0].id,
  'start_date='+rangeStart+'&end_date='+rangeEnd,
  'start_date='+rangeStart+'&end_date='+rangeEnd+'&status=settled',
  'date_basis=due&start_date='+rangeStart+'&end_date='+rangeEnd,
  'date_basis=payment&start_date='+rangeStart+'&end_date='+rangeEnd,
  'grain=year','grain=quarter','grain=week&start_date='+recentStart+'&end_date='+rangeEnd,
  'grain=day&start_date='+rangeStart+'&end_date='+rangeEnd,
  'as_of='+priorDate,'as_of='+priorDate+'&risk=high',
  'as_of='+priorDate+'&start_date='+recentStart+'&end_date='+priorDate
];
test('all filter combinations reconcile invoices, balances, aging, risk and charts',()=>{
  for(const f of filters){
    const d=original.getView(new URLSearchParams(f));
    assert.ok(!d.error,`${f} => ${d.error}`);
    assert.equal(d.metrics.invoice_count,d.total,f+' invoice count');
    const m=d.metrics;
    assert.ok(Number(m.overdue)<=Number(m.open)+0.01,f+' overdue exceeds open');
    near(sum(d.aging,'amount'),m.open,f+' aging');
    near(sum(d.risk,'amount'),m.open,f+' risk');
    near(sum(d.due,'amount'),m.open,f+' due schedule');
    near(sum(d.series,'invoiced'),m.invoiced,f+' invoiced series');
    near(sum(d.series,'collected'),m.collected,f+' cash series');
    near(Object.values(d.allocation).reduce((a,v)=>a+Number(v),0),m.billed,f+' allocation');
    const q=new URLSearchParams(f);
    if(!q.has('start_date')&&!q.has('end_date')){
      near(d.allocation.collected,m.collected,f+' receipts');
    }
    const countNew=d.pipeline.new+d.pipeline.investigating+d.pipeline.action+d.pipeline.resolved;
    assert.ok(countNew>=d.pipeline.awaiting, f+' pipeline pending');
  }
});
test('filtered settlements do not show unrelated portfolio collections',()=>{
  const get=(f)=>original.getView(new URLSearchParams(f));
  const all=get(''), settled=get('status=settled'), cancelled=get('status=cancelled'), high=get('risk=high');
  near(settled.metrics.collected,settled.metrics.invoiced,'settled receipts');
  assert.equal(Number(cancelled.metrics.collected),0);
  assert.ok(Number(high.metrics.collected)<Number(all.metrics.collected));
  assert.ok(Number(get('search=IQ-SC-00001').metrics.collected)<=Number(get('search=IQ-SC-00001').metrics.invoiced));
});
test('historical outage cannot silently masquerade as complete all-data totals',async()=>{
  const {window}=boot(async()=>Response.json({detail:'Service unavailable'},{status:503}));
  const r=await window.fetch('/api/finance/overview?grain=month');
  assert.equal(r.status,503);
  assert.match((await r.json()).detail,/cannot|unavailable|verified/i);
});
test('2026-only dates need no 2016 remote query',async()=>{
  const {window,calls}=boot();
  const r=await window.fetch('/api/finance/overview?start_date='+rangeStart+'&end_date='+rangeEnd+'&grain=week&date_basis=payment');
  assert.equal(r.status,200);
  const d=await r.json();
  assert.equal(calls.length,0);
  assert.equal(d.historical_source_count,0);
  assert.ok(d.total>0);
});
test('mixed-source median is not represented by unrelated source median',async()=>{
  const {window}=boot(async(url)=>{
    const q=url.searchParams;
    return Response.json({as_of:'2016-03-17',historical_available:true,
      currency:'INR',currency_verified:false,
      metrics:{invoice_count:1,company_count:1,invoiced:'100.00',billed:'100.00',open:'0.00',overdue:'0.00',collected:'100.00',median_invoice:'100.00'},
      invoices:q.get('page')==='1'?[{id:'a',posting_date:'2015-01-01',due_date:'2015-02-01',amount_total:'100.00'}]:[],
      total:1,companies:[],series:[],aging:[],risk:[],top:[],daily:[],due:[]});
  });
  const d=await (await window.fetch('/api/finance/overview')).json();
  assert.equal(d.total,469);
  assert.equal(d.metrics.median_invoice,null);
});
