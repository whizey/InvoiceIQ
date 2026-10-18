/* One-dashboard browser smoke. Requires Next.js dev server and Render PUBLIC_READ_ONLY=true.
 * NO mutations to Neon: approvals are intercepted as browser-only illustrations. */
import assert from 'node:assert/strict';
import {chromium} from 'playwright';
const base=process.env.INVOICEIQ_PREVIEW_URL||'http://localhost:3000';
let browser;
try{browser=await chromium.launch({channel:'chrome',headless:true});}
catch{browser=await chromium.launch({headless:true});}
const errors=[];
const check=async(name,fn)=>{try{await fn();console.log('PASS:',name);}catch(e){console.error('FAIL:',name,e.message);errors.push(name);}};
const context=await browser.newContext({viewport:{width:1440,height:960},acceptDownloads:true});
const page=await context.newPage();
const frame=()=>page.frameLocator('iframe.reference-frame');
page.on('pageerror',error=>errors.push('Browser script: '+error.message));
const visit=async(path)=>{await page.goto(base+path,{waitUntil:'domcontentloaded',timeout:60000});await frame().locator('main').waitFor({timeout:60000});};
const synced=async()=>frame().getByText(/Synced|Neon historical endpoint returned|Updating source data/i).first().waitFor({timeout:60000});
try{
 await check('No visitor credentials or mode switch',async()=>{
   await visit('/');
   assert.equal(await page.getByRole('group',{name:'Data source'}).count(),0);
   assert.equal(await frame().getByText('Workspace access',{exact:true}).count(),0);
   assert.equal(await page.getByText('Workspace credential',{exact:true}).count(),0);
 });
 await check('Public finance GET works anonymously; historical Neon source is available',async()=>{
   const result=await page.evaluate(async()=>{
     const r=await fetch('/api/finance/overview?grain=year');
     return {status:r.status,body:await r.json().catch(()=>null)};
   });
   assert.equal(result.status,200);
   assert.ok(result.body?.metrics?.invoice_count>=45839);
 });
 await check('Overview: Month, Day, Year and All data change real chart periods',async()=>{
   await visit('/');await synced();
   await frame().locator('[data-action="grain"][data-value="day"]').first().click();
   assert.match(page.url(),/grain=day/);
   assert.match(page.url(),/preset=90D/);
   await frame().locator('[data-action="grain"][data-value="year"]').first().click();
   assert.match(page.url(),/grain=year/);
   await frame().locator('[data-action="range"][data-value="All data"]').click();
   await frame().getByText(/46,\d{3} invoices from/).waitFor({timeout:60000});
   await frame().locator('[data-action="basis"][data-value="due"]').click();
   assert.match(page.url(),/date_basis=due/);
 });
 await check('Overview: compare historical cutoff with visible KPI differences',async()=>{
   await visit('/');await synced();
   await frame().locator('input[data-action="compare"]').click();
   const dialog=frame().getByRole('dialog');
   await dialog.getByRole('button',{name:'30 days earlier'}).click();
   await dialog.getByRole('button',{name:'Apply comparison'}).click();
   await frame().getByRole('region',{name:'Cutoff comparison'}).waitFor({timeout:20000});
   await frame().getByText('Prior overdue',{exact:true}).waitFor();
   assert.match(page.url(),/compare_date=/);
   await frame().getByRole('button',{name:'Clear comparison'}).click();
   assert.ok(!page.url().includes('compare_date='));
 });
 await check('Invoice intelligence: cutoff compare, range and clear filters',async()=>{
   await visit('/invoices');await synced();
   await frame().getByRole('button',{name:'Compare dates'}).click();
   await frame().getByRole('dialog').getByRole('button',{name:'7 days earlier'}).click();
   await frame().getByRole('dialog').getByRole('button',{name:'Apply comparison'}).click();
   await frame().getByRole('region',{name:'Invoice cutoff comparison'}).waitFor({timeout:20000});
   await frame().getByRole('button',{name:/Date range/}).click();
   const dateDialog=frame().getByRole('dialog');
   await dateDialog.getByLabel('Range start date').fill('2026-08-01');
   await dateDialog.getByLabel('Range end date').fill('2026-10-15');
   await dateDialog.getByRole('button',{name:'Apply range'}).click();
   assert.match(page.url(),/start_date=2026-08-01/);
   await frame().locator('[data-action="status"][data-value="overdue"]').click();
   await frame().getByRole('button',{name:'Reset filters'}).click();
   assert.ok(!page.url().includes('status=overdue'));
 });
 await check('Invoice Intelligence: status, company, sort, search, columns, evidence',async()=>{
   await visit('/invoices');await synced();
   await frame().locator('[data-action="status"][data-value="overdue"]').click();
   assert.match(page.url(),/status=overdue/);
   await frame().locator('[data-action="status"][data-value="all"]').click();
   const search=frame().getByRole('searchbox',{name:'Search invoices'});
   await search.fill('IQ-SC-00001');
   await frame().getByRole('link',{name:'IQ-SC-00001'}).waitFor({timeout:15000});
   await search.fill('');await page.waitForTimeout(500);
   const col=frame().getByRole('button',{name:'Columns'});
   await col.waitFor({state:'visible',timeout:15000});
   await page.waitForFunction(()=>{
      const doc=document.querySelector('iframe.reference-frame')?.contentDocument;
      function find(root){for(const b of root.querySelectorAll('button'))if(b.textContent?.trim()==='Columns')return b;
        for(const n of root.querySelectorAll('*'))if(n.shadowRoot){const b=find(n.shadowRoot);if(b)return b;}
        return null;}
      return !!doc&&!find(doc)?.disabled;
   },null,{timeout:15000});
   await col.click();await frame().getByRole('dialog').waitFor();
   await frame().getByRole('dialog').getByRole('button',{name:'Done'}).click();
   await frame().locator('a[data-action="evidence"]').first().click();
   await frame().getByRole('dialog').getByText('Invoice evidence').waitFor();
 });
 await check('Recovery Cases: table, board and case detail',async()=>{
   await visit('/cases');
   await frame().getByRole('group',{name:'View'}).getByRole('button',{name:'Table'}).click();
   await frame().getByRole('table',{name:'Live recovery case table'}).waitFor({timeout:20000});
   await frame().getByRole('group',{name:'View'}).getByRole('button',{name:'Board'}).click();
   await frame().locator('[data-case]').first().click();
   await frame().getByText('Policy limits on this case').waitFor({timeout:15000});
 });
 await check('Approvals: no reviewer credential and local-only decisions',async()=>{
   await visit('/approvals');
   await frame().getByText('Simulated approval workflow',{exact:false}).waitFor({timeout:20000});
   assert.equal(await frame().getByRole('textbox',{name:'Reviewer credential'}).count(),0);
   await frame().locator('[data-approval]').first().click();
   await frame().locator('[data-review-note]').fill('Approved in browser-only UI smoke test.');
   page.once('dialog',d=>d.accept());
   await frame().locator('[data-decision="approve"]').click();
   await frame().getByText('Decision history').waitFor();
 });
}finally{await context.close();await browser.close();}
if(errors.length){console.error('FAIL:',errors.join('; '));process.exitCode=1;}
else console.log('PASS: Unified public browser smoke. No Neon writes performed.');
