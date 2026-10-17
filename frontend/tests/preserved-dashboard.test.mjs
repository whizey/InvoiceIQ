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
test('original six cards and the retained chart titles remain in order',()=>{
 const logic={state:{},props:{},query:new URLSearchParams()};
 assert.deepEqual(Array.from(api.vals(logic).kpis,x=>x.label),['Invoiced','Open receivables','Overdue exposure','Overdue share','Collected','Score coverage']);
 assert.ok(api.vals(logic).kpis.every(k=>k.value==='—'));
 const html=fs.readFileSync(new URL('../public/reference/overview/index.html',import.meta.url),'utf8');
 const headings=Array.from(html.matchAll(/<h2[^>]*>(.*?)<\/h2>/g),m=>m[1]);
 assert.deepEqual(headings,['Invoiced vs collected','Aging schedule','Balance bridge','Recovery pipeline','Where the overdue money sits','Daily collections','What comes due next']);
 assert.ok(!html.includes('sample numbers'));
 assert.ok(!html.includes('Math.random'));
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
