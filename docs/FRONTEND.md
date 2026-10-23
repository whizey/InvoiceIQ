# Frontend

Next.js 16 (App Router, React 19, TypeScript) in `frontend/`. The production build uses webpack (`next build --webpack`).

## Pages

| Route | What it shows |
|---|---|
| `/home` | Public landing page: "See the cash behind every invoice", value propositions, **Explore the product** (opens the public workspace) and sign-up links |
| `/signup`, `/login` | Account creation and sign-in |
| `/workspace-dashboard` | The main analytics dashboard (the reference overview in an auto-sizing frame, fed by live data) |
| `/invoices` | Invoice intelligence: search, sort, pagination, risk and drivers, CSV import for customer workspaces |
| `/cases` | Recovery reviews: charts plus the invoice review table, with an **Approval alerts** panel |
| `/approvals` | Approval queue: charts, alerts panel and decision form |
| `/assistant` | Dedicated AI assistant page with voice input and read-aloud |
| `/settings` | Display name, company, sign out |
| `/ml` | Redirects to the dashboard (the model-intelligence page was retired) |
| `/` | Redirects to `/home` when the portal is enabled |

Pages that need a session redirect to `/home` when none exists.

## Key components

| Component | Role |
|---|---|
| `ReferenceFrame` | Embeds the preserved reference dashboard pages (`public/reference/*`) in an iframe that auto-sizes to its content and hides its own sidebar on narrow screens, so the app's navigation is used |
| `PortalNavigation` | Top-level navigation across Overview, Invoices, Cases, Approvals, Assistant and Settings |
| `PortalOperations` | The invoices, cases and approvals views: table, search, sort, paging, decision form, CSV import |
| `RecoveryCharts` | Recovery KPIs and charts (status mix, weekly decisions, exposure) for the cases and approvals views, populated for every control combination |
| `ApprovalAlerts` | Lists alerts raised by escalations and closed by decisions; refreshes every 5 seconds so events arriving through a broker appear shortly after |
| `AssistantChat` | Chat UI with suggested questions, history, speech input and read-aloud |
| `PortalIntelligence` | Model scores and drivers per invoice |
| `ExploreShowcaseButton` | Starts a sample-workspace session |
| `AccountForm`, `AccountSettings` | Sign-up, sign-in and settings forms |

`lib/speech.ts` wraps the browser's Web Speech API with friendly error messages and falls back to recording audio with MediaRecorder and transcribing it on the server. `lib/invoice-csv.mjs` parses and validates the import CSV in the browser before upload.

## The reference dashboard

The overview, invoices, cases and approvals pages preserve a reviewed visual design as static HTML, CSS and JavaScript in `public/reference/`. `dashboard-data.js` and the overview script draw the charts from live data: invoiced vs collected, aging schedule, balance bridge, recovery pipeline, where the overdue money sits, daily collections and what comes due next. The headline KPI cards show prior-period comparisons, and the date and period controls re-query the backend, so every chart and KPI is populated for every selection. Automated tests hash the stylesheets and check headings, KPI labels and table columns, so the design cannot drift unnoticed. The "late-payment" chart and the ML-intelligence page were removed on purpose.

## The server-side proxy

The browser calls only the Next.js app. Key files:

- `app/api/portal/[...path]/route.ts` is the portal proxy.
  - **Allow-list:** only named `GET` paths (`me`, `overview`, `invoices`, `settings`, `intelligence`, `recovery-summary`, `alerts`) and `POST` paths (`register`, `login`, `showcase`, `decisions`, `import`, `score`, `assistant`, `transcribe`), plus `PATCH settings` and `DELETE session`. Anything else is a 404.
  - **Origin check:** every non-GET request must have an `Origin` equal to the site's own origin, or it is rejected with 403.
  - **Session cookie:** on register, login or showcase, the API token is placed in an HTTP-only, `SameSite=Strict`, `Secure` (on HTTPS) cookie named `invoiceiq_portal_session`, valid for 8 hours. The token is never returned to JavaScript.
  - **Limits:** payload caps (import 256 KB, assistant 16 KB, transcription about 1 MB, others 4 KB) and upstream timeouts (assistant 30 s, transcription 60 s, others 15 s). Responses are marked `no-store`.
- `app/api/finance/[...path]/route.ts` serves the dashboard iframe: with a portal session it forwards `overview` to the portal dashboard and `export` to the CSV export, and blocks everything else.
- `app/api/sample/ready` is a readiness probe.
- Other proxy folders (`reviewer`, `workspace`, `session`, `review-requests`) serve the legacy single-company surface.

## Responsive behaviour and accessibility

The layout works at phone width with no horizontal page scroll; tables collapse into labelled rows (`data-label`). Status messages use `role="status"` and errors `role="alert"`. Inputs have accessible labels. Voice input degrades gracefully with explicit messages when the microphone or network is unavailable.

## Frontend testing

Node's built-in test runner covers the dashboard inventory, portal contracts, CSV import, navigation, financial consistency and scenario data. Playwright journeys exercise sign-up, import, decisions and the dashboard in Chromium against a production build. See [TESTING.md](TESTING.md).
