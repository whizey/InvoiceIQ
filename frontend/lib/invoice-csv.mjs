/**
 * Strict RFC 4180-style CSV parser for bounded financial imports.
 * No mutation occurs until the complete file passes parsing and field checks.
 */
export function parseInvoiceCsv(input) {
  if (typeof input !== "string" || input.length > 200000)
    throw new Error("Invoice CSV exceeds the 200,000-character import limit");
  const rows = [];
  let record = [], value = "", quoted = false, endedQuote = false, started = false;
  function field() {
    record.push(value.trim());
    value = "";
    endedQuote = false;
    started = false;
  }
  function line() {
    field();
    if (!(record.length === 1 && record[0] === "")) rows.push(record);
    if (rows.length > 251) throw new Error("Maximum 250 invoice rows");
    record = [];
  }
  for (let i = 0; i < input.length; i++) {
    const c = input[i];
    if (quoted) {
      if (c === '"') {
        if (input[i + 1] === '"') { value += '"'; i++; }
        else { quoted = false; endedQuote = true; }
      } else value += c;
      continue;
    }
    if (c === ",") { field(); continue; }
    if (c === "\n" || c === "\r") {
      if (c === "\r" && input[i + 1] === "\n") i++;
      line();
      continue;
    }
    if (c === '"' && !started && value === "") {
      quoted = true; started = true;
    } else if (c === '"' || (endedQuote && c !== " " && c !== "\t")) {
      throw new Error("Malformed CSV quoting");
    } else if (!endedQuote) {
      value += c; started = true;
    }
  }
  if (quoted) throw new Error("Unclosed quoted field");
  if (started || value !== "" || record.length) line();
  if (rows.length < 2 || rows.length > 251)
    throw new Error("Select a CSV containing 1–250 invoices");
  const headers = rows.shift().map((header) => header.replace(/^\uFEFF/, "").trim().toLowerCase());
  const required = ["invoice_number", "counterparty", "issue_date",
                    "posting_date", "due_date", "amount_total", "amount_paid", "payment_date"];
  if (new Set(headers).size !== headers.length)
    throw new Error("Duplicate CSV column headings");
  if (required.some((column) => !headers.includes(column)))
    throw new Error("CSV must include: " + required.join(", "));
  return rows.map((values, index) => {
    if (values.length !== headers.length)
      throw new Error("Wrong column count on invoice row " + (index + 2));
    const row = Object.fromEntries(headers.map((key, i) => [key, values[i]]));
    if (!row.invoice_number || !row.counterparty)
      throw new Error("Missing invoice identifier or counterparty on row " + (index + 2));
    if (!row.payment_date) row.payment_date = null;
    return row;
  });
}
