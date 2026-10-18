import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
const read = file => readFileSync(new URL('../public/reference/'+file,import.meta.url),'utf8');
const scenarioCode=read('scenario-data.js'),unifiedCode=read('unified-data.js');
const realInvoice={id:'12345678-1234-4234-8234-123456789012', invoice_number:'B2B-REAL-00001',
  company_id:'34567890-1234-4234-8234-123456789012',company:'Customer 0001',
  posting_date:'2015-08-15',due_date:'2015-09-15',amount_total:'2000000.00',
  amount_paid:'2000000.00',balance:'0.00',status:'PAID',probability:0.73,case_id:null};
const sampleHistorical=(q)=>{
  const match=!q.get('search') || realInvoice.invoice_number.toLowerCase().includes(q.get('search').toLowerCase());
  const after=!q.get('start_date') || realInvoice.posting_date>=q.get('start_date');
  const before=!q.get('end_date') || realInvoice.posting_date<=q.get('end_date');
  const status=q.get('status')||'all';
  const candidate=match&&after&&before&&(['all','settled'].includes(status));
  const invoices=candidate&&q.get('page')==='1'?[realInvoice]:[];
  const metrics={invoiced:candidate?'2000000.00':'0.00',billed:candidate?'2000000.00':'0.00',
    collected:candidate?'2000000.00':'0.00',invoice_count:Number(candidate),company_count:Number(candidate),
    open:'0.00',open_count:0,overdue:'0.00',overdue_count:0,scored_open:0,
    partial_count:0,settled_count:Number(candidate),cancelled_count:0,written_off_count:0,
    median_invoice:candidate?'2000000.00':null};
  return {as_of:'2016-03-17',currency:'INR',currency_verified:false,
    metrics,invoices,total:Number(candidate),companies:[{id:realInvoice.company_id,name:'Customer 0001'}],
    series:candidate?[{period:'2015-08-01',invoiced:'2000000.00',collected:'2000000.00',open:'0.00'}]:[],
    aging:[],risk:[],top:[],due:[],daily:[],chart_notes:{},page:Number(q.get('page')||1),page_size:100};
};
function boot(fail=false,priorFails=false){
  const requests=[];const store=new Map();
  const mock=async (url,options)=>{
    const endpoint=new URL(url,'https://invoiceiq.example');
    requests.push({url:String(url),method:options?.method||'GET', headers:options?.headers||{}});
    if(fail) return Response.json({detail:'Public readonly mode not yet enabled'},{status:401});
    if(priorFails && endpoint.searchParams.get('as_of')==='2015-10-01') return Response.json({detail:'Prior cutoff unavailable'},{status:503});
    if(endpoint.pathname==='/api/finance/overview') return Response.json(sampleHistorical(endpoint.searchParams));
    if(endpoint.pathname==='/api/workspace/cases')return Response.json({cases:[{
      id:'55555555-5555-4555-8555-555555555555',company:'Customer 0001',invoice:'B2B-REAL-00001',
      outstanding:'0.00',opened:'2016-01-01',status:'RESOLVED'}]});
    throw Error('Unexpected backend path '+endpoint.pathname);
  };
  const window={location:{search:'',origin:'https://invoiceiq.example',pathname:'/reference/overview/index.html'},
    localStorage:{getItem:k=>store.get(k)||null,setItem:(k,v)=>store.set(k,v)},
    document:{addEventListener(){}},fetch:mock};
  const context=vm.createContext({window,Date,URL,URLSearchParams,Response,Number,Math,JSON,Array,Set,Map,BigInt,Intl,Blob});
  vm.runInContext(scenarioCode,context);vm.runInContext(unifiedCode,context);
  return {window,requests};
}
const fetchData = async(win,q='')=>{
 const r=await win.fetch('/api/finance/overview?'+q);
 assert.equal(r.status,200);
 return r.json();
};
test('Neon and operational examples share one response and preserve provenance',async()=>{
 const {window,requests}=boot();
 const data=await fetchData(window,'grain=month&sort=posting_desc');
 assert.equal(data.historical_source_connected,true);
 assert.equal(data.historical_source_count,1);
 assert.equal(data.illustrative_source_count,468);
 assert.equal(data.total,469);
 assert.equal(data.metrics.invoice_count,469);
 assert.equal(Number(data.metrics.invoiced),Number(window.InvoiceIQScenario.getView(new URLSearchParams()).metrics.invoiced)+2000000);
 assert.ok(data.series.some(x=>x.period==='2015-08-01'));
 assert.ok(data.series.some(x=>x.period.startsWith('2026')));
 assert.ok(data.historical_reason.includes('illustrative'));
 assert.ok(requests.every(r=>!JSON.stringify(r).includes('Bearer')));
 assert.ok(requests.every(r=>r.url.includes('as_of=2016-03-17')));
});
test('all statuses and date-basis filtering change the mixed totals',async()=>{
 const {window}=boot();
 const full=await fetchData(window,'grain=month');
 const lastMonth=await fetchData(window,'start_date=2026-09-01&end_date=2026-10-15&grain=month');
 const overdue=await fetchData(window,'status=overdue&grain=month');
 const payment=await fetchData(window,'date_basis=payment&start_date=2026-09-01&end_date=2026-10-15');
 assert.ok(full.total>lastMonth.total);
 assert.ok(overdue.total<full.total);
 assert.ok(payment.total<=full.total);
 assert.equal(lastMonth.historical_source_count,0);
 assert.ok(overdue.metrics.overdue_share>=0 && overdue.metrics.overdue_share<=100);
});
test('mixed result sorting, paging and company search include historical records',async()=>{
 const {window}=boot();
 const invoice=await fetchData(window,'search=B2B-REAL-00001');
 assert.equal(invoice.total,1);
 assert.equal(invoice.invoices[0].record_origin,'historical_b2b');
 const sorted=await fetchData(window,'sort=amount_desc');
 assert.equal(sorted.invoices[0].invoice_number,'B2B-REAL-00001');
 const p2=await fetchData(window,'page=2&sort=posting_desc');
 const p1=await fetchData(window,'page=1&sort=posting_desc');
 assert.equal(p2.total,p1.total);
 assert.notEqual(p2.invoices[0].id,p1.invoices[0].id);
});
test('browser-only approval actions never mutate Neon; public cases share one list',async()=>{
 const {window,requests}=boot();
 const response=await window.fetch('/api/workspace/cases');
 const c=await response.json();
 assert.ok(c.cases.length>20);
 assert.ok(c.cases.some(item=>item.invoice==='B2B-REAL-00001'));
 const review=window.InvoiceIQScenario.getReviews()[0];
 const decision=await window.fetch('/api/reviewer/reviews/'+review.id+'/approve',{
   method:'POST',body:JSON.stringify({note:'Confirmed this illustrative reviewer action locally'})});
 assert.equal(decision.status,200);
 assert.ok(requests.every(r=>r.method==='GET'));
});
test('historical API failures block incomplete financial totals',async()=>{
 const {window}=boot(true);
 const response=await window.fetch('/api/finance/overview?grain=month');
 assert.equal(response.status,503);
 assert.match((await response.json()).detail,/historical.*unavailable|could not be verified/i);
});
test('public site has no workspace credential gate or source toggle',()=>{
 const fsFile=(p)=>readFileSync(new URL('../'+p,import.meta.url),'utf8');
 const frame=fsFile('components/ReferenceFrame.tsx');
 const overview=read('overview/index.html');
 const login=fsFile('app/connect/page.tsx');
 assert.ok(!frame.includes('Data source'));
 assert.ok(!overview.includes('Workspace access'));
 assert.ok(login.includes('redirect("/")'));
 const finance=fsFile('app/api/finance/[...path]/route.ts');
 assert.ok(!finance.includes("if(!token&&process.env.INVOICEIQ_SAMPLE_SYNTHETIC"));
});

test('comparison reconstructs an earlier as-of, not the current metrics',async()=>{
  const {window,requests}=boot();
  const current=await fetchData(window,'as_of=2026-10-15&grain=month');
  const compared=await fetchData(window,'as_of=2026-10-15&compare_date=2026-09-15&grain=month');
  assert.equal(compared.comparison_as_of,'2026-09-15');
  assert.equal(compared.comparison_coverage,'unified');
  assert.ok(compared.comparison);
  assert.notEqual(compared.comparison.open,compared.metrics.open);
  assert.equal(Number(compared.comparison.invoiced)+Number(window.InvoiceIQScenario.getView(new URLSearchParams('as_of=2026-10-15')).metrics.invoiced)-Number(window.InvoiceIQScenario.getView(new URLSearchParams('as_of=2026-09-15')).metrics.invoiced),Number(current.metrics.invoiced));
  assert.ok(requests.every(r=>!r.url.includes('compare_date=')), 'Backend comparison must be fetched explicitly');
});

test('comparison before historic dataset cutoff is independently queried',async()=>{
  const {window,requests}=boot();
  const compared=await fetchData(window,'as_of=2026-10-15&compare_date=2015-10-01&grain=year');
  assert.equal(compared.comparison_coverage,'unified');
  assert.ok(compared.comparison);
  const upstreams=requests.filter(r=>r.url.includes('/api/finance/overview'));
  assert.ok(upstreams.some(r=>r.url.includes('as_of=2016-03-17')));
  assert.ok(upstreams.some(r=>r.url.includes('as_of=2015-10-01')));
  assert.ok(upstreams.every(r=>!r.url.includes('compare_date=')));
});

test('invalid comparison on or after as-of refuses without fabricated delta',async()=>{
  const {window}=boot();
  for(const date of ['2026-10-15','2026-10-16','not-a-date']){
    const result=await window.fetch('/api/finance/overview?as_of=2026-10-15&compare_date='+date);
    assert.equal(result.status,422,date);
    const body=await result.json();
    assert.match(body.detail,/Comparison date/i);
  }
});

test('comparison surfaces and labels are present in both financial views',()=>{
  const overview=read('overview/index.html');
  const invoices=read('invoices/index.html');
  const adapter=read('dashboard-data.js');
  assert.match(overview,/aria-label="Cutoff comparison"/);
  assert.match(overview,/data-action="clear-compare"/);
  assert.match(overview,/\{\{k\.delta\}\}/);
  assert.match(invoices,/aria-label="Invoice cutoff comparison"/);
  assert.match(invoices,/data-action="compare"/);
  assert.match(adapter,/Comparison date must be earlier|Comparison unavailable|Choose an earlier valid date/);
  assert.match(adapter,/el\.checked=!!logic\.query\.get\('compare_date'\)/);
});

test('comparison fails closed when historical source is unavailable',async()=>{
  const {window}=boot(true);
  const response=await window.fetch('/api/finance/overview?compare_date=2026-09-15&as_of=2026-10-15');
  assert.equal(response.status,503);
  assert.match((await response.json()).detail,/historical.*unavailable|could not be verified/i);
});


test('historical comparison failure stops rather than substituting current values',async()=>{
  const {window}=boot(false,true);
  const response=await window.fetch('/api/finance/overview?as_of=2026-10-15&compare_date=2015-10-01');
  assert.equal(response.status,503);
  const error=await response.json();
  assert.match(error.detail,/Cannot verify the historical comparison/);
});

test('invoice filters and comparison controls have active-state UI',()=>{
  const adapter=read('dashboard-data.js');
  const invoices=read('invoices/index.html');
  assert.match(adapter,/action==='custom-range'/);
  assert.match(adapter,/action==='reset-filters'/);
  assert.match(adapter,/controls\('button\[data-action=company\]'\)/);
  assert.match(adapter,/controls\('button\[data-action=risk\]'\)/);
  assert.match(invoices,/data-action="custom-range"/);
  assert.match(invoices,/data-action="reset-filters"/);
  assert.match(invoices,/\{\{filterSummary\}\}/);
});
