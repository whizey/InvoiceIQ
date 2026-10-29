# The assistant

The assistant answers questions about a workspace's receivables ("who owes the most past 90 days?", "what if Acme
pays?"). It's a tool-calling loop in [`finance_agent.py`](../backend/app/agents/finance_agent.py), not one prompt with
the whole ledger pasted in.

## How an answer is made

1. The model picks tools. All of them are read-only and only see the caller's workspace: portfolio summary,
   ageing, overdue accounts, invoice search, invoice detail, review queue, monthly trend and DSO, customer profile,
   risk explanation, `what_if_paid`, invoice notes (`get_notes`, `search_notes`) and a calculator that only does
   arithmetic. A step can call up to four tools, and they run concurrently. The loop stops after five model calls.
2. Every figure is checked. Code pulls each number out of the answer (₹15.2 lakh, 83%, "ninety-one percent")
   and matches it against what the tools returned, at the precision it was written. If something doesn't match, the
   model gets one chance to fix it; after that the sentence is removed. If the first sentence goes, the user is told
   the answer couldn't be verified.
3. Suggestions go through the same rules as people. The model can suggest a review decision. Code refuses
   suggestions on paid invoices, escalations before the due date or without a reason, other workspaces' invoices and
   repeats. Allowed ones show an Apply button and nothing is saved until someone clicks it.
4. Fallbacks. `LLM_MODEL`, then each of `GEMINI_FALLBACK_MODELS`, then Groq if a key is set, then a rule-based
   answer. A rate-limited model is retried once if the wait is a few seconds, otherwise skipped until its quota
   resets.

Workspace figures come from the same function the dashboard uses (`compute_portal_dashboard`), so the two can't
disagree. Notes reach the model wrapped in `<data>` tags, and a note that tries to give the assistant instructions is
withheld. Invoice numbers are matched ignoring case and separators; one that isn't in the workspace stays in the
answer, marked "not found in your records".

Answers stream as progress steps ("Checking ageing…"), but the text itself is only sent once the checks pass.

## Tracing

Every model call, tool call, check and fallback is written to `assistant_traces` with timings but without the
question or answer text. With `LANGCHAIN_TRACING_V2` and `LANGCHAIN_API_KEY` set, each answer is also one LangSmith
trace. For company workspaces that trace carries structure and timings only, unless
`LANGSMITH_LOG_CUSTOMER_CONTENT=true`. The `/assistant/reliability` page shows which model answered, how the figures
fared and response times. `python -m app.agents.check_providers` tries each model in the chain once.

## Note search

`search_notes` ranks notes two ways and merges the rankings with reciprocal rank fusion
([`note_search.py`](../backend/app/agents/note_search.py)): TF-IDF with light stemming, and cosine similarity of
Gemini embeddings (`gemini-embedding-001`, 256 dimensions). A note only counts as a meaning match above 0.75 cosine.
Without an embedding key it's keywords only.

On a hand-labelled set of 28 notes and 25 queries
([`notes_retrieval_v1.json`](../backend/tests/fixtures/notes_retrieval_v1.json)), with embeddings cached so CI can
rerun it offline:

| Method | Recall@3 | MRR | Recall@3 keyword / mixed / paraphrase | False matches per unrelated query |
|---|---|---|---|---|
| Keywords | 0.65 | 0.72 | 1.00 / 0.92 / 0.07 | 0.6 |
| Meaning | 0.90 | 0.88 | 0.86 / 1.00 / 0.86 | 0.2 |
| Hybrid | 0.90 | 0.89 | 1.00 / 1.00 / 0.71 | 0.6 |

Hybrid keeps exact matches like invoice numbers or "IFSC" that meaning-only search misses, and gets most of the
paraphrase recall. The 0.75 floor was picked on this same set, so these numbers are optimistic.
`gemini-embedding-2` scored lower. `python -m app.agents.retrieval_eval --sweep` rebuilds the table.

Embedding the question costs about 0.65 s. Because one step often searches several times, those searches run at the
same time and repeated questions hit a cache: four searches in one step went from 2.6 s one after another to 0.74 s.

## Evaluation

There's no answer key for live questions, so the assistant is scored on 108 hand-written cases over an 11-invoice
fixture ([`assistant_eval_v1.json`](../backend/tests/fixtures/assistant_eval_v1.json)): figures, tricky wording,
follow-ups, prompt injection (including an instruction planted in a note), out-of-scope requests and policy cases.
Expected figures are worked out by hand. Each model answers alone, three times; an answer counts only if it states
every key figure and lets nothing forbidden through.

The README's per-category table mixes two different things under "injection": ordinary wrong-answer mistakes (a
missed figure, a stray mention of another tenant's name) and an actual forbidden action getting approved. Eight of
the cases are built to test the second, more serious thing directly (prompt injection, an already-paid or
already-escalated invoice, a planted note, each asking for a decision the policy engine should never allow). The
README states that count on its own, separately from the category accuracy, because a low accuracy number there
doesn't by itself say whether anything unsafe actually happened.

CI runs the offline part on every change. Live model evaluations can be run manually with
[`run_assistant_eval_daily.sh`](../backend/scripts/run_assistant_eval_daily.sh) using separate evaluation keys;
the published README results are a dated snapshot.
`gemini-3.6-flash` isn't in that table: its free tier allows about six questions a day.
