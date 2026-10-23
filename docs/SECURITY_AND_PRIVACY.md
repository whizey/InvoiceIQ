# Security and privacy

The short version for users is on the [privacy page](https://invoiceiq-ai.vercel.app/privacy). This is the
engineering side.

## Boundaries

1. The browser only talks to the Next.js app. It never gets the API token and can't reach the database.
2. The Next.js proxy forwards an allow-listed set of paths, refuses cross-origin writes, and passes the session on as
   a bearer token.
3. Every portal query in the API is filtered by the caller's workspace.
4. Outside services, all optional: Gemini and Groq (assistant answers, reminder drafts, note embeddings,
   transcription), LangSmith (traces), Sentry (error reports) and Resend (email in the older workflow).

## Accounts and sessions

- Passwords: scrypt with a random salt, compared in constant time, 12 characters minimum.
- Session tokens are random, start with `iqp_`, are stored only as a SHA-256 digest, last 8 hours and can be revoked.
- The browser keeps the token in an HTTP-only, `SameSite=Strict` cookie (`Secure` over HTTPS).
- A wrong password and an unknown email get the same response.
- Five failed sign-ins lock the account for 30 seconds, doubling up to 15 minutes; a success resets it. It's keyed by
  email, not IP, because behind the proxy every visitor shares an address and forwarded headers can be faked. Locked
  attempts are refused before any hashing. The state is per API process.

## Who can do what

- The `identity` dependency resolves the session, workspace and membership for every request.
- Owner or admin to import and score, reviewer or above for decisions, notes and reminders, owner only to delete data.
- Public-workspace sessions can't belong to a user or touch a company workspace, and what a visitor writes is only
  visible to that visitor.
- A middleware blocks the older single-company API on the public deployment.

## Input

- Request sizes are capped at the proxy and in the API.
- Imports are validated row by row on the server; CSV exports neutralise spreadsheet formulas.
- Search terms can't inject wildcards.
- Notes reach the assistant as data, and any note that looks like an instruction to it is withheld. Reminder drafts
  are rejected if they contain a number that isn't in the invoice.

## What the system won't do on its own

- It never contacts customers. Reminders are drafted, and a person sends them from their own email or WhatsApp.
- No reminder on a disputed invoice or within three days of the last one; large or very late balances are flagged
  for a manager.
- In the older workflow, high-value actions wait for a person, and email can only go to one configured address.
- Nothing moves money.

## Data

- An owner can delete all of a workspace's data, or the whole account, from Settings. That includes copies in event
  payloads, alerts and assistant traces.
- `.github/workflows/backup.yml` takes a nightly `pg_dump`, encrypts it with a passphrase before it leaves the runner,
  and keeps it for seven days. It does nothing until its two secrets are set.
- Seeding scripts refuse any database that isn't on an approved list (or named in `INVOICEIQ_SHOWCASE_DATABASE`).
- Tests check the database's identity before touching it and refuse anything but the disposable instance.

## Logs and error reports

Logs are JSON with a request id and don't include request bodies. The event consumer logs only the topic and the
outcome. Sentry, if `SENTRY_DSN` is set, receives the method, path and stack trace only; bodies, query strings,
cookies, headers, user details and local variables are stripped before sending. Assistant traces store timings and
outcomes, not questions or answers.

## Secrets

`.env` files are never committed. CI scans the whole history with gitleaks and blocks known private files. Deploy
credentials are GitHub secrets. Anything that has appeared in a chat, ticket or log should be rotated.

## Not done yet

- Rate limits are per process; more than one API instance would need them in Redis.
- No email verification, password reset or two-factor sign-in.
- Before holding real customer data at scale: data residency, retention rules and a data-processing agreement with
  the model provider.
