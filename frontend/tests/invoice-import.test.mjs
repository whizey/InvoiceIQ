import test from "node:test";
import assert from "node:assert/strict";
import {parseInvoiceCsv} from "../lib/invoice-csv.mjs";

const cols = "invoice_number,counterparty,issue_date,posting_date,due_date,amount_total,amount_paid,payment_date";
test("quoted vendor names, multiline fields and payment provenance", () => {
  const data = "\uFEFF" + cols + "\r\n" +
    'INV-001,"Example, Inc.",2026-01-01,2026-01-01,2026-02-01,2500.00,1000.00,2026-02-01\r\n' +
    'INV-002,"Line 1\nLine 2",2026-01-02,2026-01-02,2026-03-02,1000.00,0.00,\r\n';
  const result = parseInvoiceCsv(data);
  assert.equal(result.length, 2);
  assert.equal(result[0].counterparty, "Example, Inc.");
  assert.equal(result[1].counterparty, "Line 1\nLine 2");
  assert.equal(result[1].payment_date, null);
  assert.equal(result[0].payment_date, "2026-02-01");
});
test("escaped quotes and CR-only line endings parse correctly", () => {
  const row = 'I1,"He said ""paid""",2026-01-01,2026-01-01,2026-02-01,12,0,';
  const result = parseInvoiceCsv(cols+"\r"+row);
  assert.equal(result[0].counterparty, 'He said "paid"');
});
test("invalid and oversized files fail before an API mutation", () => {
  const row = "A,Example,2026-01-01,2026-01-01,2026-02-01,12,0,";
  assert.throws(() => parseInvoiceCsv(cols+"\n"+row.replace("Example",'"Bad"junk')), /Malformed/);
  assert.throws(() => parseInvoiceCsv(cols+"\n"+row+"\n"+row.replace("Example,","")), /column count/);
  assert.throws(() => parseInvoiceCsv(cols+"\n"+row.repeat(300)), /column count/);
  assert.throws(() => parseInvoiceCsv(cols+"\n"+Array(251).fill(row).join("\n")), /Maximum 250/);
  assert.throws(() => parseInvoiceCsv(cols+"\n"+row+"x".repeat(200001)), /limit/);
  assert.throws(() => parseInvoiceCsv(cols+"\n"+'A,"broken'), /Unclosed/);
  assert.throws(() => parseInvoiceCsv(cols+",invoice_number\n"+row+",B"), /Duplicate/);
});
