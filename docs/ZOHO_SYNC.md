# Zoho Books live sync

A company workspace can stream its invoices from Zoho Books instead of uploading a CSV. Zoho pushes an
invoice-created or invoice-updated event to a webhook URL the moment it happens; the API upserts that one
invoice, scores it, and the Follow-ups and Invoices pages reflect it on the next load. Nothing in this feature
runs until a Zoho account is connected, and nothing is connected until the three settings below are set.

## What this does and does not do

It receives Zoho's webhook and keeps one invoice's number, customer, dates, total and balance in sync. A
webhook only ever fires on a *future* create or update, so it can never retroactively deliver invoices that
already existed in Zoho before the webhook was connected -- that's what **Sync Now** is for (see below), a
one-time pull using the Zoho Books API directly.

Payments recorded in Zoho are also synced, as real dated records (not guessed from an invoice's balance
moving) -- see step 4b. An invoice's `amount_paid` is kept in line with the sum of its actual payment records
once any exist, falling back to inferring one payment from the balance change only for a workspace with no
payment records synced at all. It does not sync credit notes, estimates, refunds or any object other than
invoices and payments.

## Why the webhook URL carries a secret

Zoho's classic Books webhooks (the kind configured from a module's settings, not the newer signed
"notifications" API) POST the resource JSON with no signature. There is nothing in the request body or headers
that proves it came from Zoho and not from anyone who guessed the URL. So the only check available is a random
secret embedded in the URL path itself, generated when the connection completes and never reused. Treat that
URL like a password: anyone who has it can create and update invoices in the workspace it belongs to.

## Setting it up

### 1. Register an app in Zoho's API Console

Go to [api-console.zoho.com](https://api-console.zoho.com) (or the regional equivalent — `.in`, `.eu`,
`.com.au`, `.jp` — matching where the Zoho Books account was created) and create a **Server-based
Application**. Set its redirect URI to:

```
https://<your-api-host>/portal/zoho-callback
```

using the actual hostname the backend is deployed at (for this project's Hugging Face Space,
`https://riya1104-invoiceiq.hf.space/portal/zoho-callback`). This has to match exactly what's registered in
the Space's settings below — Zoho checks the redirect URI it receives against the one on file for the app.

Note the **Client ID** and **Client Secret** it gives you.

### 2. Set three values in the Space (or wherever `backend/.env` lives in production)

| Setting | Value |
|---|---|
| `ZOHO_CLIENT_ID` | from the API Console |
| `ZOHO_CLIENT_SECRET` | from the API Console |
| `ZOHO_REDIRECT_URI` | the exact URI registered in step 1 |

That's all that needs setting. The account's data centre (India, EU, ...) does **not** need configuring here —
it's detected automatically from Zoho's own callback (the `accounts-server` parameter it reports after
authorizing), not guessed from a setting, so this works for an account on any data centre out of the box.
`FRONTEND_PUBLIC_URL` controls where the browser lands after authorizing — it already defaults to this
project's live site, and `ZOHO_ACCOUNTS_DOMAIN` is only a fallback for the rare case Zoho omits that
parameter, so it's almost never worth touching.

Until `ZOHO_CLIENT_ID`, `ZOHO_CLIENT_SECRET` and `ZOHO_REDIRECT_URI` are all set, `/portal/zoho-status` reports
`configured: false` and connecting is refused with a clear error — no Zoho account is required to run the rest
of the app.

### 3. Connect the workspace

From the workspace's Settings page (owner or admin only), the integration section calls `/portal/zoho-connect`,
which returns a Zoho authorization URL, and sends the browser there. After the Zoho account owner approves
access, Zoho redirects back to `/portal/zoho-callback`, which stores the connection and a fresh webhook secret,
then redirects to the Settings page with a success or error flag.

### 4. Point a webhook at the workspace

The Settings page then shows the webhook URL for the connected workspace (reported by `/portal/zoho-status` as
`webhook_url`), something like:

```
https://<your-api-host>/portal/zoho-webhook/<secret>
```

In Zoho Books, go to **Settings → Automation → Webhooks**, create a new webhook, set its URL to the one shown,
and choose **Invoices** as the module with the triggers for invoice creation and update (Zoho's exact wording
varies by account type — look for "New Invoice" and "Invoice Status Change" or similar). Save it. From then on,
every invoice Zoho creates or updates for that organization reaches the workspace within seconds.

Disconnecting (also on the Settings page) deletes the connection and invalidates the old webhook secret
immediately; the corresponding webhook in Zoho should be removed too, since it will otherwise get a 404 on
every delivery. Re-authorizing without disconnecting first (clicking Connect again, or a future token refresh)
keeps the existing webhook secret, so it never breaks a URL already saved in Zoho.

### 4b. Optionally, a second webhook for payments

The same webhook URL also understands a **Customer Payments** delivery -- the payload's own shape says which
kind it is, so both can point at one URL without any extra configuration. In Zoho Books, create a second
webhook (e.g. "InvoiceIQ Payment Sync"), module **Customer Payments**, triggered on payment creation (and
update, if Zoho offers it), same URL as step 4. Once both are connected, a payment recorded against an
invoice -- full, partial, or split across several invoices -- shows up as its own dated record within
seconds, not just an inferred change in the invoice's balance. This one is optional: everything in step 4
works fully without it, just with less payment-level detail.

### 5. Pull in invoices (and payments) that already existed

A webhook only ever fires on a *future* create or update — it can't retroactively deliver invoices or
payments Zoho already had before the webhooks were connected. The **Sync Now** button on the Settings page
(`POST /portal/zoho-sync-now`) covers that gap: a one-time pull of every invoice (`GET /invoices`) and every
customer payment (`GET /customerpayments`) in the account directly from the Zoho Books API, paginated and
capped at 2,000 each per run — the same ceiling the CSV importer uses — refreshing the access token first if
it's expired. It's safe to run more than once; re-importing an invoice number or a payment updates the
existing row rather than duplicating it, exactly like a second webhook delivery would. Draft and void
invoices are skipped the same way they are on the webhook path. One limitation specific to the backfill path:
Zoho's invoice-list endpoint for payments reports only one invoice per payment, so a *backfilled* payment that
was actually split across several invoices in Zoho only reconciles against the one it names -- the live
webhook path (4b) doesn't have this limit, since it carries the full per-invoice breakdown.

## What arrives, and what's skipped

Each invoice delivery maps to one invoice: number, customer name, invoice date, due date, total and balance.
Each payment delivery maps to one or more invoice allocations: a payment id, a date, and the amount applied to
each invoice it was recorded against. A void or draft invoice, or a payment allocated against an invoice this
workspace hasn't synced, is accepted but not written anywhere — the webhook responds `200 {"ok": false, ...}` or
`{"ok": true, "unmatched": ...}` so Zoho doesn't retry it as a failure, and the reason is logged. The same is
true of a payload Zoho sends in a shape this endpoint doesn't recognise (missing the invoice or payment object,
an unreadable date, a balance larger than the total). Every delivery, applied or skipped, shows up in the
"Recent deliveries" list on the Settings page (the last 20), so there's no need to dig through Zoho's own
webhook logs to see why something didn't sync.

## Events

A successful sync publishes `invoice.synced` through the same outbox-then-Kafka pipeline every other domain
event uses (see `docs/EVENT_PIPELINE.md`). Nothing currently consumes it beyond the pipeline's own bookkeeping —
it exists so a future feature (a "just synced" toast, a sync activity feed) has something to subscribe to
without adding a new publish call later.
