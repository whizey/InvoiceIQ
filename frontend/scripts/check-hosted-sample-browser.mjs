/** Real hosted browser acceptance. No route interception or synthetic replies. */
import assert from 'node:assert/strict';
import {isIP} from 'node:net';
import {chromium} from 'playwright';

const supplied = process.env.INVOICEIQ_PUBLIC_SAMPLE_URL;
if (!supplied) throw Error('Set INVOICEIQ_PUBLIC_SAMPLE_URL to the approved public HTTPS origin');
const url = new URL(supplied);
assert.equal(url.protocol, 'https:', 'Hosted sample requires HTTPS');
assert.equal(url.pathname, '/', 'Use only the public origin');
assert.equal(url.search, '', 'Use only the public origin');
assert.equal(url.hash, '', 'Use only the public origin');
assert.equal(url.port, '', 'Use the default HTTPS port');
assert.equal(url.username, '', 'Credentials must not be embedded in the URL');
assert.equal(url.password, '', 'Credentials must not be embedded in the URL');
assert.ok(!isIP(url.hostname) && !url.hostname.startsWith('[') &&
  !/(^localhost$|\.localhost$|\.local$|\.internal$)/i.test(url.hostname), 'Use a public host');

const browser = await chromium.launch({headless: true,
  ...(process.env.CHROME_EXECUTABLE ? {executablePath: process.env.CHROME_EXECUTABLE} : {})});
try {
  const page = await browser.newPage();
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  for (const [path, title] of [['/', 'InvoiceIQ overview'], ['/invoices', 'InvoiceIQ invoices']]) {
    const response = await page.goto(new URL(path, url), {waitUntil: 'domcontentloaded'});
    assert.equal(response?.status(), 200, `${path} did not load`);
    await page.getByText('Synthetic sample · actions simulated · AI fallback').waitFor();
    const frame = page.frameLocator(`iframe[title="${title}"]`);
    await frame.getByText(/Synced /).first().waitFor({timeout: 30000});
    const main = await frame.locator('main').innerText();
    assert.doesNotMatch(main, /Data unavailable|Backend unavailable|Authentication required/);
    assert.match(main, /invoice/i, `${path} lacks invoice content`);
  }
  assert.deepEqual(errors, [], 'Browser JavaScript errors occurred');
  console.log('PASS: hosted Chromium dashboard and invoice pages used real synthetic API data');
} finally {
  await browser.close();
}
