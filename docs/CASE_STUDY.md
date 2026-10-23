# Who to chase today: a case study

InvoiceIQ helps a CA or a small finance team answer three questions every morning: **who do I follow up first, how
much cash is coming in, and what do I say?** This note records how it answers them, how well, and what did not work.

## The problem

A small business that sells on credit typically tracks receivables in Tally or Zoho Books and chases payment from
memory. The owner or their CA opens an outstanding-bills report, scans it, and phones whoever comes to mind. Large,
old balances get chased; the customer who always pays 45 days late gets chased too early; a disputed invoice gets a
reminder that sours the relationship. Nobody knows how much cash will actually arrive this month.

## What the product does

1. Data in. Upload the export you already have: Zoho Books' invoice export or Tally's Bills Receivable report,
   CSV or Excel. The importer finds the header (Tally puts the company name and period above it), reads Indian dates
   and "Dr/Cr" amounts, maps columns automatically or by hand, and previews the result: rows ready, rows skipped and
   why ("12 rows skipped: missing due date"). Nothing is saved until you confirm.
2. A ranked follow-up list. Overdue invoices ordered by *amount × chance of paying late × days overdue*, each with
   one line of reason from the evidence: a dispute, a part payment, what the customer said or promised, or how late
   they usually pay ("₹69k, 51 days overdue, usually pays 46 days late"). Disputed invoices are held out.
3. A cash forecast. Expected collections for each of the next eight weeks, with a range, and how the same forecast
   did over recent weeks.
4. A reminder drafter. A WhatsApp or email message in English or Hinglish, drafted from the invoice's facts, that a
   person edits and sends. The system logs it and checks whether a payment followed.

## Baseline first; the model has to earn its place

The default "chance of paying late" is deliberately simple: a customer's share of late payments over their last six
settled invoices, using only what was known on the invoice's posting date, pulled toward the workspace's overall late
rate so one late invoice does not read as "100% late". Anyone can check it by hand.

A LightGBM model (14 posting-time behavioural features) is available, but each workspace uses it only if it ranks that
workspace's own last six months of settled invoices better than the simple average, by at least 0.02 AUC. Both numbers
are shown on the Model page either way.

On the chronological test split of a public B2B invoice-payment dataset (6,181 invoices posted Dec 2015 – Jan 2016,
which the model never trained on), the model clearly wins:

| | Model | Simple average |
|---|---|---|
| ROC-AUC | 0.706 | 0.520 |
| PR-AUC | 0.636 | 0.470 |

The simple average barely beats a coin flip here. A likely reason is that many customers in this dataset pay late
often, so their recent late share is similar and separates little, while the model also uses trend, streaks and the
amount relative to the customer's usual invoice. We have not tested that explanation. This is the only one of the
three comparisons below on invoices neither method has seen; treat it as the real result and the other two as what
they are, not independent evidence.

In the product, the two public workspaces disagree, and neither result should be trusted the way the test split
above can be. On Kaveri, the fictional distributor, the simple average wins (AUC 0.88 vs 0.81 when written). Kaveri
is a generated ledger in which each customer has one fixed, consistent payment habit by construction, which is
exactly what a per-customer average is built to capture; a method that learns "this customer's habit" doing well on
data built from fixed habits says nothing about how it would do on a real, messier business. On the real 2015 ledger
the model wins instead (AUC 0.87 vs 0.61), but those invoices fall inside the period the model was trained on, so
that result is optimistic in the other direction. Both numbers are shown on the Model page precisely so a visitor
can see them disagree, rather than picking the one that looks better.

## Calibration, honestly

A ranking can be good while its probabilities are wrong. On the same public test split:

| The model said | Actually late |
|---|---|
| about 5% | 32% |
| about 29% | 63% |
| about 52% | 71% |
| about 71% | 81% |
| about 89% | 69% |

The model ranks well but its probabilities are off by a lot in both directions, and its top band is *less* often late
than the band below it. A Platt-scaling calibration was tried and rejected because it hurt ranking. The product
therefore treats scores as a ranking ("likely late" ordering), shows the calibration table for whichever method a
workspace uses, and never presents a single probability as fact.

## The cash forecast

For each open invoice, the forecast asks how likely it is to be paid in each coming week, given how long past due it
already is. The curve comes from the workspace's own history: a Kaplan–Meier estimate of days from due date to payment,
kept separately for low-, medium- and high-risk invoices when each has enough history. Unpaid invoices count as
still waiting, not as never paying.

Each forecast is checked against the past. Re-running it from the start of each of the last six four-week periods,
using only what was known then, actual collections came to 92–126% of the forecast on Kaveri, but 52–181% on the
real 2015 ledger. The Kaveri figure is tight mostly because Kaveri's payments were generated to be tight; it is not
evidence the forecasting method works on a real business, any more than the AUC comparison above is. The real-ledger
figure is the one worth taking seriously, and it is wide: real payments cluster (a customer clearing several
invoices in one batch), and it is only six periods from one ledger. The page shows the range rather than hiding it.

## What failed or surprised us

- The first forecast range was far too narrow. Treating invoices as independent gave an 80% range that missed the
  actual outcome in three of six past periods: payments move together (month-end runs, a customer's cash crunch).
  The range is now widened to cover the forecast's own track record.
- The validation split flattered the model. Scored on it, the model reached 0.954 AUC, because the served model
  had been refit on data that includes those rows. Only the chronological test split is reported.
- Tally does not export payment dates for part-paid bills. The importer dates those payments on the due date and
  says so in the preview, because otherwise every part payment would look on time.
- Language models invent numbers. Asked for a reminder, they occasionally add an interest rate, a deadline or a
  rounded amount. Any draft with a figure that is not in the case is rejected; after one retry the fixed template is
  used. Nothing reaches a customer without a person editing and sending it.
- Kafka was more than a small team needs. The event pipeline still runs on Kafka in the hosted demo, but a
  PostgreSQL outbox with an in-process worker covers the same guarantees and is the default.

## Rules the system enforces

- No reminder on a disputed invoice.
- No second reminder to the same invoice within three days.
- A balance over ₹5 lakh, or more than 60 days overdue, is flagged for a manager to send.
- The system never contacts a customer or moves money: people send what they choose.

## Pilot: one month with one user

*Not yet run.* The plan is one CA or family business using InvoiceIQ for their own follow-ups for a month.

| Measure | Before (their usual process) | With InvoiceIQ |
|---|---|---|
| Time spent on follow-ups per week | | |
| Invoices followed up per week | | |
| Reminders sent, and share followed by payment within two weeks | | |
| Overdue balance at start and end of the month | | |
| Forecast for the month vs cash actually received | | |
| What they said | | |

The reminder log already records the third row automatically; the others come from a short weekly check-in.

## Reproduce

- Model vs simple average on public data: `cd backend && python -m app.ml.baseline_comparison`
  (writes [docs/evaluations/baseline-vs-model.json](evaluations/baseline-vs-model.json)).
- Follow-up ranking, forecast and reminders: `tests/test_follow_up.py`, `tests/test_cash_forecast.py`,
  `tests/test_reminders.py`; importer: `tests/test_ledger_import.py`.
