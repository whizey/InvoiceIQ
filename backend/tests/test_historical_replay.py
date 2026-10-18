"""Historical clocks must preserve source chronology and actual execution time."""
from datetime import date, datetime, timezone

import pytest

from app.core.time import days_since_workflow_action, historical_as_of, utc_now, utc_today
from app.seed.historical_replay import replay_selection
from app.seed.load_real_dataset import read_dataset


def test_historical_clock_is_scoped_without_shifting_system_timestamps():
    actual = utc_today()
    with historical_as_of(date(2015, 2, 3)):
        assert utc_today() == date(2015, 2, 3)
        assert utc_now().date() == actual
        assert days_since_workflow_action(datetime.now(timezone.utc)) == 0
        with historical_as_of(date(2014, 1, 1)):
            assert utc_today() == date(2014, 1, 1)
        assert utc_today() == date(2015, 2, 3)
    assert utc_today() == actual


def test_real_replay_selection_is_deterministic_observable_and_supports_review():
    frame = read_dataset()
    cutoff = date(2015, 2, 3)
    selected = replay_selection(frame, cutoff)
    assert selected == replay_selection(frame.sample(frac=1, random_state=69), cutoff)
    assert len(set(selected)) == 3
    records = frame.set_index('Document_No').loc[selected]
    assert all(records.Posting_Date.dt.date <= cutoff)
    assert all(records.Net_Due_Date.dt.date < cutoff)
    assert all(records.Clearing_date.dt.date > cutoff)
    assert records.iloc[0].Amount >= 1_000_000
    assert (cutoff - records.iloc[0].Net_Due_Date.date()).days < 45
    assert all(records.iloc[1:].Amount < 1_000_000)
    with pytest.raises(ValueError, match='cutoff lacks'):
        replay_selection(frame, date(2016, 3, 17))
