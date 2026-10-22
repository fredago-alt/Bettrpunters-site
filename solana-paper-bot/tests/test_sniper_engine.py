import json

import pytest

from paperbot.sniper.pumpfun import TOKEN_UNIT, CompleteEvent, CurveAccount
from sniper_helpers import T0, Curve, make_sniper, position, pump_until_signal


def test_enters_after_momentum_conditions_and_accounts_in_aud():
    eng, conn, sol = make_sniper()
    c = Curve()
    t = pump_until_signal(eng, c)
    p = position(conn)
    assert p["status"] == "open"
    assert p["cost_aud"] == pytest.approx(5.0, abs=1e-6)  # min(10% of A$50, A$5)
    assert eng.cash() == pytest.approx(45.0, abs=1e-6)
    assert p["entry_sol_usd"] == 150.0 and p["entry_aud_per_usd"] == 1.5
    # A$5 -> US$3.333 -> 0.02222 SOL; check the fill against the curve
    ev = json.loads(conn.execute("SELECT details_json FROM order_events WHERE event='filled'").fetchone()[0])
    assert ev["budget_lamports"] == int(5.0 / 1.5 / 150.0 * 1e9)
    assert ev["curve_in"] + ev["trade_fee"] + ev["network_fee"] == ev["budget_lamports"]
    assert ev["fee_bps"] == 125  # 95 protocol + 30 creator, read from the events
    assert p["qty"] == pytest.approx(ev["tokens_raw"] / TOKEN_UNIT)
    assert t > T0


def test_no_entry_without_enough_distinct_buyers():
    eng, conn, _ = make_sniper(min_unique_buyers=5)
    c = Curve()
    eng.on_create(c.create_event(), T0)
    for i in range(8):
        eng.on_trade(c.trade("same-wallet", 0.5), T0 + i + 1)
    assert position(conn) is None


def test_creator_buys_do_not_count_as_demand():
    eng, conn, _ = make_sniper(min_net_inflow_sol=1.0)
    c = Curve(creator="DEV")
    eng.on_create(c.create_event(), T0)
    eng.on_trade(c.trade("DEV", 1.5), T0 + 1)  # creator's own buy, within max_creator_buy_sol
    for i in range(3):
        eng.on_trade(c.trade(f"b{i}", 0.2), T0 + 2 + i)  # only 0.6 SOL of outside demand
    assert position(conn) is None


def test_creator_sell_blocks_entry_and_forces_exit():
    eng, conn, _ = make_sniper()
    c = Curve(creator="DEV")
    eng.on_create(c.create_event(), T0)
    eng.on_trade(c.trade("DEV", 0.5), T0 + 1)
    eng.on_trade(c.trade("DEV", tokens=10**9, is_buy=False), T0 + 2)
    for i in range(4):
        eng.on_trade(c.trade(f"b{i}", 0.5), T0 + 3 + i)
    assert position(conn) is None
    status = conn.execute("SELECT status, rejection FROM pump_tokens").fetchone()
    assert status[0] == "rejected" and "creator_sold" in status[1]

    eng2, conn2, _ = make_sniper()
    c2 = Curve(creator="DEV")
    t = pump_until_signal(eng2, c2)
    eng2.on_trade(c2.trade("DEV", tokens=10**6, is_buy=False), t + 1)
    assert position(conn2)["exit_reason"] == "creator_sold"


def test_mayhem_and_non_sol_tokens_are_skipped_and_recorded():
    eng, conn, _ = make_sniper()
    for c in (Curve("M1", mayhem=True), Curve("M2", quote=Curve("Q").mint)):
        pump_until_signal(eng, c)
    assert position(conn) is None
    rows = dict(conn.execute("SELECT status, rejection FROM pump_tokens").fetchall() and
                conn.execute("SELECT rejection, status FROM pump_tokens").fetchall())
    assert rows == {"mayhem_mode_unmodelled": "skipped", "non_sol_quote": "skipped"}


def test_token_too_old_is_rejected_once():
    eng, conn, _ = make_sniper(max_age_seconds=10)
    c = Curve()
    eng.on_create(c.create_event(), T0)
    for i in range(4):
        eng.on_trade(c.trade(f"b{i}", 0.5), T0 + 20 + i)
    assert position(conn) is None
    assert conn.execute("SELECT status FROM pump_tokens").fetchone()[0] == "rejected"


def test_latency_fill_uses_reserves_after_the_delay():
    eng, conn, _ = make_sniper(latency_ms=500, max_entry_drift_pct=50)
    c = Curve()
    t = pump_until_signal(eng, c, buyers=3)  # signal fires on the 3rd buy
    p = position(conn)
    assert p["status"] == "pending"
    eng.on_trade(c.trade("late", 1.0), t + 0.2)  # someone else buys during our latency
    eng.tick(t + 0.3)
    assert position(conn)["status"] == "pending"
    eng.tick(t + 0.6)
    p = position(conn)
    assert p["status"] == "open" and p["entry_ref_price_usd"] > p["signal_price_usd"]


def test_entry_abandoned_on_drift():
    eng, conn, _ = make_sniper(latency_ms=500, max_entry_drift_pct=5)
    c = Curve()
    t = pump_until_signal(eng, c, buyers=3)
    eng.on_trade(c.trade("whale", 5.0), t + 0.1)
    eng.tick(t + 0.6)
    assert position(conn)["abandon_reason"] == "entry_price_drift"
    assert eng.cash() == 50.0


def test_take_profit_on_net_value():
    eng, conn, _ = make_sniper(take_profit_pct=30)
    c = Curve()
    t = pump_until_signal(eng, c)
    for i in range(30):
        eng.on_trade(c.trade(f"fomo{i}", 1.0), t + 1 + i)
        if position(conn)["status"] == "closed":
            break
    p = position(conn)
    assert p["exit_reason"] == "take_profit" and p["pnl_pct"] >= 30
    assert eng.cash() == pytest.approx(45.0 + p["proceeds_aud"])


def test_stop_loss():
    eng, conn, _ = make_sniper(stop_loss_pct=-10)
    c = Curve()
    t = pump_until_signal(eng, c)
    eng.on_trade(c.trade("b0", tokens=60_000_000 * TOKEN_UNIT, is_buy=False), t + 1)  # early buyers dump
    p = position(conn)
    assert p["exit_reason"] == "stop_loss" and p["pnl_pct"] <= -10


def test_max_hold_exits_on_clock_without_new_trades():
    eng, conn, _ = make_sniper(max_hold_seconds=120)
    c = Curve()
    t = pump_until_signal(eng, c, buyers=3)
    eng.tick(t + 119)
    assert position(conn)["status"] == "open"
    eng.tick(t + 121)
    p = position(conn)
    assert p["exit_reason"] == "max_hold"
    # flat curve: loss is exactly fees both ways + both network fees (+ rounding), slippage is attribution only
    assert p["pnl_aud"] == pytest.approx(-(p["entry_fee_aud"] + p["exit_fee_aud"]), abs=1e-4)


def test_failed_exit_retries():
    eng, conn, _ = make_sniper(max_hold_seconds=5, exit_fill_failure_prob=0.6)
    c = Curve()
    t = pump_until_signal(eng, c)
    for k in range(100):
        eng.tick(t + 6 + k)
        if position(conn)["status"] == "closed":
            break
    p = position(conn)
    assert p["status"] == "closed"
    n = conn.execute("SELECT COUNT(*) FROM order_events WHERE side='sell' AND event='failed'").fetchone()[0]
    assert n == p["failed_exit_attempts"]


def test_curve_completion_closes_position():
    eng, conn, _ = make_sniper()
    c = Curve()
    t = pump_until_signal(eng, c)
    eng.on_complete(CompleteEvent(c.mint, c.curve), t + 1)
    assert position(conn)["exit_reason"] == "curve_complete"


def test_independent_account_refresh_marks_position():
    eng, conn, _ = make_sniper(take_profit_pct=30)
    c = Curve()
    t = pump_until_signal(eng, c)
    # the stream is silent, but an RPC read shows the curve moved up a lot
    acc = CurveAccount(c.vt // 2, c.vq * 2, c.rt // 2, c.vq, False, False, c.quote, 30)
    eng.on_curve_account(c.mint, acc, t + 5)
    assert position(conn)["exit_reason"] == "take_profit"


def test_no_sol_price_means_no_trade():
    eng, conn, sol = make_sniper()
    sol.usd = None
    pump_until_signal(eng, Curve())
    assert position(conn) is None
    d = json.loads(conn.execute("SELECT reasons_json FROM filter_decisions").fetchone()[0])
    assert "sol_price_unavailable" in d["reasons"]


def test_one_position_at_a_time():
    eng, conn, _ = make_sniper()
    pump_until_signal(eng, Curve("A"))
    pump_until_signal(eng, Curve("B"), t=T0 + 10)
    assert conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0] == 1


def test_bars_written_and_immutable():
    import sqlite3

    eng, conn, _ = make_sniper(min_buys=999)
    c = Curve()
    pump_until_signal(eng, c, buyers=5)
    eng.tick(T0 + 30)
    rows = conn.execute("SELECT n_buys, unique_buyers, close_price_sol FROM pump_bars").fetchall()
    assert sum(r[0] for r in rows) == 5 and rows[-1][1] == 5 and rows[-1][2] > 0
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE pump_bars SET n_buys = 0")


def test_states_expire_after_track_window():
    eng, conn, _ = make_sniper(min_buys=999, track_window_seconds=100)
    pump_until_signal(eng, Curve())
    assert len(eng.states) == 1
    eng.tick(T0 + 101)
    assert eng.states == {}


def test_metrics_and_snapshot_work_for_sniper_run():
    from paperbot.metrics import compute

    eng, conn, _ = make_sniper(max_hold_seconds=1)
    t = pump_until_signal(eng, Curve())
    eng.tick(t + 2)
    eng.snapshot(t + 3)
    m = compute(conn, "sniper")
    assert m.trades == 1 and m.fees_aud > 0
