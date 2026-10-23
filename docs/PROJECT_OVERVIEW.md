# Project overview

## The problem

A business that sells on credit has to chase payment, and in a small company that's usually the owner, an
accountant, or their CA working from a Tally or Zoho Books report. They know roughly who's late. What they don't have
is a quick answer to three questions: who should I call today, how much money will actually come in this month, and
what do I say to a customer who's 50 days late without souring the relationship?

So the biggest balances get chased, the customer who always pays 45 days late gets chased too early, and disputed
invoices get reminders they shouldn't.

## What InvoiceIQ does about it

It takes the export they already have and turns it into a short daily list: the overdue invoices worth chasing, in
order, each with the reason it's late. Alongside that it gives a weekly cash forecast with an honest range, and a
reminder drafted from the invoice that they edit and send themselves.

The scoring starts with something a CA can check by hand (how often this customer has paid late recently) and only
switches to the machine-learning model where the model does better on that business's own history. Both are shown.

It doesn't send anything to customers, move money, or try to be an accounting system. It sits next to Tally or Zoho.

## Who it's for

CAs looking after several small clients, and small finance teams (often one or two people) at businesses that invoice
other businesses.

## Data

- Kaveri Industrial Supplies, the first public workspace, is generated: about 700 invoices for 40 made-up customers
  over the last year.
- The second public workspace holds 1,998 real, anonymised invoices from a public B2B invoice-payment dataset, as of
  30 Nov 2015.
- The late-payment model was trained on that same public dataset (45,839 invoices, 69 customers). CI downloads it from
  a pinned commit and checks its SHA-256 before the tests run.
- The survival-model research uses the public BPI Challenge 2019 purchase-to-pay log (about 730 MB). It's only used
  offline and isn't checked in CI.
- Company workspaces hold whatever their owner imports.

See [CASE_STUDY.md](CASE_STUDY.md) for how well it works and [ML_MODELS.md](ML_MODELS.md) for the models.
