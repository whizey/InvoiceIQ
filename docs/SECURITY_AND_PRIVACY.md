# Security and privacy

## Trust boundaries

1. **Browser to Next.js:** the only thing the browser talks to. It never receives the API token or database access.
2. **Next.js to FastAPI:** the proxy allow-lists paths, enforces same-origin on writes, and forwards the session as a bearer token.
3. **FastAPI to PostgreSQL:** every portal query is filtered by workspace.
4. **FastAPI to third parties:** Gemini (assistant, transcription, voice reasoning), Sarvam (speech), and optionally Resend (email). All are optional and auto-fall back when unconfigured.

## Authentication and sessions

- Passwords: scrypt with a random salt, constant-time comparison, minimum 12 characters.
- Session tokens: random, prefixed `iqp_`, stored only as a SHA-256 digest, valid 8 hours, revocable.
- The browser holds the token in an HTTP-only, `SameSite=Strict` cookie, `Secure` over HTTPS.
- Login errors are identical for an unknown email and a wrong password.
- **Sign-in throttling:** five failed attempts lock the account for 30 seconds, doubling on each further failure up to 15 minutes, and a success clears the streak. The limiter is keyed by normalised email, not IP (behind the web proxy every visitor shares one source address, and a forwarded address can be spoofed). Unknown emails are throttled identically so the response never reveals whether an account exists, and locked attempts are refused before any password hashing runs. State is per API process.

## Authorization and isolation

- A single `identity` dependency resolves the session, workspace and membership; customer workspaces require an active membership.
- Roles gate sensitive actions: owner or admin to import and score; owner, admin or reviewer to record decisions.
- Sample-workspace sessions cannot carry a user, cannot be used on a customer workspace, and keep decisions private through a per-session scope.
- A guard denies the older single-company API on a public portal, so portal identities never reach it.

## Input handling

- Request bodies are size-limited at the proxy and in the API (1 MB, with a larger limit for voice turns).
- CSV import is validated server-side; exported CSV neutralises spreadsheet-formula injection.
- Search terms escape SQL wildcards.
- The assistant and voice agents screen input for prompt-injection phrases, cap context size and validate model output against a schema. The deterministic policy engine, not the model, is the security boundary for actions.

## Controls on automated actions

- High-value reminders require human approval; nothing is sent until approved.
- Escalated cases stop automated reminders; voice calls are human-triggered only.
- Reminder email can only go to one configured sample address; there is no code path to any other recipient.
- The payment endpoint is a simulation and never moves money.

## Data safety

- Sample-data tooling refuses to run unless the target database name is on an approved list or explicitly allowed by `INVOICEIQ_SHOWCASE_DATABASE`.
- Tests refuse any database that is not the disposable instance and verify its identity first.
- Migrations avoid destructive changes; new columns are nullable.

## Secrets and repository hygiene

- `.env` files and credentials are never committed. CI scans the full history with gitleaks and runs a privacy check that blocks private and assistant-state paths.
- Rotate any credential that has appeared in a chat, ticket or log. Treat tokens (Hugging Face, Vercel, model API keys, database passwords) as compromised if they were ever shared.
- Deploy credentials live only as GitHub secrets, and the deploy requires an approving reviewer.

## Logging

Structured JSON logs carry a request id and avoid dumping payloads. The event consumer logs only topic and outcome because event payloads can contain predictions and invoice facts.

## Hardening roadmap

The current controls fit an evaluation deployment. Before hosting production customer data, the planned hardening is:

- A shared (Redis) store for the sign-in, assistant and transcription limits once more than one API instance runs, plus per-network throttling at an edge proxy.
- Email verification, password reset, multi-factor authentication and audit-log export.
- Shared (Redis) limits for the assistant and transcription endpoints once more than one API instance runs.
- A compliance review for real customer data (data residency, retention, a data-processing agreement with the model provider).
- Prompt-injection screening is a defence in depth layer; the deterministic policy engine remains the control that gates actions.
