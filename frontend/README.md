# Frontend

Next.js (App Router) dashboard for the AI Revenue Recovery Agent — the visual layer over the backend built in Phases 1-14.

## Quickest way to run it

From the repo root: `./run.sh` starts Postgres, the API, and this dashboard together. See the root [README](../README.md#getting-started).

## Run standalone

```bash
npm install
cp .env.local.example .env.local   # points at the backend; edit if it's not on localhost:8000
npm run dev
```

Requires the backend running separately (`cd ../backend && uvicorn app.main:app --reload`, or via Docker).

## Architecture

- **App Router, Server Components by default.** `app/page.tsx` (dashboard) and `app/cases/[id]/page.tsx` (case detail) fetch data server-side (`lib/api.ts`) — the browser never talks to the backend directly, so the API needs no CORS configuration.
- **Mutations are Server Actions** (`app/actions.ts`) — "Run recovery cycle", "Simulate payment", and "Run detection sweep" all call the backend from the Next.js server and revalidate the page afterward. `components/ActionButton.tsx` is the one Client Component that needs interactivity (a pending state + inline error display).
- **Types** (`lib/types.ts`) mirror `backend/app/schemas/*.py` by hand — money fields are `string` (FastAPI serializes `Decimal` as a JSON string), parsed at render time in `lib/format.ts`.
- **Styling**: Tailwind CSS v4, Geist font (self-hosted via `next/font`), a small deliberate color system (`lib/badges.ts`) mapping risk levels/case statuses/policy decisions to consistent badge colors throughout.

## Test

```bash
npx tsc --noEmit   # type-check
npm run lint       # eslint
npm run build      # production build (also used by Dockerfile, output: "standalone")
```
