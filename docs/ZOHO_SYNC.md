# Zoho Books live sync

A company workspace can stream its invoices from Zoho Books instead of uploading a CSV. Zoho pushes an
invoice-created or invoice-updated event to a webhook URL the moment it happens; the API upserts that one
invoice, scores it, and the Follow-ups and Invoices pages reflect it on the next load. Nothing in this feature
runs until a Zoho account is connected, and nothing is connected until the three settings below are set.

## What this does and does not do

It receives Zoho's webhook and keeps one invoice's number, customer, dates, total and balance in sync. It does
not currently pull Zoho's existing invoice history on first connect (a backfill), does not sync payments
recorded directly in Zoho beyond what the invoice's own balance reflects, and does not sync credit notes,
estimates or any object other than invoices. The access and refresh tokens captured at connect time are stored
for that future backfill; the webhook path itself doesn't need them, since Zoho is the one sending data to us.

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

Two more exist with working defaults and rarely need changing: `ZOHO_ACCOUNTS_DOMAIN` (`accounts.zoho.in` by
default, since this product's audience is Indian CAs; use `accounts.zoho.com`, `accounts.zoho.eu`, etc. for
other data centres) and `ZOHO_API_DOMAIN` (`www.zohoapis.in`, with the matching regional variant).
`FRONTEND_PUBLIC_URL` controls where the browser lands after authorizing — it already defaults to this
project's live site.

**This has to match the data centre the Zoho *account* actually authenticates against, not what its web apps
show in the browser.** Zoho Books can display as `books.zoho.com` while the underlying account is provisioned
on a regional data centre (India is a common case) — exchanging an authorization code against the wrong
domain fails with `invalid_code` even though everything else about the setup is correct. If connecting fails
that way, the account is very likely on a different data centre than these settings currently point at.

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
every delivery.

## What arrives, and what's skipped

Each delivery maps to one invoice: number, customer name, invoice date, due date, total and balance (amount
paid is total minus balance). A void or draft invoice is accepted but not written anywhere — the webhook
responds `200 {"ok": false, "skipped": "..."}` so Zoho doesn't retry it as a failure. The same is true of a
payload Zoho sends in a shape this endpoint doesn't recognise (missing the invoice object, an unreadable date,
a balance larger than the total): skipped, not an error, logged in the reason string.

## Events

A successful sync publishes `invoice.synced` through the same outbox-then-Kafka pipeline every other domain
event uses (see `docs/EVENT_PIPELINE.md`). Nothing currently consumes it beyond the pipeline's own bookkeeping —
it exists so a future feature (a "just synced" toast, a sync activity feed) has something to subscribe to
without adding a new publish call later.
