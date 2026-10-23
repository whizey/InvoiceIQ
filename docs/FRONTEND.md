# Frontend

Next.js 16 (App Router), React 19 and TypeScript, in `frontend/`. Production builds use webpack
(`next build --webpack`).

## Pages

| Route | What's there |
|---|---|
| `/home` | Landing page with the two ways into the public workspaces, plus sign-up and sign-in |
| `/follow-ups` | Where you land: cash forecast, the ranked chase list with reasons, due this week, disputed invoices, reminders sent |
| `/workspace-dashboard` | The receivables dashboard (the reference overview in a frame) and the recovery panel |
| `/invoices` | Import from Tally or Zoho, the invoice table with reasons and risk, notes and decisions |
| `/cases`, `/approvals` | Review and approval queues; clicking a status bar filters the list |
| `/assistant`, `/assistant/reliability` | The assistant and its reliability page |
| `/model` | Model vs simple average on this workspace and on public data, with calibration |
| `/settings` | Profile, password, and deleting the workspace's data or the account |
| `/privacy` | What's stored and where |
| `/signup`, `/login` | Accounts |

`/ml` redirects to `/model`. Pages that need a session send you to `/home` without one.

## Components worth knowing

- `FollowUps`, `CashForecast` and `ReminderPanel` make up the follow-up page. The forecast chart is plain SVG sized to
  its container, with a table view for screen readers.
- `LedgerImport` reads the file in the browser, sends it base64-encoded for a preview, lets you remap columns and then
  imports. On a public workspace the final step links to sign-up instead.
- `PortalOperations` is the invoices, cases and approvals views. `RecoveryCharts` sits above the cases and approvals
  lists.
- `ModelEvidence` is the model page. The public-data numbers come from `lib/evidence/baseline-vs-model.json`, which
  `python -m app.ml.baseline_comparison` writes.
- `ReferenceFrame` embeds the dashboard pages from `public/reference/` and passes the workspace's as-of date for the
  2015 ledger.
- `Term` shows a plain-language explanation of a finance term on hover or focus (`lib/glossary.ts`).

## The dashboard frame

The overview, invoices, cases and approvals designs exist as static HTML in `public/reference/`, and
`dashboard-data.js` fills them from the API. Tests hash their stylesheets and check headings and KPI labels so the
layout doesn't drift by accident.

## The API proxy

The browser only ever calls the Next.js app. `app/api/portal/[...path]/route.ts` forwards a fixed list of portal
paths to the API and returns 404 for anything else. It also:

- rejects any non-GET request whose `Origin` isn't the site itself,
- keeps the session token in an HTTP-only, `SameSite=Strict` cookie (`invoiceiq_portal_session`, 8 hours) and never
  hands it to JavaScript,
- caps request sizes (3.5 MB for imports, 16 KB for the assistant, 4 KB for most things) and sets timeouts (60 s for
  the assistant, imports and drafts, 15 s otherwise).

`app/api/finance/[...path]/route.ts` serves data to the dashboard frame. The other proxy folders belong to the older
single-company API.

## Phones

Every page fits a 390px-wide screen without sideways scrolling: tables turn into labelled rows, the navigation
scrolls in one line, and term explanations open at the bottom of the screen.

## Tests

The Node test runner covers the dashboard inventory, proxy contracts, CSV parsing, navigation and money formatting.
Playwright journeys run sign-up, import, decisions and the dashboard against a production build. See
[TESTING.md](TESTING.md).
