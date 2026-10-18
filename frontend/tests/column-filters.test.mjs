import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const base=new URL('../public/reference/',import.meta.url);
const scenarioCode=fs.readFileSync(new URL('scenario-data.js',base),'utf8');
const unifiedCode=fs.readFileSync(new URL('unified-data.js',base),'utf8');
const uiCode=fs.readFileSync(new URL('column-filters.js',base),'utf8');
const html=fs.readFileSync(new URL('invoices/index.html',base),'utf8');
const schema=fs.readFileSync(new URL('../../backend/app/schemas/preserved_dashboard.py',import.meta.url),'utf8');
const sql=fs.readFileSync(new URL('../../backend/app/services/preserved_dashboard.py',import.meta.url),'utf8');
function scenario(){
 const window={location:{origin:"https://invoiceiq.local",pathname:"/",search:""},
  document:{addEventListener(){}},fetch:async()=>{throw Error("Unexpected API request")},
  localStorage:{getItem:()=>null,setItem:()=>{}},setInterval:()=>0};
 const context=vm.createContext({window,Date,URL,URLSearchParams,Response,console});
 vm.runInContext(scenarioCode,context);return window.InvoiceIQScenario;
}
const q=(params={})=>new URLSearchParams({as_of:new Date().toISOString().slice(0,10),...params});
test('all eleven column headers have accessible filters',()=>{
 for(const key of ['invoice','company','posted','due','amount','paid','balance','days','status','risk','case'])
  assert.match(html,new RegExp('data-column-filter="'+key+'" aria-label="Filter '));
 assert.match(uiCode,/composedPath/);
});
test('new SQL API declares full-cohort financial and case fields and capability marker',()=>{
 assert.match(schema,/column_filters_version: int = 1/);
 for(const key of ['invoice_number','company_search','posted_from','due_to','amount_min','paid_max','balance_min','days_max','risk_max','case_presence']){
  assert.ok(schema.includes(key),key);assert.ok(sql.includes(key),key);
 }
 assert.match(sql,/payment_invoice_ids/);
 assert.match(unifiedCode,/body.column_filters_version!==1/);
});
test('invoice and company column filtering precede pagination',()=>{
 const s=scenario();const item=s.invoices[12];
 const x=s.getView(q({invoice_number:item.invoice_number}));
 assert.equal(x.total,1);assert.equal(x.invoices[0].invoice_number,item.invoice_number);
 const company=s.getView(q({company_search:item.company.slice(0,8)}));
 assert.ok(company.total>0&&company.total<468);
 assert.ok(company.invoices.every(r=>r.company.toLowerCase().includes(item.company.slice(0,8).toLowerCase())));
});
test('amount paid balance and risk ranges maintain full data accounting',()=>{
 const s=scenario();
 const filters=[{amount_min:'80000',amount_max:'130000'},
  {paid_min:'5000'},{balance_min:'10000',balance_max:'40000'},
  {risk_min:'35',risk_max:'65'},{days_min:'1',days_max:'70'},
  {case_presence:'yes'},{case_presence:'no'},
  {status:'partial',risk:'medium',amount_min:'15000'}];
 for(const f of filters){
  const full=s.getView(q({...f,page_size:'100'}));
  assert.ok(!full.error,JSON.stringify(f));
  assert.equal(full.total,full.metrics.invoice_count);
  assert.equal(full.page_size,100);
  assert.ok(full.invoices.length<=100);
  // Check each returned page row meets its own requested constraints.
  for(const row of full.invoices){
   for(const [prefix,field] of [['amount','amount_total'],['paid','amount_paid'],['balance','balance']]){
    if(f[prefix+'_min'])assert.ok(Number(row[field])+1e-6>=Number(f[prefix+'_min']));
    if(f[prefix+'_max'])assert.ok(Number(row[field])-1e-6<=Number(f[prefix+'_max']));
   }
   if(f.risk_min)assert.ok(Number(row.probability)*100+1e-6>=Number(f.risk_min));
   if(f.risk_max)assert.ok(Number(row.probability)*100-1e-6<=Number(f.risk_max));
   if(f.case_presence==='yes')assert.ok(row.case_id);
   if(f.case_presence==='no')assert.equal(row.case_id,null);
  }
 }
});
test('column filters combine, pagination retains same total',()=>{
 const s=scenario(),params=q({company_search:'Traders',risk_min:'20',amount_min:'15000',page_size:'4'});
 const a=s.getView(params);const b=s.getView(q({...Object.fromEntries(params),page:'2'}));
 assert.ok(a.total>0);assert.equal(a.total,b.total);
 assert.ok(a.invoices.every(r=>r.company.includes('Traders')));
 assert.deepEqual(new Set(a.invoices.map(r=>r.id)).size,a.invoices.length);
 const idsA=new Set(a.invoices.map(r=>r.id));assert.ok(b.invoices.every(r=>!idsA.has(r.id)));
});
test('column sorting works before pagination',()=>{
 const s=scenario();
 for(const [sort,field,sign] of [['amount_asc','amount_total',1],['amount_desc','amount_total',-1],
  ['risk_asc','probability',1],['balance_desc','balance',-1]]){
  const out=s.getView(q({sort,page_size:'100'}));const values=out.invoices.map(r=>Number(r[field]));
  for(let i=1;i<values.length;i++)assert.ok((values[i]-values[i-1])*sign>=-1e-6,sort);
 }
});
test('an old historical API cannot silently ignore new filters',async()=>{
 let requests=0;
 const mock=async()=>{requests++;return new Response(JSON.stringify({
  metrics:{invoice_count:45839,company_count:69,billed:'50000000',invoiced:'50000000',open:'0',
   overdue:'0',collected:'50000000'},invoices:[],series:[],total:45839,page:1,page_size:100,
  historical_available:true,aging:[],risk:[],due:[],companies:[],daily:[],top:[]
 }),{status:200,headers:{'Content-Type':'application/json'}});};
 const window={location:{origin:'https://invoiceiq.local',pathname:'/invoices',search:''},
  document:{addEventListener(){}},fetch:mock,
  localStorage:{getItem:()=>null,setItem:()=>{}},setInterval:()=>0};
 const env=vm.createContext({window,Date,URL,URLSearchParams,Response,console,Intl});
 vm.runInContext(scenarioCode,env);
 vm.runInContext(unifiedCode,env);
 const response=await window.fetch('/api/finance/overview?amount_min=30000&start_date=2015-01-01&end_date=2015-12-31');
 assert.equal(response.status,503);
 assert.ok(requests>0);
 assert.match((await response.json()).detail,/deploy|updated FastAPI/i);
});
test('a present-day invoice column filter does not wake the historic 2016 API',async()=>{
 let requests=0;
 const window={location:{origin:'https://invoiceiq.local',pathname:'/invoices',search:''},
  document:{addEventListener(){}},fetch:async()=>{requests++;throw Error('Unexpected historical request')},
  localStorage:{getItem:()=>null,setItem:()=>{}},setInterval:()=>0};
 const env=vm.createContext({window,Date,URL,URLSearchParams,Response,console,Intl});
 vm.runInContext(scenarioCode,env);
 vm.runInContext(unifiedCode,env);
 const today=new Date();today.setUTCHours(0,0,0,0);
 const start=new Date(today.getTime()-30*86400000).toISOString().slice(0,10);
 const end=today.toISOString().slice(0,10);
 const response=await window.fetch('/api/finance/overview?start_date='+start+'&end_date='+end+'&amount_min=20000');
 assert.equal(response.status,200);
 assert.equal(requests,0);
 const data=await response.json();assert.ok(data.metrics.invoice_count>0);
});
