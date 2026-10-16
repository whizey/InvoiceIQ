# Deploying the InvoiceIQ SaaS product

`render.yaml` deploys the **legacy read-only synthetic sample** only. It has no
accounts, signup, assistant or recovery pages. This document describes what the
current product needs. Nothing here has been deployed.

## Free path (Hugging Face Space + Supabase + Vercel; no card)

1. **Database:** create a free Supabase project dedicated to this app and copy its *Session pooler*
   connection string (port 5432). The seeding script only accepts approved database names; Supabase's
   is `postgres`, so set the Space variable `INVOICEIQ_SHOWCASE_DATABASE=postgres` on purpose.
2. **API:** create a new Hugging Face Space. Use SDK **Gradio** (template Blank): the Docker SDK is paid
   for new accounts, and the Gradio build simply runs our FastAPI server from `app.py`. Hardware: CPU basic if
   selectable, otherwise the free option offered. In its settings add the
   secrets `DATABASE_URL` and `GOOGLE_API_KEY` and the variable `INVOICEIQ_SHOWCASE_DATABASE`.
3. **Push the code:** from the repo run `git lfs pull && scripts/build_hf_space.sh` (add `docker` as a second argument for the Docker SDK), then in the printed
   folder: `git remote add space https://huggingface.co/spaces/<you>/invoiceiq && git push space main`
   (use a Hugging Face access token with write access as the password).
4. The Space builds, migrates the database, seeds the showcase and serves the API at
   `https://<you>-invoiceiq.hf.space`. Check `/health`.
5. **Frontend:** in the Vercel project `invoiceiq`, set `INVOICEIQ_API_URL` to that URL and
   `INVOICEIQ_PORTAL_ENABLED=true`, then redeploy to production.

Free Spaces sleep after about two days without traffic and wake on the next request (the first
request is slow). Data lives in Supabase, so it survives. Supabase pauses idle free projects.

## Quick path (Render + Vercel)

1. Render: New -> Blueprint -> this repo -> Blueprint file `render.saas.yaml`. It creates the
   `invoiceiq` PostgreSQL database and the `invoiceiq` API together. Paste the Gemini key into
   `GOOGLE_API_KEY` when prompted. The pre-deploy command migrates the database and seeds the
   showcase (safe to repeat). Both resources are paid; Render asks you to confirm.
2. Vercel project `invoiceiq`: set `INVOICEIQ_API_URL` to the Render service URL and
   `INVOICEIQ_PORTAL_ENABLED=true`, then redeploy to production.
3. Run the smoke tests below.

Neon also works: create a database named `invoiceiq`, remove the `databases:` block from
`render.saas.yaml` and give `DATABASE_URL` the Neon string instead.

## Recommended topology

| Piece | Host | Notes |
|---|---|---|
| Frontend (Next.js) | Vercel project `invoiceiq` | Already builds. |
| API (FastAPI, Docker) | Render web service, or any Docker host | `backend/Dockerfile`, port 8000. |
| Database | Render PostgreSQL (or Neon) | Use a **dedicated** database for this deployment. |

## Decision required: one database or two

`backend/scripts/provision_portal_showcase.py` deliberately refuses to run
unless the database is named `invoiceiq_sample`, `invoiceiq_sample_test` or
`invoiceiq_portal_test`, or exactly matches `INVOICEIQ_SHOWCASE_DATABASE` (set to
`invoiceiq` in `render.saas.yaml`). That guard stops illustrative data reaching a real
customer database. Choose one:

1. **Portfolio/sample deployment (recommended for a submission).** One Neon
   database named `invoiceiq`. Showcase and sample sign-ups live together;
   say clearly that accounts are for demonstration only. No real customer data.
2. **Production with real customers.** A separate database with no showcase
   (`INVOICEIQ_PORTAL_SHOWCASE=false`), plus a second sample deployment as in (1).
   The visitor "Explore the product" flow needs the showcase, so it runs only on
   the sample deployment.

Do not weaken the name guard to make one database serve both without a
deliberate review.

## Environment

API service:

| Variable | Value |
|---|---|
| `DATABASE_URL` | Neon connection string (secret). Use the `postgresql+asyncpg://` form. |
| `INVOICEIQ_PORTAL_ENABLED` | `true` |
| `INVOICEIQ_PORTAL_SHOWCASE` | `true` on the sample deployment, `false` otherwise |
| `PUBLIC_READ_ONLY` | `false` (portal writes are authorised by portal sessions) |
| `RUN_MIGRATIONS` | `false`; migrate as a separate release step |
| `GOOGLE_API_KEY` | Gemini key (secret). Never commit it. |
| `LLM_MODEL` | `gemini-3.6-flash` (voice transcription prefers `gemini-3.5-flash-lite` automatically) |

Frontend (Vercel):

| Variable | Value |
|---|---|
| `INVOICEIQ_PORTAL_ENABLED` | `true`, set **before the build**: `/` is prerendered and its redirect to `/home` is fixed at build time |
| `INVOICEIQ_API_URL` | Public HTTPS URL of the API |

## Release steps

1. Wait for a fully green CI run on the exact release commit.
2. Back up the target database (Neon branch or snapshot).
3. Run migrations once, from a trusted machine: `cd backend && alembic upgrade head`
   against the target `DATABASE_URL`. Check the revision first.
4. Sample deployment only: provision the showcase, once, deliberately:
   `INVOICEIQ_PORTAL_SHOWCASE=true python -m scripts.provision_portal_showcase`
   (it also seeds the illustrative baseline decisions; reruns are no-ops).
5. Deploy the API, then the frontend with the variables above.
6. Rotate any key that was ever pasted into a chat or ticket.

## Smoke tests after deploying

- `GET <api>/health` and `/health/ready` return OK.
- Home page loads; **Explore the product** opens the dashboard with data.
- Register a test account, import a small CSV, see it on the dashboard.
- Record a decision on Cases/Approvals; charts update.
- Settings: change the display name; sign out and sign in.
- AI assistant answers a question. Voice needs HTTPS and a microphone.
- Two accounts cannot see each other's invoices.

## Rollback

- Frontend: promote the previous Vercel deployment.
- API: redeploy the previous image or commit.
- Database: restore the Neon snapshot or branch taken in step 2. The portal
  migrations only add tables, so rolling the code back leaves them unused.

## Known limits

- Rate limits for sign-in and the assistant are per API process; a multi-replica
  deployment needs a shared limiter.
- The assistant and voice features depend on a third-party model and free-tier
  quotas can return temporary errors.
- Write-offs are not tracked, so the balance bridge shows them as zero.
