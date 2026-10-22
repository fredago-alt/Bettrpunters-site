import json

import pytest

from conftest import T0, Harness, make_settings


def test_entry_sizing_is_10pct_capped_at_5_aud(harness):
    harness.feed(T0)
    pos = harness.position()
    assert pos["status"] == "open"
    assert pos["cost_aud"] == pytest.approx(5.0)  # min(50 * 10%, A$5)
    assert harness.engine.cash() == pytest.approx(45.0)


def test_entry_sizing_uses_10pct_when_below_cap():
    h = Harness(make_settings(starting_cash_aud=30.0))
    h.feed(T0)
    assert h.position()["cost_aud"] == pytest.approx(3.0)


def test_entry_records_fx_and_costs(harness):
    harness.feed(T0, price=0.001)
    p = harness.position()
    assert p["entry_aud_per_usd"] == 1.5
    assert p["entry_exec_price_usd"] == pytest.approx(0.001 * 1.015)
    assert p["qty"] == pytest.approx((5.0 / 1.01) / 1.5 / (0.001 * 1.015))
    assert p["entry_fee_aud"] + p["entry_notional_aud"] == pytest.approx(5.0)


def test_one_open_position_at_a_time(harness):
    harness.feed(T0, token="A", pair="PA")
    harness.feed(T0 + 1, token="B", pair="PB")
    assert harness.position("B") is None
    d = harness.conn.execute("SELECT reasons_json FROM filter_decisions WHERE token_address='B'").fetchone()
    assert "max_open_positions" in json.loads(d[0])["reasons"]


def test_take_profit(harness):
    harness.feed(T0, price=0.001)
    harness.feed(T0 + 60, price=0.00125)
    p = harness.position()
    assert p["status"] == "closed" and p["exit_reason"] == "take_profit"
    assert p["pnl_pct"] >= 15.0
    assert harness.engine.cash() == pytest.approx(45.0 + p["proceeds_aud"])


def test_stop_loss_uses_executable_net_value(harness):
    harness.feed(T0, price=0.001)
    harness.feed(T0 + 60, price=0.00095)  # -5% reference move ~ -9.6% net: no stop yet
    assert harness.position()["status"] == "open"
    harness.feed(T0 + 120, price=0.00094)  # -6% reference ~ -10.6% net -> stop
    p = harness.position()
    assert p["exit_reason"] == "stop_loss" and p["pnl_pct"] <= -10.0


def test_max_holding_time(harness):
    harness.feed(T0, price=0.001)
    harness.feed(T0 + 14 * 60, price=0.00102)
    assert harness.position()["status"] == "open"
    harness.feed(T0 + 15 * 60, price=0.00102)
    p = harness.position()
    assert p["exit_reason"] == "max_hold"
    assert p["hold_seconds"] == pytest.approx(15 * 60)


def test_pnl_identity_and_no_double_count(harness):
    harness.feed(T0, price=0.001)
    harness.feed(T0 + 900, price=0.001)  # flat, exits on max hold
    p = harness.position()
    costs = p["entry_fee_aud"] + p["exit_fee_aud"] + p["entry_slippage_aud"] + p["exit_slippage_aud"]
    assert p["pnl_aud"] == pytest.approx(p["proceeds_aud"] - p["cost_aud"])
    assert -p["pnl_aud"] == pytest.approx(costs)
    assert harness.engine.cash() == pytest.approx(50.0 + p["pnl_aud"])


def test_latency_fills_at_later_observation_not_signal_price():
    h = Harness(make_settings(latency_seconds=3.0))
    h.feed(T0, price=0.001)
    assert h.position()["status"] == "pending"
    h.feed(T0 + 1, price=0.0011)  # inside latency window: must not fill
    assert h.position()["status"] == "pending"
    h.feed(T0 + 4, price=0.00105)
    p = h.position()
    assert p["status"] == "open" and p["entry_ref_price_usd"] == 0.00105
    assert p["entry_exec_price_usd"] == pytest.approx(0.00105 * 1.015)


def test_entry_abandoned_on_price_drift():
    h = Harness(make_settings(latency_seconds=3.0, max_entry_drift_pct=10))
    h.feed(T0, price=0.001)
    h.feed(T0 + 5, price=0.0012)
    p = h.position()
    assert p["status"] == "abandoned" and p["abandon_reason"] == "entry_price_drift"
    assert h.engine.cash() == 50.0


def test_entry_fill_failure():
    h = Harness(make_settings(entry_fill_failure_prob=0.999))
    h.feed(T0)
    p = h.position()
    assert p["status"] == "abandoned" and p["abandon_reason"] == "simulated_fill_failure"
    assert h.engine.cash() == 50.0


def test_pending_entry_times_out_without_observation():
    h = Harness(make_settings(latency_seconds=3.0, fill_timeout_seconds=90))
    h.feed(T0)
    h.engine.on_clock(T0 + 91)
    assert h.position()["abandon_reason"] == "no_fill_observation"


def test_failed_exit_retries_then_fills():
    h = Harness(make_settings(exit_fill_failure_prob=0.5), seed=3)
    h.feed(T0, price=0.001)
    t = T0 + 60
    for _ in range(50):
        h.feed(t, price=0.0013)
        if h.position()["status"] == "closed":
            break
        t += 15
    p = h.position()
    assert p["status"] == "closed" and p["exit_reason"] == "take_profit"
    failed = h.conn.execute("SELECT COUNT(*) FROM order_events WHERE side='sell' AND event='failed'").fetchone()[0]
    assert failed == p["failed_exit_attempts"]


def test_missing_data_is_failed_exit_then_stale_writeoff(harness):
    harness.feed(T0, price=0.001)
    harness.feed_missing(T0 + 16 * 60)
    p = harness.position()
    assert p["status"] == "open" and p["failed_exit_attempts"] == 1
    harness.engine.on_clock(T0 + 61 * 60)
    p = harness.position()
    assert p["status"] == "closed" and p["exit_reason"] == "stale_writeoff"
    assert p["pnl_aud"] == pytest.approx(-5.0)  # 0% recovery assumption
    assert harness.engine.cash() == pytest.approx(45.0)


def test_token_traded_once_per_run(harness):
    harness.feed(T0, price=0.001)
    harness.feed(T0 + 900, price=0.001)
    assert harness.position()["status"] == "closed"
    harness.feed(T0 + 960, price=0.001)
    n = harness.conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0]
    assert n == 1


def test_open_position_updated_without_entry_evaluation(harness):
    harness.feed(T0, price=0.001)
    # Position pair now fails every filter (no liquidity, old) but must still be marked/exited.
    harness.feed(T0 + 60, price=0.0013, liquidity=None, created=T0 - 10_000)
    assert harness.position()["exit_reason"] == "take_profit"


def test_summary_equity_and_unrealised(harness):
    harness.feed(T0, price=0.001)
    harness.feed(T0 + 60, price=0.00105)
    a = harness.engine.summary()
    p = harness.position()
    assert a.cash_aud == pytest.approx(45.0)
    assert a.equity_aud == pytest.approx(45.0 + p["last_nlv_aud"])
    assert a.unrealised_pnl_aud == pytest.approx(p["last_nlv_aud"] - 5.0)
    assert a.realised_pnl_aud == 0.0
