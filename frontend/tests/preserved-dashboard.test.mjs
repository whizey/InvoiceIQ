import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {createHash} from 'node:crypto';
const context={window:{},Intl,URLSearchParams,Date,Number,Math};
vm.runInNewContext(fs.readFileSync(new URL('../public/reference/dashboard-data.js',import.meta.url),'utf8'),context);
const api=context.window.InvoiceIQ;
test('current INR compact display, negatives, foreign currency and unverified source',()=>{
 assert.equal(api.money(125000,'INR',true,false),'₹1.25L');
 assert.equal(api.money(125000,'INR',true,true),'₹1.25L');
 assert.equal(api.money(-12500000,'INR',true,true),'−₹1.25Cr');
 assert.ok(!api.money(100,'USD',true,false).includes('₹'));
 assert.equal(api.money(100,'INR',false,false),'₹100*');
 assert.equal(api.money(816262430,'INR',false,true),'₹81.63 Cr*');
 assert.equal(api.money(null,'INR',true,false),'Unavailable');
});
test('calendar presets include correct quarter and leap-year boundaries',()=>{
 assert.equal(api.range('QTD','2026-10-13').start_date,'2026-10-01');
 assert.equal(api.range('90D','2026-10-13').start_date,'2026-07-16');
 assert.equal(api.range('MTD','2024-02-29').start_date,'2024-02-01');
 assert.equal(api.range('All data','2026-10-13').start_date,'');
});
test('the six cards and chart titles keep their order and plain-language labels',()=>{
 const logic={state:{},props:{},query:new URLSearchParams()};
 assert.deepEqual(Array.from(api.vals(logic).kpis,x=>x.label),['Total billed','Still owed to you','Overdue','Share that is late','Payments received','Risk scores']);
 assert.ok(api.vals(logic).kpis.every(k=>k.value==='—'));
 const html=fs.readFileSync(new URL('../public/reference/overview/index.html',import.meta.url),'utf8');
 const headings=Array.from(html.matchAll(/<h2[^>]*>(.*?)<\/h2>/g),m=>m[1]);
 assert.deepEqual(headings,['Invoiced vs collected','How late is the money owed?','Balance bridge','Recovery pipeline','Where the overdue money sits','Daily collections','What comes due next']);
 assert.ok(!html.includes('sample numbers'));
 assert.ok(!html.includes('Math.random'));
});
test('dashboard dates and balance bridge labels stay readable',()=>{
 const data={as_of:'2026-10-30',generated_at:'2026-10-30T00:00:00Z',currency:'INR',workspace_kind:'showcase',fixed_as_of:false,
   metrics:{invoice_count:0,company_count:0,open:36700000},series:[{period:'2026-09',invoiced:847000,collected:0,open:0}],
   aging:[],risk:[],top:[],daily:[],due:[],invoices:[],chart_notes:{},historical_available:true,
   bridge:{start_date:'2026-10-01',end_date:'2026-10-30',opening:36700000,invoiced:847000,collected:1000000,written_off:0,closing:36547000}};
 const view=api.vals({state:{data},props:{},query:new URLSearchParams()});
 assert.equal(view.asOf,'30 Oct 2026');
 assert.match(view.status,/Report generated/);
 assert.match(view.sourceNote,/Sample workspace/);
 assert.match(api.vals({state:{data:{...data,fixed_as_of:true}},props:{},query:new URLSearchParams()}).sourceNote,/Historical ledger/);
 assert.match(api.vals({state:{data:{...data,workspace_kind:'customer'}},props:{},query:new URLSearchParams()}).sourceNote,/imported or synced/);
 assert.equal(view.bars[0].label,'Sep 26');
 assert.equal(view.bridgeInvoicedLabelBottom,52+view.bridgeInvoicedHeight+8);
 assert.equal(view.bridgeCollectedLabelBottom,78+view.bridgeCollectedHeight+8);
 assert.ok(view.bridgeStartLabelBottom<210);
 const html=fs.readFileSync(new URL('../public/reference/overview/index.html',import.meta.url),'utf8');
 assert.ok(!html.includes('{{t.id}}'));
});
test('invoice table columns retained',()=>{
 const html=fs.readFileSync(new URL('../public/reference/invoices/index.html',import.meta.url),'utf8');
 const headings=Array.from(html.matchAll(/<th\b[^>]*>(.*?)<\/th>/g),m=>m[1]).slice(1,12);

 const labels=headings.map(raw=>
   raw.replace(/<button\b[^>]*>[\s\S]*?<\/button>/g,'')
      .trim()
      .replace(/\s*↓$/, '')
 );

 assert.deepEqual(labels,[
   'Invoice','Company','Posted','Due','Amount',
   'Paid','Balance','Days past due','Status',
   'Late-payment risk','Case'
 ]);

 const expectedKeys=[
   'invoice','company','posted','due','amount',
   'paid','balance','days','status','risk','case'
 ];

 headings.forEach((raw,i)=>{
   const buttons=Array.from(
     raw.matchAll(/<button\b[^>]*data-column-filter="([^"]+)"[^>]*>[\s\S]*?<\/button>/g)
   );

   assert.equal(buttons.length,1,`Filter button missing or duplicated for ${labels[i]}`);
   assert.equal(buttons[0][1],expectedKeys[i]);
   assert.match(raw,/aria-label="Filter [^"]+"/);
   assert.match(raw,/aria-haspopup="dialog"/);
 });

 assert.equal(headings.length,11);
});

test('original CSS is byte-for-byte preserved on all four pages',()=>{
 const inventory=JSON.parse(fs.readFileSync(new URL('./fixtures/dashboard-inventory.json',import.meta.url),'utf8'));
 for(const name of ['overview','invoices','cases','approvals']){
   const html=fs.readFileSync(new URL(`../public/reference/${name}/index.html`,import.meta.url),'utf8');
   const css=Array.from(html.matchAll(/<style>([\s\S]*?)<\/style>/g),m=>m[1]).join('');
   assert.equal(createHash('sha256').update(css).digest('hex'),inventory[name].css_sha256,name);
 }
});
