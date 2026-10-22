import math

import pytest

from paperbot.metrics import bootstrap_mean_ci, compare_runs, format_comparison
from sniper_helpers import Curve, make_sniper, pump_until_signal


def test_bootstrap_ci_brackets_mean_and_detects_sign():
    pos = [5.0, 7.0, 6.0, 4.0, 8.0] * 40
    lo, hi, p = bootstrap_mean_ci(pos)
    assert lo < 6.0 < hi and lo > 0 and p == 1.0
    noisy = [30.0, -28.0] * 100  # mean +1 with huge spread
    lo, hi, p = bootstrap_mean_ci(noisy)
    assert lo < 0 < hi and 0.5 < p < 1
    assert all(math.isnan(x) for x in bootstrap_mean_ci([1.0]))


def test_bootstrap_is_deterministic():
    vals = [1.0, -2.0, 3.5, -0.5, 2.0] * 10
    assert bootstrap_mean_ci(vals) == bootstrap_mean_ci(vals)


def test_compare_runs_reports_per_trade_percentages():
    eng, conn, _ = make_sniper(max_hold_seconds=1)
    t = 1_800_000_000.0
    for i in range(4):
        t = pump_until_signal(eng, Curve(f"T{i}"), t=t + 100)
        eng.tick(t + 2)
    (row,) = compare_runs(conn)
    assert row["run"] == "sniper" and row["trades"] == 4
    assert row["expectancy_pct"] == pytest.approx(row["first_half_pct"] / 2 + row["second_half_pct"] / 2)
    assert row["ci95_low"] <= row["expectancy_pct"] <= row["ci95_high"]
    assert row["costs_pct_of_traded"] > 0
    text = format_comparison([row])
    assert "PAPER TRADING ONLY" in text and "95% CI" in text and "in-sample" in text
