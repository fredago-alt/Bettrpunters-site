import pytest

from paperbot.db import open_db
from paperbot.research import _slot_trade, single_timeline, slot_ceiling
from sniper_helpers import T0, Curve, make_sniper


def bar(i, start, end, n):
    return {"id": i, "bar_start": start, "bar_end": end, "n_buys": n, "n_sells": 0}


def test_single_timeline_removes_interleaved_duplicate_processes():
    a = [bar(1, 0.0, 5.0, 4), bar(4, 5.2, 10.2, 3)]  # process A
    b = [bar(2, 0.01, 5.01, 4), bar(5, 5.21, 10.21, 3)]  # process B (same trades, ms later)
    c = [bar(3, 0.02, 5.02, 1)]  # process C started late / saw less
    kept = single_timeline(a + b + c)
    assert len(kept) == 2 and sum(r["n_buys"] for r in kept) == 7
    assert single_timeline(a) == a


def _trades_for(curve, plan, create_slot=100, create_sig="CREATE"):
    """plan: list of (slot_offset, user, sol) buys; offset 0 with user 'DEV' is the launch tx."""
    rows, i = [], 0
    for off, user, sol in plan:
        ev = curve.trade(user, sol)
        i += 1
        rows.append({"id": i, "slot": create_slot + off, "signature": create_sig if (off == 0 and user == "DEV")
                     else f"s{i}", "is_buy": 1, "quote_amount": ev.quote_amount, "token_amount": ev.token_amount,
                     "virtual_quote": ev.virtual_quote, "virtual_token": ev.virtual_token, "fee_bps": 125})
    return rows


def test_earlier_slot_entry_beats_later_entry_in_a_pump():
    c = Curve()
    plan = [(0, "DEV", 0.5)] + [(s, f"b{s}", 1.0) for s in range(1, 30)]
    tr = _trades_for(c, plan)
    budget = 30_000_000
    first = _slot_trade(tr, 100, "CREATE", 0, 25, budget, 105_000)
    late = _slot_trade(tr, 100, "CREATE", 10, 12, budget, 105_000)
    assert first > late and first > 0


def test_flat_market_round_trip_costs_fees_only():
    c = Curve()
    tr = _trades_for(c, [(0, "DEV", 0.5)])
    r = _slot_trade(tr, 100, "CREATE", 1, 5, 30_000_000, 0)
    assert r == pytest.approx(((1 / 1.0125) * (1 - 0.0125) - 1) * 100, abs=0.01)


def test_only_main_sniper_records_market_data():
    eng, conn, _ = make_sniper(min_buys=999)
    c = Curve()
    eng.on_create(c.create_event(), T0, "CREATE", 100)
    eng.on_trade(c.trade("b1", 0.5), T0 + 0.4, slot=101, signature="s1")
    eng.on_trade(c.trade("b2", 0.5), T0 + 130, slot=400, signature="s2")  # after the 120 s window
    rows = conn.execute("SELECT slot, signature FROM pump_early_trades").fetchall()
    assert [tuple(r) for r in rows] == [(101, "s1")]

    from paperbot.sniper.engine import SniperEngine
    variant = SniperEngine(conn, eng.s, eng.fx, eng.sol_price, record_market_data=False)
    c2 = Curve("V")
    variant.on_create(c2.create_event(), T0, "C2", 300)
    variant.on_trade(c2.trade("x", 0.5), T0 + 1, slot=301, signature="v1")
    variant.tick(T0 + 700)
    assert conn.execute("SELECT COUNT(*) FROM pump_early_trades WHERE mint=?", (c2.mint,)).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM pump_bars WHERE mint=?", (c2.mint,)).fetchone()[0] == 0


def test_slot_report_end_to_end():
    eng, conn, _ = make_sniper(min_buys=999)
    t = T0
    for n in range(50):
        c = Curve(f"S{n}")
        base = 1000 * (n + 1)
        eng.on_create(c.create_event(), t, f"C{n}", base)
        eng.on_trade(c.trade("CREATOR", 0.3), t, slot=base, signature=f"C{n}")
        for s in range(1, 40):
            eng.on_trade(c.trade(f"u{s}", 0.2 if n % 2 else 0.05), t + s * 0.4, slot=base + s, signature=f"x{n}-{s}")
        t += 60
    text = slot_ceiling(conn, 1.42)
    assert "SLOT-BY-SLOT" in text and "after creator" in text and "launch+25 slot" in text
    assert "A$50 per trade" in text
    assert "not enough data" in slot_ceiling(open_db(":memory:"), 1.42)
