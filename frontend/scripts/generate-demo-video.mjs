// Rebuild the silent, captioned two-minute portfolio video from checked-in screenshots.
// Run from frontend/: node scripts/generate-demo-video.mjs
import { chromium } from 'playwright';
import { readFile, mkdir, mkdtemp } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';

const root = resolve(import.meta.dirname, '../..');
const output = resolve(root, 'frontend/public/demo/invoiceiq-demo.webm');
const image = async name => `data:image/png;base64,${(await readFile(join(root, 'docs/images', name))).toString('base64')}`;
const [dashboard, approvals, assistant, alerts] = await Promise.all([
  image('dashboard.png'), image('approvals.png'), image('assistant.png'), image('alerts.png'),
]);
const slides = [
  { eyebrow: 'INVOICEIQ · PUBLIC DEMO', title: 'From overdue invoice to reviewed action',
    body: 'A two-minute walkthrough of the seeded sample workspace and a test-backed approval path.', image: dashboard,
    footer: 'Public model data · seeded product data · simulated communications' },
  { eyebrow: '01 · DETECT', title: 'The invoice is flagged',
    body: 'An integration fixture creates a ₹12,00,000 invoice, five days overdue. Ingestion stores the late-payment score and its top five drivers; overdue detection opens a case.', image: dashboard,
    footer: 'Synthetic integration fixture, not a real customer invoice' },
  { eyebrow: '02 · INVESTIGATE', title: 'The agent proposes contact',
    body: 'The recovery graph reads invoice and customer evidence, then records a contact recommendation with reasons and evidence references.', image: assistant,
    footer: 'The recommendation is a proposal; it cannot send a message by itself' },
  { eyebrow: '03 · POLICY', title: 'High value means human review',
    body: 'The deterministic policy applies high_value_review at ₹10,00,000. The action becomes PENDING_APPROVAL and no message is sent.', image: approvals,
    footer: 'Policy decision: REQUIRES_HUMAN_REVIEW' },
  { eyebrow: '04 · SIGN-OFF', title: 'A reviewer approves the action',
    body: 'A scoped reviewer signs off. The audit records REVIEW_APPROVED and the fixture records an EXECUTED action with a simulated communication.', image: approvals,
    footer: 'The duplicate approval is rejected; no real customer was contacted' },
  { eyebrow: '05 · EVENT SAFETY', title: 'A duplicate changes nothing',
    body: 'Kafka can redeliver an event. The first delivery returns processed; the same event_id returns duplicate. Poison events go to a dead-letter topic and can be replayed.', image: alerts,
    footer: 'Validation ROC-AUC 0.714 · retrospective test ROC-AUC 0.706 (public data)' },
];

await mkdir(resolve(root, 'frontend/public/demo'), { recursive: true });
const temp = await mkdtemp(join(tmpdir(), 'invoiceiq-video-'));
const browser = await chromium.launch({ headless: true });
const context = await browser.newContext({ viewport: { width: 1280, height: 720 },
  recordVideo: { dir: temp, size: { width: 1280, height: 720 } } });
const page = await context.newPage();
await page.setContent(`<!doctype html><html><head><meta charset="utf-8"><style>
  *{box-sizing:border-box}body{margin:0;background:#0a1328;color:#eef6ff;font-family:Arial,sans-serif}
  main{height:720px;padding:52px 58px;display:grid;grid-template-columns: 510px 1fr;gap:38px;align-items:center}
  .eyebrow{color:#6ddbe3;font-size:18px;font-weight:bold;letter-spacing:3px}.title{font-size:52px;line-height:1.07;font-weight:800;margin:22px 0}
  .body{font-size:27px;line-height:1.38;color:#d5e6f2}.footer{font-size:18px;color:#9fc4d3;margin-top:38px}
  .image{height:560px;display:flex;align-items:center;justify-content:center;background:#142844;border:1px solid #2a4965;border-radius:22px;overflow:hidden;box-shadow:0 20px 50px #0007}
  img{width:100%;height:100%;object-fit:contain}.progress{position:absolute;bottom:0;left:0;height:9px;background:#6ddbe3;width:0}
  .card{position:relative}.badge{display:inline-block;padding:9px 15px;border-radius:8px;background:#123851;color:#8ce7e7;font-size:18px;font-weight:700;margin-bottom:10px}
</style></head><body><main><div class="card"><div class="badge">2-MINUTE DEMO</div><div class="eyebrow" id="eyebrow"></div><div class="title" id="title"></div><div class="body" id="body"></div><div class="footer" id="footer"></div></div><div class="image"><img id="shot" alt="InvoiceIQ sample interface"></div></main><div class="progress" id="progress"></div></body></html>`);
for (let index = 0; index < slides.length; index++) {
  const slide = slides[index];
  await page.evaluate(({ slide, index }) => {
    for (const key of ['eyebrow', 'title', 'body', 'footer']) document.getElementById(key).textContent = slide[key];
    document.getElementById('shot').src = slide.image;
    document.getElementById('progress').style.width = `${index / 6 * 100}%`;
  }, { slide, index });
  await page.locator('#shot').evaluate(img => img.decode());
  await page.waitForTimeout(17_000);
}
await context.close();
await page.video().saveAs(output);
await browser.close();
console.log(output);
