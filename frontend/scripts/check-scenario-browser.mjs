/**
 * Read-only browser smoke tests for the InvoiceIQ scenario frontend.
 * Start Next.js first: npm run dev
 * Then run: node scripts/check-scenario-browser.mjs
 * No requests are sent to the real backend while scenario mode is active.
 */
import assert from 'node:assert/strict';
import { chromium } from 'playwright';

const base = process.env.INVOICEIQ_PREVIEW_URL || 'http://localhost:3000';
let browser;
try {
  browser = await chromium.launch({channel:'chrome',headless:true});
} catch {
  try { browser = await chromium.launch({headless:true}); }
  catch (e) { console.error('Browser unavailable. Install Playwright Chrome using the existing project setup.'); throw e; }
}
const failures = [];
const test = async (name,fn) => {
  try { await fn(); console.log('PASS:',name); }
  catch(error) { failures.push(name);console.error('FAIL:',name,'—',error.message); }
};
const context = await browser.newContext({acceptDownloads:true, viewport:{width:1440,height:950}});
const page=await context.newPage();
page.on('pageerror',error=>console.error('BROWSER ERROR:',error.message));
const frame=()=>page.frameLocator('iframe.reference-frame');
const action=async (path,fn) => { await page.goto(base+path,{waitUntil:'domcontentloaded',timeout:45000});await frame().locator('main').waitFor({state:'visible',timeout:45000});await fn(); };
try {
  await test('Overview populates and date range responds',async()=>action('/?scenario=1',async()=>{
    const count=frame().locator('[data-action="range"]');
    await count.first().waitFor();
    await frame().locator('[aria-label="Primary"]').waitFor();
    const summary=frame().locator('body').getByText(/468 invoices from 18 companies/);
    await summary.waitFor({timeout:20000});
    await frame().locator('[data-action="range"][data-value="90D"]').click();
    await page.waitForTimeout(600);
    assert.match(page.url(),/start_date=/);
    await frame().locator('[data-action="grain"][data-value="quarter"]').first().click();
    assert.match(page.url(),/grain=quarter/);
  }));
  await test('Invoice Intelligence filters, columns and invoice evidence',async()=>action('/invoices?scenario=1',async()=>{
    const all=frame().locator('[data-action="status"][data-value="all"]');
    await all.waitFor({timeout:20000});
    await frame().locator('[data-action="status"][data-value="overdue"]').click();
    assert.match(page.url(),/status=overdue/);
    await all.click();
    const search=frame().getByRole('searchbox',{name:'Search invoices'});
    await search.fill('IQ-SC-00001');
    await page.waitForTimeout(1000);
    await frame().getByRole('link',{name:'IQ-SC-00001'}).waitFor({timeout:10000});
    await search.fill('');
    await page.waitForTimeout(600);
    const columns=frame().getByRole('button',{name:'Columns'});
    await columns.waitFor({state:'visible'});
    await page.waitForFunction(() => {
      const iframe=document.querySelector('iframe.reference-frame');
      const w=iframe?.contentWindow;
      function find(root){const b=[...root.querySelectorAll('button')].find(b=>b.textContent.trim()==='Columns');
        if(b)return b;for(const n of root.querySelectorAll('*'))if(n.shadowRoot){const x=find(n.shadowRoot);if(x)return x;}return null;}
      return !!w?.document && !!find(w.document) && !find(w.document).disabled;
    },{timeout:12000});
    await columns.click();
    const dialog=frame().getByRole('dialog');
    await dialog.waitFor();
    await dialog.getByRole('button',{name:'Done'}).click();
    const firstInvoice=frame().locator('a[data-action="evidence"]').first();
    await firstInvoice.click();
    await frame().getByRole('dialog').getByText('Invoice evidence').waitFor();
    await frame().getByRole('button',{name:'Close'}).click();
  }));
  await test('Cases board/table controls and case details',async()=>action('/cases?scenario=1',async()=>{
    await frame().getByRole('group',{name:'View'}).getByRole('button',{name:'Table'}).click();
    await frame().getByRole('table',{name:'Live recovery case table'}).waitFor({timeout:15000});
    await frame().getByRole('group',{name:'View'}).getByRole('button',{name:'Board'}).click();
    const caseButton=frame().locator('[data-case]').first();
    await caseButton.click();
    await frame().getByText('Policy limits on this case').waitFor({timeout:15000});
  }));
  await test('Approvals load and decision history is populated',async()=>action('/approvals?scenario=1',async()=>{
    await frame().getByText('Simulated approval workflow',{exact:false}).waitFor({timeout:15000});
    await frame().locator('[data-approval]').first().click();
    await frame().locator('[data-review-note]').fill('Approved for a local scenario browser test.');
    page.once('dialog',dialog=>dialog.accept());
    await frame().locator('[data-decision="approve"]').click();
    await page.waitForTimeout(800);
    await frame().getByText('Decision history').waitFor();
  }));
} finally {
  await context.close();
  await browser.close();
}
if (failures.length) {
  console.error(`FAIL: ${failures.length} scenario browser checks failed: ${failures.join(', ')}`);
  process.exitCode=1;
} else console.log('PASS: Scenario browser smoke checks passed');
