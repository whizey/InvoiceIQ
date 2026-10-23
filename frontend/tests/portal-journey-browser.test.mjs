import test from 'node:test';
import assert from 'node:assert/strict';
import { chromium } from 'playwright';

const origin = process.env.BROWSER_TEST_ORIGIN || 'http://127.0.0.1:3048';

test('visitor and customer journeys use isolated persisted workspaces', async () => {
  const browser = await chromium.launch({headless: true, executablePath: process.env.CHROME_EXECUTABLE});
  try {
    const visitor = await browser.newContext();
    const page = await visitor.newPage();
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    if(process.env.PORTAL_BROWSER_DEBUG){
      page.on('response', response => {if(response.url().includes('/api/')) void response.text().then(body=>console.log('API',response.status(),response.url(),body.slice(0,200)))});
      page.on('console', message => {if(message.type()==='error') console.log('CONSOLE',message.text())});
    }
    await page.goto(origin + '/home');
    await page.getByRole('button', {name: /Explore the product/}).click();
    await page.waitForURL('**/follow-ups');
    await page.getByRole('heading', {name:'Who to chase today'}).waitFor();
    await page.getByRole('heading', {name:'Chase first'}).waitFor();
    const showcaseFinancial=page.waitForResponse(response => response.url().includes('/api/finance/overview?'));
    await page.getByRole('button', {name:'More'}).click();
    await page.getByRole('link', {name:'Analytics', exact:true}).click();
    await page.waitForURL('**/workspace-dashboard');
    const showcaseResponse=await showcaseFinancial;
    assert.equal(showcaseResponse.status(),200,await showcaseResponse.text());
    const showcaseData=await showcaseResponse.json();
    assert.equal(showcaseData.workspace,'Kaveri Industrial Supplies');
    assert.equal(showcaseData.metrics.scored_open,showcaseData.metrics.open_count,
      'Every open invoice has a genuine stored champion prediction');
    assert.ok(showcaseData.metrics.invoice_count>0);
    assert.ok(Number(showcaseData.metrics.overdue_share)>20 && Number(showcaseData.metrics.overdue_share)<65,
      `Overdue share should reflect a believable aged ledger: ${showcaseData.metrics.overdue_share}`);
    assert.equal(showcaseData.series.at(-1).open,showcaseData.metrics.open,
      'The plotted period-end balance must reconcile to the open receivables KPI');
    await page.frameLocator('iframe').getByText('Total billed', {exact: true}).first().waitFor();
    if(process.env.PORTAL_SCREENSHOT_PATH){
      await page.screenshot({path:process.env.PORTAL_SCREENSHOT_PATH});
      const frame=page.frame({url:/\/reference\/overview\/index\.html/});
      await frame.evaluate(() => window.scrollTo(0,650));
      await page.screenshot({path:process.env.PORTAL_SCREENSHOT_PATH.replace(/\.png$/, '-lower.png')});
    }
    await page.frameLocator('iframe').getByRole('link', {name:/View overdue/}).click();
    await page.waitForFunction(() => location.pathname==='/invoices' && new URLSearchParams(location.search).get('view')==='analytics');
    assert.equal(new URL(page.url()).searchParams.get('status'),'overdue');
    await page.getByRole('button', {name:'Go back to previous page'}).click();
    await page.waitForURL('**/workspace-dashboard');
    assert.equal(await page.getByRole('link', {name:'ML intelligence'}).count(),0,'ML intelligence is no longer in the navigation');
    await page.getByRole('button', {name:'More'}).click();
    await page.getByRole('link', {name:'AI assistant'}).click();
    await page.waitForURL('**/assistant');
    await page.getByRole('heading', {name:'AI assistant'}).waitFor();
    await page.getByRole('button', {name:'Which overdue invoices should we chase first?'}).waitFor();
    await page.goto(new URL('/ml',page.url()).href);
    await page.waitForURL('**/model');
    await page.getByRole('heading', {name:'Model or simple average?'}).waitFor();
    await page.getByRole('button', {name:'More'}).click();
    await page.getByRole('link', {name:'Cases', exact:true}).click();
    await page.waitForURL('**/cases');
    await page.getByRole('heading', {name:'Case overview'}).waitFor();
    await page.getByRole('heading', {name:'Still owed by status'}).waitFor();
    await page.getByRole('button', {name:'More'}).click();
    await page.getByRole('link', {name:'Approvals', exact:true}).click();
    await page.waitForURL('**/approvals');
    await page.getByRole('heading', {name:'Decision overview'}).waitFor();
    await page.getByRole('heading', {name:'Decisions per week'}).waitFor();
    await page.getByRole('button', {name:'More'}).click();
    await page.getByRole('link', {name:'Analytics', exact:true}).click();
    await page.waitForURL('**/workspace-dashboard');
    await page.getByRole('link', {name: 'Invoices', exact: true}).first().click();
    await page.waitForURL('**/invoices');
    await page.getByText(/Showing 25 of \d+ invoices/).waitFor();
    await page.route('**/api/portal/invoices?**', route => route.fulfill({status:503,contentType:'application/json',body:'{"detail":"Temporary test outage"}'}));
    await page.getByRole('textbox', {name: 'Search invoices'}).fill('temporary outage');
    await page.getByRole('alert').getByText('Temporary test outage').waitFor();
    await page.unroute('**/api/portal/invoices?**');
    await page.getByRole('button', {name: 'Retry invoices'}).click();
    await page.getByText('No invoices match this search.').waitFor();
    await page.getByRole('textbox', {name: 'Search invoices'}).fill('Sundaram');
    await page.waitForFunction(() => new URLSearchParams(location.search).get('q') === 'Sundaram');
    await page.getByText(/invoices matching the search/).waitFor();
    await page.getByRole('button', {name: 'Review →'}).first().click();
    await page.getByRole('textbox', {name: 'Review note'}).fill('Browser review of an overdue invoice');
    await page.getByRole('button', {name: 'Record decision'}).click();
    await page.getByText('Decision recorded in the audit history.').waitFor();
    await page.reload();
    assert.equal(await page.getByRole('textbox', {name: 'Search invoices'}).inputValue(), 'Sundaram');
    await page.getByText('Browser review of an overdue invoice').waitFor();
    await page.getByRole('button', {name: 'Go back to previous page'}).click();
    await page.waitForURL('**/workspace-dashboard');
    assert.deepEqual(errors, []);
    await visitor.close();

    const customer = await browser.newContext();
    const account = await customer.newPage();
    const email = `browser-${Date.now()}-${Math.random().toString(36).slice(2)}@example.invalid`;
    const password = 'Unique-browser-test-password-9324';
    await account.goto(origin + '/signup');
    await account.getByLabel('Full name').fill('Browser Test Owner');
    await account.getByLabel('Company name').fill('Browser Test Company');
    await account.getByLabel('Email address').fill(email);
    await account.getByLabel('Password').fill(password);
    const emptyFollowUps=account.waitForResponse(response => response.url().includes('/api/portal/follow-ups') && response.status()===200);
    await account.getByRole('button', {name: 'Create account'}).click();
    await account.waitForURL('**/follow-ups');
    assert.equal((await (await emptyFollowUps).json()).totals.to_chase,0);
    await account.getByText('Nothing overdue. Nobody to chase today.').waitFor();
    await account.getByRole('link', {name: 'Invoices', exact: true}).first().click();
    await account.getByText('No invoices yet. Import a Tally or Zoho Books export to begin.').waitFor();
    const csv = 'Invoice Date,Invoice Number,Invoice Status,Customer Name,Due Date,Total,Balance,Last Payment Date\n'
      + '2026-01-01,BROWSER-001,PartiallyPaid,Verified Account,2026-02-01,2000.00,1500.00,2026-01-20\n';
    await account.getByLabel('Choose an export file').setInputFiles({name:'browser.csv',mimeType:'text/csv',buffer:Buffer.from(csv)});
    await account.getByText('1 ready to import').waitFor();
    await account.getByRole('button', {name: 'Import 1 invoices'}).click();
    await account.getByText('Imported 1 invoices and scored 1.').waitFor();
    await account.reload();
    await account.getByText('BROWSER-001').waitFor();
    const importedFinancial=account.waitForResponse(response => response.url().includes('/api/finance/overview?') && response.status()===200);
    await account.getByRole('button', {name:'More'}).click();
    await account.getByRole('link', {name: 'Analytics', exact: true}).first().click();
    const totals=(await (await importedFinancial).json()).metrics;
    assert.equal(totals.invoiced,'2000.00');
    assert.equal(totals.open,'1500.00');
    assert.equal(totals.collected,'500.00');
    await account.getByRole('link', {name: 'Settings', exact: true}).click();
    await account.getByRole('heading', {name: 'Settings'}).waitFor();
    await account.getByLabel('Full name').fill('Updated Browser Owner');
    await account.getByRole('button', {name: 'Save profile'}).click();
    await account.getByText('Profile saved.').waitFor();
    await account.getByLabel('Current password').fill(password);
    await account.getByLabel('New password').fill('Changed-browser-test-password-9325');
    await account.getByRole('button', {name: 'Change password'}).click();
    await account.getByText('Password changed. Other sessions were revoked.').waitFor();
    await account.getByRole('button', {name: 'Sign out'}).click();
    await account.waitForURL('**/home');
    await account.goto(origin + '/invoices');
    await account.waitForURL('**/home');
    await account.goto(origin + '/login');
    await account.getByLabel('Email address').fill(email);
    await account.getByLabel('Password').fill('Changed-browser-test-password-9325');
    await account.getByRole('button', {name: 'Sign in'}).click();
    await account.waitForURL('**/follow-ups');
    await customer.close();
  } finally {
    await browser.close();
  }
});
