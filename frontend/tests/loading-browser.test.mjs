import test from 'node:test';
import assert from 'node:assert/strict';

const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || 'playwright');
const origin = process.env.BROWSER_TEST_ORIGIN || 'http://127.0.0.1:3047';

let browser;
test.before(async () => {
  browser = await chromium.launch({
    headless: true,
    ...(process.env.CHROME_EXECUTABLE
      ? { executablePath: process.env.CHROME_EXECUTABLE } : {}),
  });
});
test.after(async () => { await browser?.close(); });

async function setup(t) {
  const context = await browser.newContext();
  t.after(async () => { await context.close(); });
  const page = await context.newPage();
  page.setDefaultTimeout(20000);
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  t.after(() => assert.deepEqual(errors, [], 'No uncaught browser errors'));
  return { page, context };
}

async function dashboardFrame(page, pathname) {
  await page.goto(origin + pathname, { waitUntil: 'domcontentloaded' });
  const iframe = page.frameLocator('iframe');
  await iframe.locator('main').waitFor({ state: 'visible' });
  const frame = page.frames().find(item => item.url().includes('/reference/'));
  assert.ok(frame, 'Expected embedded dashboard frame');
  return frame;
}

const realFixture = (count = 7) => ({
  generated_at: '2026-10-15T00:00:00Z',
  as_of: '2016-03-17',
  workspace: 'InvoiceIQ read-only',
  currency: 'INR',
  currency_verified: false,
  historical_available: true,
  historical_reason: 'Reconciled historical B2B records',
  metrics: {
    invoiced: '125000', billed: '125000', open: '25000', overdue: '10000',
    overdue_share: 40, collected: '100000', invoice_count: count,
    company_count: 1, open_count: 2, overdue_count: 1, scored_open: 1,
    partial_count: 0, settled_count: 0, cancelled_count: 0, written_off_count: 0,
  },
  chart_notes: {}, series: [], aging: [], risk: [], top: [], daily: [], due: [],
  companies: [{ id: 'company-1', name: 'Historical company' }],
  invoices: count ? [{
    id: 'invoice-1', invoice_number: 'INV-HISTORICAL',
    company: 'Historical company', company_id: 'company-1',
    posting_date: '2016-01-05', due_date: '2016-02-10',
    amount_total: '125000', amount_paid: '100000', balance: '25000',
    status: 'OPEN', days_past_due: 36, probability: 0.45,
  }] : [],
  total: count, page: 1, page_size: 100, column_filters_version: 1,
});

test('recent portfolio renders without fetching impossible historical dates', async t => {
  const { page } = await setup(t);
  const requests = [];
  await page.route('**/api/finance/overview?**', route => {
    requests.push(route.request().url());
    return route.fulfill({ status: 503, body: '{"detail":"Unexpected historical request"}' });
  });
  const frame = await dashboardFrame(page,
    '/invoices?start_date=2026-07-18&end_date=2026-10-15&date_basis=posting&grain=month');
  await frame.locator('table.t tbody tr').first().waitFor();
  const value = await frame.evaluate(() => {
    const query = new URLSearchParams(location.search);
    return window.InvoiceIQScenario.getView(query);
  });
  assert.ok(value.metrics.invoice_count > 0, 'Recent illustrative invoices are available');
  assert.ok(value.total > 0, 'Recent selection is populated');
  assert.deepEqual(requests, [], 'No request for 2016 data under 2026-only dates');
  await frame.locator('[data-column-filter="amount"]').click();
  await frame.getByRole('dialog', { name: 'Filter Amount' }).waitFor();
  await frame.getByRole('button', { name: 'Close', exact: true }).click();
});

test('full-history queries wait for historical evidence and fail closed', async t => {
  const { page } = await setup(t);
  let pending;
  await page.route('**/api/finance/overview?**', route => {
    pending = route;
  });
  const frame = await dashboardFrame(page,
    '/invoices?start_date=2016-01-01&end_date=2016-03-17&as_of=2016-03-17');
  await frame.getByText(/Loading source data/i).first().waitFor();
  await page.waitForFunction(() => Boolean(window.__unused) || true);
  for (let i = 0; i < 100 && !pending; i++) await page.waitForTimeout(30);
  assert.ok(pending, 'A historical request must be issued');
  assert.match(pending.request().url(), /as_of=2016-03-17/);
  await pending.fulfill({
    status: 503, contentType: 'application/json',
    body: '{"detail":"Neon temporarily unavailable"}',
  });
  await frame.getByText(/Historical records could not be verified/i).first().waitFor();
  assert.doesNotMatch(await frame.locator('main').innerText(), /INV-HISTORICAL/);
  assert.doesNotMatch(await frame.locator('main').innerText(), /No data for this selection/);
});

test('historical column filters use server-side capability instead of page slices', async t => {
  const { page } = await setup(t);
  const urls = [];
  await page.route('**/api/finance/overview?**', route => {
    urls.push(route.request().url());
    const query = new URL(route.request().url()).searchParams;
    if (query.get('amount_min') === '10000') {
      return route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify(realFixture(2)),
      });
    }
    return route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify(realFixture()),
    });
  });
  const frame = await dashboardFrame(page,
    '/invoices?start_date=2016-01-01&end_date=2016-03-17&as_of=2016-03-17');
  await frame.locator('table.t tbody tr').first().waitFor();
  await frame.locator('[data-column-filter="amount"]').click();
  const dialog = frame.getByRole('dialog', { name: 'Filter Amount' });
  await dialog.waitFor();
  await dialog.getByRole('spinbutton', { name: 'Minimum ₹' }).fill('10000');
  await dialog.getByRole('button', { name: 'Apply' }).click();
  await frame.waitForFunction(() => window.location.search.includes('amount_min=10000'));
  assert.ok(urls.some(url => new URL(url).searchParams.get('amount_min') === '10000'),
    'Amount filter must reach backend query before pagination');
});

test('scenario approvals load anonymously and never write to production', async t => {
  const { page } = await setup(t);
  const calls = [];
  await page.route('**/api/**', route => {
    calls.push({ method: route.request().method(), path: new URL(route.request().url()).pathname });
    return route.fulfill({
      status: 503, contentType: 'application/json',
      body: '{"detail":"Unexpected production approval request"}',
    });
  });
  const frame = await dashboardFrame(page, '/approvals');
  const queue = frame.locator('[aria-label="Queue"]');
  await queue.getByRole('button').first().waitFor();
  assert.equal(await frame.locator('input[aria-label="Reviewer credential"]').isVisible(), false,
    'No privileged reviewer credential prompt on the public scenario');
  assert.ok(await queue.getByRole('button').count() > 0, 'Scenario queue is populated');
  assert.equal(calls.filter(call => call.method !== 'GET').length, 0, 'No production writes');
});

test('navigation preserves the selected query and never leaks old rows', async t => {
  const { page } = await setup(t);
  await page.route('**/api/finance/overview?**', route => {
    return route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify(realFixture()),
    });
  });
  const frame = await dashboardFrame(page,
    '/?start_date=2016-01-01&end_date=2016-03-17&as_of=2016-03-17');
  await frame.locator('main').getByText('Invoiced vs collected').waitFor();
  await frame.locator('a.nav[href="/invoices"]').click();
  await page.waitForURL(/\/invoices/);
  assert.match(page.url(), /start_date=2016-01-01/,
    'Selected historical cohort must survive navigation');
  const otherFrame = page.frames().find(item => item.url().includes('/reference/invoices/'));
  assert.ok(otherFrame, 'Invoice Intelligence frame loaded');
  await otherFrame.locator('table.t tbody tr').first().waitFor();
  const text = await otherFrame.locator('main').innerText();
  assert.ok(text.includes('INV-HISTORICAL'), 'Historical invoice shown after navigation');
});
