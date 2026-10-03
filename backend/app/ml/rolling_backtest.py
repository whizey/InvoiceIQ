"""Rolling-origin backtest: retrain on the past, score the next window.

Run with:  python -m app.ml.rolling_backtest

Uses train + validation only. The test split is never read.

Three variants, same random forest, same windows:
  plain    no weighting, no regime feature
  recency  sample weights decay with age (half-life HALF_LIFE_DAYS)
  regime   recency + trailing_late_rate, a lagged base-rate feature

trailing_late_rate for a row is the late rate of invoices whose due date
fell in the 30 days ending LAG_DAYS before the row's posting date. The lag
is the observation horizon, so every outcome used was already known on
that posting date. No clearing dates are needed.
"""

import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import roc_auc_score

from app.ml.common import (
    RANDOM_STATE,
    load_splits,
    prepare_categorical,
    to_numeric_codes,
    within_period_auc,
)
from app.ml.datasets.build_primary_features import (
    OBSERVATION_HORIZON_DAYS,
    TARGET_COLUMN,
)

HALF_LIFE_DAYS = 270
LAG_DAYS = OBSERVATION_HORIZON_DAYS
WINDOW_DAYS = 35
MIN_TRAIN_DAYS = 150
RF_PARAMS = dict(
    n_estimators=300, max_depth=12, min_samples_leaf=20,
    max_features="sqrt", n_jobs=-1, random_state=RANDOM_STATE,
)


def trailing_late_rate(frame: pd.DataFrame, pool: pd.DataFrame) -> np.ndarray:
    """Late rate of pool invoices due in [posting-LAG-30, posting-LAG]."""
    daily = (
        pool.assign(due=pool["net_due_date"].dt.normalize())
        .groupby("due")[TARGET_COLUMN].agg(["sum", "count"]).sort_index()
    )
    days = daily.index.values
    csum = np.concatenate([[0], daily["sum"].cumsum().values])
    ccnt = np.concatenate([[0], daily["count"].cumsum().values])
    post = frame["posting_date"].dt.normalize()
    hi = (post - pd.Timedelta(days=LAG_DAYS)).values
    lo = (post - pd.Timedelta(days=LAG_DAYS + 30)).values
    i_hi = np.searchsorted(days, hi, side="right")
    i_lo = np.searchsorted(days, lo, side="left")
    n = ccnt[i_hi] - ccnt[i_lo]
    s = csum[i_hi] - csum[i_lo]
    return np.where(n >= 20, s / np.maximum(n, 1), np.nan)


def run_window(train: pd.DataFrame, test: pd.DataFrame, variant: str) -> np.ndarray:
    tr, te = train.reset_index(drop=True), test.reset_index(drop=True)
    extra_tr = extra_te = None
    if variant == "regime":
        extra_tr = trailing_late_rate(tr, tr)
        extra_te = trailing_late_rate(te, pd.concat([tr, te]))
    p_tr, p_te = prepare_categorical([tr, te])
    (X_tr, X_te), _ = to_numeric_codes([p_tr, p_te])
    if extra_tr is not None:
        fill = np.nanmedian(extra_tr) if np.isfinite(extra_tr).any() else 0.4
        X_tr = X_tr.assign(trailing_late_rate=np.where(np.isnan(extra_tr), fill, extra_tr))
        X_te = X_te.assign(trailing_late_rate=np.where(np.isnan(extra_te), fill, extra_te))
    weights = None
    if variant in ("recency", "regime"):
        age = (tr["posting_date"].max() - tr["posting_date"]).dt.days.values
        weights = 0.5 ** (age / HALF_LIFE_DAYS)
    model = RandomForestClassifier(**RF_PARAMS)
    model.fit(X_tr, tr[TARGET_COLUMN].values, sample_weight=weights)
    return model.predict_proba(X_te)[:, 1]


def main() -> None:
    train, validation, _test_never_used = load_splits()
    data = pd.concat([train, validation]).sort_values("posting_date").reset_index(drop=True)
    start = data["posting_date"].min() + pd.Timedelta(days=MIN_TRAIN_DAYS)
    end = data["posting_date"].max()
    variants = ["plain", "recency", "regime"]
    rows = []
    edge = start
    while edge < end:
        stop = edge + pd.Timedelta(days=WINDOW_DAYS)
        tr = data[data["posting_date"] < edge]
        te = data[(data["posting_date"] >= edge) & (data["posting_date"] < stop)]
        edge = stop
        if len(te) < 100 or te[TARGET_COLUMN].nunique() < 2:
            continue
        row = {"window": f"{te['posting_date'].min():%Y-%m-%d}", "rows": len(te),
               "late": te[TARGET_COLUMN].mean(), "train_late": tr[TARGET_COLUMN].mean()}
        for v in variants:
            p = run_window(tr, te, v)
            row[v] = within_period_auc(te, p)[0]
            row[v + "_pooled"] = roc_auc_score(te[TARGET_COLUMN], p)
        rows.append(row)
        print(f"  {row['window']}  rows {row['rows']:>5}  late {row['late']:.2f} "
              f"(train {row['train_late']:.2f})  "
              + "  ".join(f"{v} {row[v]:.3f}" for v in variants), flush=True)

    df = pd.DataFrame(rows)
    print("\nROLLING BACKTEST (within-period AUC per window; test split unused)")
    print(f"  windows: {len(df)}   window size: {WINDOW_DAYS}d   half-life: {HALF_LIFE_DAYS}d")
    print(f"  {'variant':<10} {'mean':>7} {'min':>7} {'std':>7} {'<0.50':>6} {'pooled mean':>12}")
    for v in variants:
        print(f"  {v:<10} {df[v].mean():>7.4f} {df[v].min():>7.4f} {df[v].std():>7.4f} "
              f"{int((df[v] < 0.5).sum()):>6} {df[v + '_pooled'].mean():>12.4f}")
    print("\n  Keep recency or regime only if it lifts min and cuts the <0.50 count.")
    print("  A higher mean alone does not count.")


if __name__ == "__main__":
    main()
