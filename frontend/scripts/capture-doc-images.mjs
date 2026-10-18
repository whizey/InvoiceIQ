import {chromium} from 'playwright';
import {mkdir} from 'node:fs/promises';
import {resolve} from 'node:path';

const origin = 'https://invoiceiq-ai.vercel.app';
const output = resolve(import.meta.dirname, '../../docs/images');
await mkdir(output, {recursive: true});
const browser = await chromium.launch({headless: true});
try {
  const context = await browser.newContext({viewport: {width: 1440, height: 900}, deviceScaleFactor: 1});
  const page = await context.newPage();
  await page.goto(`${origin}/home`, {waitUntil: 'domcontentloaded'});
  await page.waitForLoadState('networkidle', {timeout: 30000});
  await page.getByRole('button', {name: /Explore the product/}).click();
  await page.waitForURL('**/workspace-dashboard', {timeout: 35000}).catch(async () => {
    throw Error(`Showcase entry failed at ${page.url()}: ${await page.getByRole('alert').allTextContents()}`);
  });
  const shots = [
    ['/workspace-dashboard', 'dashboard.png', 'Analytics'],
    ['/approvals', 'approvals.png', 'Approval queue'],
    ['/assistant', 'assistant.png', 'AI assistant'],
    ['/cases', 'alerts.png', 'Recovery reviews'],
  ];
  for (const [path, filename, marker] of shots) {
    const response = await page.goto(`${origin}${path}`, {waitUntil: 'domcontentloaded', timeout: 90000});
    if (response?.status() !== 200) throw Error(`${path}: HTTP ${response?.status()}`);
    if (path === '/workspace-dashboard') {
      await page.frameLocator('iframe').getByText(/Synced /).first().waitFor({timeout: 90000});
    } else {
      await page.getByRole('heading', {name: marker}).first().waitFor({timeout: 90000});
    }
    if (path === '/approvals') {
      await page.getByRole('heading', {name: 'Review pending invoices'}).waitFor({timeout: 90000});
      await page.getByText('Loading records…').waitFor({state: 'hidden', timeout: 90000});
      await page.getByText('Loading recovery summary…').waitFor({state: 'hidden', timeout: 90000});
      await page.getByRole('button', {name: 'Review →'}).first().click();
      await page.getByRole('heading', {name: 'Record a decision'}).waitFor();
      await page.setViewportSize({width: 1440, height: 1600});
      await page.evaluate(() => window.scrollTo(0, 0));
    }
    if (path === '/cases') {
      const panel = page.getByRole('region', {name: 'Approval alerts'});
      await panel.waitFor({timeout: 90000});
      await panel.getByText(/1 open/).waitFor({timeout: 30000});
      await panel.screenshot({path: `${output}/${filename}`, animations: 'disabled'});
    } else {
      await page.screenshot({path: `${output}/${filename}`, fullPage: path !== '/approvals', animations: 'disabled'});
    }
    console.log(`${filename}: ${page.url()}`);
    if (path === '/approvals') {
      await page.setViewportSize({width: 1440, height: 900});
      await page.getByLabel('Review note').fill('Escalated for review in the sample workspace screenshot.');
      await page.getByRole('button', {name: 'Record decision'}).click();
      await page.getByText('Decision recorded in the audit history.').waitFor({timeout: 30000});
    }
  }
  await context.close();
} finally {
  await browser.close();
}
