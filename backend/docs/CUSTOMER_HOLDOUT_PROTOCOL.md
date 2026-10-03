# Frozen New-Customer Diagnostic

Frozen: 2026-10-03

## Question

The chronological validation split asks:

> Can the model rank later invoices in the observed customer population?

It does not cleanly answer:

> How does the system behave for a completely unseen customer?

This diagnostic addresses the second question.

## Selection

The diagnostic customer set is chosen before its performance is examined.

Population:

- unique customer IDs appearing in train or validation
- test split is not read

Rule:

1. Normalize the customer identifier as text.
2. Hash `salt|customer_id` with SHA-256.
3. Sort hashes ascending.
4. Reserve the first `ceil(15%)`.

The frozen salt is:

`recoveriq-new-customer-diagnostic-v1`

The repository stores hashes rather than raw customer IDs in the manifest.

## Use

This holdout is:

- a new-customer stress diagnostic
- not a replacement for chronological validation
- not used to choose hyperparameters
- not used to choose the winning model
- not used to tune features
- not part of the reserved final test split

## Interpretation

The dataset has only a small number of customers.

Therefore this diagnostic will have high variance and must not be presented as
a precise population-level estimate.

Its purpose is to expose whether performance collapses when customer-specific
history is unavailable.

## Frozen artifact

`data/processed/primary/customer_holdout_manifest.json`

The manifest records:

- selection rule
- selection fraction
- hashed customer IDs
- train/validation file SHA-256 fingerprints
- explicit confirmation that no target columns or test split were read
