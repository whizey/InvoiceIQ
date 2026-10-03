# End-to-End Demo Walkthrough

This walks through the project brief's own "definition of DONE" checklist (section 14) — 16 concrete things V1 should be able to demonstrate. Every step below has actually been run and verified; this isn't a speculative script.

## Setup (one time)

```bash
./run.sh
```

Wait for `Starting dashboard at http://localhost:3000`, then in another terminal:

```bash
cd backend && source .venv/bin/activate && python -m app.seed.run
```

Open `http://localhost:3000`.

---

## 1. Seed mock enterprise data

Already done by the setup step above. Confirm it worked:

```bash
curl -s http://localhost:8000/recovery-cases | python3 -m json.tool
```

Expect 5 cases: Northwind (LOW/Open), Bluepeak (MEDIUM/Open), Sundial (MEDIUM/Monitoring), Vertex (HIGH/Escalated, ₹18,00,000 — the brief's own example), Aarav (LOW/Closed, recovered).

## 2-4. Identify overdue invoices, create recovery cases, calculate revenue at risk

The seeded scenarios already demonstrate this, but the convincing proof is watching the engine do it to an invoice it has never seen:

```bash
docker exec infra-db-1 psql -U recovery_user -d recovery_db -c "
INSERT INTO companies (id, name, industry, segment, created_at)
VALUES (gen_random_uuid(), 'Demo Co', 'Testing', 'SMB', now())
RETURNING id;"
# copy the returned id into the next command

docker exec infra-db-1 psql -U recovery_user -d recovery_db -c "
INSERT INTO invoices (id, company_id, invoice_number, amount_total, currency, amount_paid, issue_date, due_date, status, created_at, updated_at)
VALUES (gen_random_uuid(), '<company-id>', 'INV-DEMO-0001', 200000.00, 'INR', 0.00, CURRENT_DATE - 32, CURRENT_DATE - 2, 'SENT', now(), now());"

curl -s -X POST http://localhost:8000/recovery-cases/detect-overdue | python3 -m json.tool
```

Expect `"cases_created": 1`. The invoice's status flips `SENT` → `OVERDUE`, a case opens with `revenue_at_risk: "200000.00"`. Refresh the dashboard — "Total Revenue at Risk" goes up by exactly ₹2,00,000.

## 5. Calculate recovery probability

Same response already includes it — check the new case:

```bash
curl -s http://localhost:8000/recovery-cases | python3 -c "import json,sys; print([c for c in json.load(sys.stdin) if c['invoice_number']=='INV-DEMO-0001'])"
```

`risk_level`/`recovery_probability` are populated immediately (XGBoost, trained on synthetic data — see [docs/architecture.md](architecture.md)), not left null.

## 6-9. AI diagnosis, AI recommendation, policy validation, execute action

On the dashboard, click into any non-closed case and click **"Run recovery cycle"**. You'll see:
- A diagnosis (`Diagnosis` section) and a recommended action, each tagged with the model that produced it (`rule-based-fallback` unless `GOOGLE_API_KEY` or `OPENAI_API_KEY` is configured).
- A new row in **Action History** showing the action, its status (`Executed`), and the **policy decision** that gated it.

**The one that matters most** — click "Run recovery cycle" again immediately: the agent will recommend another reminder, and the policy engine will reject it (`MIN_TIME_BETWEEN_REMINDERS_DAYS not yet elapsed`), executing `WAIT` instead. That's the whole architectural thesis of this project, visible on screen.

## 10-11. Simulate customer payment; continue or stop the workflow

On the same case, click **"Simulate payment"**, then **"Run recovery cycle"** once more. The case flips to **Closed**, `Recovered Amount` shows the payment. Run the cycle on a case that *hasn't* been paid and it stays open (or escalates) instead — the workflow genuinely branches on outcome.

## 12. Track promise-to-pay

Open **Sundial Retail Group**'s case — it already has a `PENDING` promise-to-pay from the seed narrative. To watch one actually break:

```bash
docker exec infra-db-1 psql -U recovery_user -d recovery_db -c "
UPDATE promise_to_pay SET promised_date = CURRENT_DATE - 2
WHERE recovery_case_id = (SELECT rc.id FROM recovery_cases rc JOIN invoices i ON i.id = rc.invoice_id WHERE i.invoice_number = 'INV-SUNDIAL-4002');"

curl -s -X POST http://localhost:8000/recovery-cases/detect-overdue | python3 -m json.tool
```

Expect `"promises_broken": 1`. Then run a recovery cycle on Sundial's case — the policy engine forces `ESCALATE`, overriding whatever the agent recommends, because a broken commitment is a hard fact (see `has_broken_promise` in `app/services/policy_engine.py`).

## 13. Escalate when necessary

Already demonstrated above two ways: a broken promise, and Vertex's seeded scenario (high-value + significantly overdue forces escalation on the very first cycle, regardless of what the agent recommends).

## 14-15. Close recovered cases; show recovered revenue on the dashboard

Aarav's seeded case is already `Closed` with `₹95,000` recovered — visible in both the case table and the "Total Revenue Recovered" KPI. Combined with step 10-11 above (simulate + run on any other case), you can create a second recovered case live and watch both KPIs move.

## 16. View the complete audit trail

Any case detail page's **Audit Trail** section — a chronological log from `Case Created` through every risk score, diagnosis, recommendation, policy decision, and execution, down to `Case Closed`. Or via API:

```bash
curl -s http://localhost:8000/recovery-cases/<case-id>/audit-trail | python3 -m json.tool
```

---

## Bonus: things beyond the brief's checklist, also real

- **Concurrency safety** — fire two `POST /recovery-cases/{id}/run` calls at the same case simultaneously; one succeeds, one returns `409 Conflict`, and exactly one new action gets created (see Phase 14).
- **Event broadcasting** — `docker compose up -d kafka` + `python -m app.events.consumer` shows every domain event (`invoice.overdue`, `recovery.case_created`, ...) flowing in real time as you trigger actions.
- **Structured, correlated logs** — every log line during one request shares a `request_id`; `grep '"request_id": "<id>"'` on the container output reconstructs that request's full story across the risk engine, the LangGraph workflow, and the event publisher.

## Cleanup

```bash
python -m app.seed.run    # wipes and rebuilds the demo dataset
```
