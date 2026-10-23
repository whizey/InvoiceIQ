# Project overview: the problem, the aim and the solution

## The problem

In business-to-business trade, the seller issues an invoice and waits to be paid. Customers often pay late, and a late invoice is a loan the seller never agreed to make. For small and mid-sized businesses this directly drives cash-flow gaps, borrowing costs and missed growth.

The day-to-day reality in many finance teams:

- Invoice and payment data lives in accounting exports and spreadsheets. Nobody sees a reconciled picture of invoiced, collected, open and overdue money.
- There is no principled way to decide **which overdue invoice to chase first**. Teams chase the loudest or the most recent, not the most at risk.
- Follow-up is inconsistent. Reminders are sent late or too often, escalations depend on memory, and a broken promise to pay is easy to miss.
- Automation is risky. A tool that emails customers on its own can annoy a key account or contact the wrong person.
- When someone asks "why did we escalate this customer?", there is rarely a record.

## What InvoiceIQ aims to solve

| Problem | How InvoiceIQ addresses it |
|---|---|
| No single, trustworthy view of receivables | A dashboard that reconciles invoiced, collected, open and overdue amounts, with aging, a balance bridge, a recovery pipeline and daily collections. |
| Unclear priorities | A late-payment model scores each invoice, with the factors that drove the score, so the team can rank work by risk and by amount. |
| Inconsistent follow-up | A LangGraph workflow produces a recommended next step for every overdue case on a fixed, repeatable path. |
| Fear of unsafe automation | A deterministic policy engine, not the AI model, decides whether an action runs. High-value actions require human approval. Escalated cases stop automated reminders. |
| No audit trail | Decisions, approvals, policy outcomes, agent steps and model scores are stored with actor and time. |
| Slow questions about the data | A grounded AI assistant answers in plain language from the workspace's own figures, with voice input and read-aloud. |
| Multi-company risk | Every query is scoped to a workspace; sessions are revocable; the sample workspace is isolated per visitor. |

## Who it is for

- **Finance and collections teams** at B2B companies who need to decide what to chase and to prove what they did.
- **Finance leaders** who want a reconciled receivables view and a record of how overdue cases were handled.
- **Reviewers and approvers** who must sign off on high-value or escalated actions.
- **Engineers and reviewers evaluating the codebase**, for whom it shows a production-style full-stack system with machine learning, agents, an event pipeline and a delivery pipeline.

## Guiding principles

1. **The model advises; rules decide.** Machine-learning output and language-model output are inputs. A deterministic policy layer is the only thing that can permit an action.
2. **Humans approve what matters.** High-value or ambiguous actions are held for review, and the workflow stops until a person decides.
3. **Evidence over claims.** Model numbers are reported with their limitations. The test split was never used to tune anything; the frozen model was scored on it once, and the docs say what has not been established.
4. **Isolation by default.** Tenants cannot see each other's data, and sample-data tooling refuses to run against anything that is not an approved sample database.
5. **Everything is explainable.** If the system did something, the reason, the rule and the actor are on record.

## Scope and non-goals

In scope: invoice and payment ingestion by CSV, receivables analytics, late-payment risk scoring, a policy-gated recovery workflow, human approvals, an assistant, auditability, and a CI/CD path.

Out of scope by design: moving money, bank integration, and acting as an accounting system of record. InvoiceIQ complements reconciliation rather than replacing it. The payment endpoint is a simulation, and model outputs are probabilities used to prioritise work.

## Where the data comes from

- **Sample workspace:** Kaveri Industrial Supplies, a fictional distributor: a deterministic seed of about 700 invoices across 40 customers over the past year, with payments, review decisions and notes, rebuilt when it goes stale and loaded only into databases explicitly approved for sample data.
- **Customer workspaces:** invoices imported by CSV by the workspace owner or admin.
- **Model training:** two public datasets. Details in [ML_MODELS.md](ML_MODELS.md).
  - **B2B invoice-payment dataset** (45,839 invoices, 69 customers): trains the late-payment model. CI downloads it from a pinned commit and verifies its SHA-256 checksum before the tests run.
  - **BPI Challenge 2019 event log** (a public purchase-to-pay process log): trains the survival models, which estimate when a purchase-to-pay item clears. It is about 730 MB, so it is used offline only and is not downloaded or checksummed in CI.
