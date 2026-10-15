import test from 'node:test';
import assert from 'node:assert/strict';
const {chromium} = await import(process.env.PLAYWRIGHT_MODULE || 'playwright');
const origin = process.env.BROWSER_TEST_ORIGIN || 'http://127.0.0.1:3047';
const dashboard = (count=7) => ({
  metrics:{invoiced:'125000',open:'25000',overdue:'10000',overdue_share:40,collected:'100000',invoice_count:count,company_count:1,open_count:2,overdue_count:1,scored_open:1,partial_count:0},
  generated_at:'2026-10-14T00:00:00Z',as_of:'2026-10-14',currency:'INR',currency_verified:true,workspace:'Test workspace',historical_available:true,historical_reason:'Verified',chart_notes:{},
  series:[],aging:[],risk:[],top:[],daily:[],due:[],companies:[],
  invoices:count?[{id:'invoice-1',invoice_number:'INV-TEST',company:'Test company',company_id:'company-1',amount_total:'125000',amount_paid:'100000',balance:'25000',status:'OPEN',days_past_due:3}]:[],
  total:count,page:1,page_size:12
});
let browser;
test.before(async()=>{browser=await chromium.launch({headless:true,...(process.env.CHROME_EXECUTABLE?{executablePath:process.env.CHROME_EXECUTABLE}:{})});});
test.after(async()=>{await browser?.close();});
async function setup(t){
  const context=await browser.newContext();await context.addInitScript(observeLoading);t.after(()=>context.close());
  const page=await context.newPage();page.setDefaultTimeout(10000);page.setDefaultNavigationTimeout(15000);const errors=[];page.on('pageerror',e=>errors.push(e.message));
  t.after(()=>assert.deepEqual(errors,[]));
  const pending=[];
  await page.route('**/api/**',route=>{pending.push(route);});
  const take=async part=>{await page.waitForFunction(()=>true);for(let i=0;i<200;i++){const index=pending.findIndex(r=>r.request().url().includes(part));if(index>=0)return pending.splice(index,1)[0];await new Promise(r=>setTimeout(r,25));}throw Error('Missing request '+part);};
  const reply=(route,data,status=200)=>route.fulfill({status,contentType:'application/json',body:JSON.stringify(data)}).catch(()=>{});
  return {page,take,reply,pending};
}
async function frameReady(page){await page.frameLocator('iframe').locator('h1').waitFor().catch(async error=>{console.log('Browser diagnostic:',page.url(),await page.locator('body').innerText(),page.frames().map(f=>f.url()));throw error;});return page.frames().find(f=>f.url().includes('/reference/'));}
function observeLoading(){
    if(window.loadingObserver)return;
    window.loadingFlashes=[];
    const check=()=>{
      const main=document.querySelector('main');
      if(main?.getAttribute('aria-busy')!=='true')return;
      const text=main.innerText;
      if(/Data unavailable|No data for this selection/.test(text))window.loadingFlashes.push(text);
      const kpis=document.querySelector('[aria-label="Key metrics"]');
      if(kpis && /Unavailable/.test(kpis.innerText))window.loadingFlashes.push(kpis.innerText);
    };
    window.loadingObserver=new MutationObserver(check);window.loadingObserver.observe(document,{childList:true,subtree:true,characterData:true,attributes:true});check();
}
async function watch(frame){await frame.evaluate(observeLoading);}
for(const path of ['/?status=outstanding','/invoices?status=outstanding'])test('pending, refresh, races, empty and errors: '+path,async t=>{
  const {page,take,reply,pending}=await setup(t);
  await page.goto(origin+path,{waitUntil:'domcontentloaded'});const frame=await frameReady(page);await watch(frame);
  const initial=await take('/api/finance/overview');assert.match(initial.request().url(),/status=outstanding/);
  await frame.evaluate(()=>document.dispatchEvent(new Event('visibilitychange')));
  await page.waitForTimeout(250);
  assert.equal(pending.filter(r=>r.request().url().includes('/finance/overview')).length,0,'one initial request with URL filters');
  assert.match(await frame.locator('main').innerText(),/Loading/);
  await reply(initial,dashboard());await frame.getByText(/Synced /).first().waitFor();
  const baseline=await frame.locator('.num').allTextContents();
  await frame.locator(path.startsWith('/invoices')?'[data-action="status"][data-value="overdue"]':'[data-action="range"][data-value="MTD"]').click();const old=await take('/api/finance/overview');
  await page.waitForTimeout(200);assert.deepEqual(await frame.locator('.num').allTextContents(),baseline,'retain financial values while pending');
  await frame.locator(path.startsWith('/invoices')?'[data-action="status"][data-value="all"]':'[data-action="range"][data-value="QTD"]').click();const latest=await take('/api/finance/overview');
  await reply(latest,dashboard(9));await frame.getByText(/Synced /).first().waitFor();await reply(old,dashboard(3));
  await page.waitForTimeout(100);assert.match(await frame.locator('main').innerText(),/9 invoices/);assert.doesNotMatch(await frame.locator('main').innerText(),/3 invoices/);
  await frame.getByRole('searchbox').fill('empty');const empty=await take('/api/finance/overview');await reply(empty,dashboard(0));await frame.getByText(/Synced /).first().waitFor();
  if(path.startsWith('/invoices'))await frame.getByText('No data for this selection.',{exact:true}).waitFor();
  for(const status of [401,403,500]){
    await frame.getByRole('searchbox').fill('failure'+status);const failure=await take('/api/finance/overview');await reply(failure,{detail:'API failure '+status},status);await frame.getByText('API failure '+status,{exact:true}).first().waitFor();
    assert.doesNotMatch(await frame.locator('main').innerText(),/INV-TEST|No data for this selection/);
  }
  assert.deepEqual(await frame.evaluate(()=>window.loadingFlashes),[]);
});
test('cases filters during delayed initial load do not claim empty data',async t=>{
  const {page,take,reply}=await setup(t);await page.goto(origin+'/cases',{waitUntil:'domcontentloaded'});const frame=await frameReady(page);
  const request=await take('/api/workspace/cases');await page.waitForTimeout(200);
  await frame.getByRole('button',{name:'Table',exact:true}).click();
  await frame.getByRole('button',{name:/Opened:/}).click();
  await frame.getByRole('button',{name:'Board',exact:true}).click();
  assert.doesNotMatch(await frame.locator('main').innerText(),/Data unavailable|No matching cases|No cases in this stage/);
  await reply(request,{cases:[]});await frame.getByText(/0 real cases/).waitFor();assert.match(await frame.locator('main').innerText(),/No cases in this stage/);
});
test('approvals preserve queue on refresh and discard old reviewer responses',async t=>{
  const {page,take,reply}=await setup(t);await page.goto(origin+'/approvals',{waitUntil:'domcontentloaded'});const frame=await frameReady(page);
  const credential=frame.locator('input[aria-label="Reviewer credential"]');await credential.waitFor();
  const connect=async key=>{await credential.fill(key);await frame.getByRole('button',{name:'Connect reviewer'}).click();return take('/api/workspace/reviews');};
  const first=await connect('reviewer-a');await page.waitForTimeout(200);
  assert.match(await frame.locator('[aria-label="Queue"]').innerText(),/Loading/);assert.doesNotMatch(await frame.locator('[aria-label="Queue"]').innerText(),/No pending/);
  const reviews={reviews:[{id:'review-1',case_id:'case-1',company:'Authorized Company',invoice:'INV-A',outstanding:'5000',action_type:'REMINDER'}]};
  await reply(first,reviews);await reply(await take('/api/reviewer/history'),{history:[]});await frame.getByText(/Authorized Company/).first().waitFor();
  const refresh=await connect('reviewer-a');assert.match(await frame.locator('[aria-label="Queue"]').innerText(),/Authorized Company/);
  const changed=await connect('reviewer-b');assert.doesNotMatch(await frame.locator('main').innerText(),/Authorized Company/);
  await reply(changed,{detail:'Reviewer forbidden'},403);await frame.getByText(/Approvals unavailable: Reviewer forbidden/).waitFor();await reply(refresh,reviews);await page.waitForTimeout(100);assert.doesNotMatch(await frame.locator('main').innerText(),/Authorized Company/);
});
test('KPI navigation and session changes never carry records into a new document',async t=>{
  const {page,take,reply}=await setup(t);await page.goto(origin+'/',{waitUntil:'domcontentloaded'});
  let frame=await frameReady(page);await reply(await take('/api/finance/overview'),dashboard());await frame.getByText(/Synced /).first().waitFor();
  await frame.getByRole('link',{name:'View overdue →',exact:true}).click();await page.waitForURL(/invoices\?/);
  frame=await frameReady(page);await watch(frame);const invoices=await take('/api/finance/overview');
  assert.match(invoices.request().url(),/status=overdue/);assert.doesNotMatch(await frame.locator('main').innerText(),/INV-TEST|Data unavailable/);
  await reply(invoices,dashboard());await frame.getByText('INV-TEST',{exact:true}).waitFor();
  // Same-origin storage events are how other tabs notify this document of login/logout.
  const other=await page.context().newPage();await other.goto(origin+'/connect');
  await other.evaluate(()=>localStorage.setItem('invoiceiq-session-change',crypto.randomUUID()));
  const newSession=await take('/api/finance/overview');
  frame=await frameReady(page);assert.doesNotMatch(await frame.locator('main').innerText(),/INV-TEST|Data unavailable/);
  await reply(newSession,{detail:'New session requires authorization'},401);await frame.getByText('New session requires authorization',{exact:true}).first().waitFor();
  assert.doesNotMatch(await frame.locator('main').innerText(),/INV-TEST/);
});
