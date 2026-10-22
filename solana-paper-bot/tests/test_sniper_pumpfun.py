import base64
import json
from pathlib import Path

import pytest

from paperbot.sniper.pumpfun import (BONDING_CURVE_DISC, BONDING_CURVE_SCHEMA, NATIVE_SOL_QUOTE, PUMP_PROGRAM,
                                     TRADE_DISC, TRADE_SCHEMA, CreateEvent, TradeEvent, b58decode, b58encode,
                                     curve_buy_tokens, curve_progress_pct, curve_sell_lamports, decode_curve_account,
                                     decode_event, events_from_logs, simulate_curve_buy, simulate_curve_sell)
from sniper_helpers import Curve, encode, log_lines, pk

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "pump_mainnet_logs.json").read_text())


def mainnet_trades():
    return [e for tx in FIXTURE for e in events_from_logs(tx["logs"]) if isinstance(e, TradeEvent)]


def test_fixture_is_real_mainnet_data():
    assert len(FIXTURE) >= 50 and all(tx["signature"] and tx["slot"] > 0 for tx in FIXTURE)


def test_decodes_mainnet_trade_events():
    trades = mainnet_trades()
    assert len(trades) >= 50
    std = [t for t in trades if t.is_sol_quoted and not t.mayhem]
    assert len(std) >= 20
    for t in std:
        assert t.virtual_quote > 0 and t.virtual_token > 0 and t.token_amount > 0
        assert 1e-12 < t.price_sol < 1e-3
        assert t.ix_name in {"buy", "sell", "buy_exact_sol_in", "buy_exact_quote_in", "multi_hop_swap"}


def test_curve_maths_reproduces_mainnet_amounts_exactly():
    """For every standard SOL-quoted mainnet trade, rebuild the pre-trade reserves from
    the post-trade reserves in the event and recompute the trade with our maths."""
    std = [t for t in mainnet_trades() if t.is_sol_quoted and not t.mayhem and t.quote_amount]
    assert len(std) >= 20
    for t in std:
        if t.is_buy:
            got = curve_buy_tokens(t.virtual_quote - t.quote_amount, t.virtual_token + t.token_amount, t.quote_amount)
            assert got == pytest.approx(t.token_amount, rel=1e-3)
        else:
            got = curve_sell_lamports(t.virtual_quote + t.quote_amount, t.virtual_token - t.token_amount, t.token_amount)
            assert got == pytest.approx(t.quote_amount, rel=1e-3)


def test_mayhem_curves_are_flagged():
    # They do not follow the standard maths, which is why the engine skips them.
    assert any(t.mayhem for t in mainnet_trades())


def test_mainnet_fee_rates_observed():
    std = [t for t in mainnet_trades() if t.is_sol_quoted and not t.mayhem and t.fee_bps]
    assert {t.fee_bps for t in std} == {95}
    assert {t.creator_fee_bps for t in std} <= {30, 300}


def test_events_from_other_programs_are_ignored():
    c = Curve()
    payload = c.trade("u", 0.1, raw=True)
    assert len(events_from_logs(log_lines([payload]))) == 1
    assert events_from_logs(log_lines([payload], program="SomeOtherProgram11111111111111111111111111")) == []
    # nested: data emitted by a CPI callee that is not pump.fun
    nested = [f"Program {PUMP_PROGRAM} invoke [1]", "Program Other111 invoke [2]",
              "Program data: " + base64.b64encode(payload).decode(), "Program Other111 success",
              f"Program {PUMP_PROGRAM} success"]
    assert events_from_logs(nested) == []


def test_garbage_lines_do_not_crash():
    lines = [f"Program {PUMP_PROGRAM} invoke [1]", "Program data: !!!notbase64", "Program data: AAAA",
             "Program data: " + base64.b64encode(TRADE_DISC + b"\x01\x02").decode(),
             f"Program {PUMP_PROGRAM} success"]
    assert events_from_logs(lines) == []


def test_legacy_trade_event_without_quote_fields():
    c = Curve()
    full = c.trade("u", 0.2, raw=True)
    legacy = encode(TRADE_DISC, TRADE_SCHEMA, {**_fields(full)}, truncate_after="real_token_reserves")
    ev = decode_event(legacy)
    assert ev.is_sol_quoted and ev.quote_amount == decode_event(full).quote_amount
    assert ev.fee_bps is None and not ev.mayhem


def _fields(raw):
    from paperbot.sniper.pumpfun import _decode
    return _decode(raw, TRADE_SCHEMA, required=10)


def test_create_event_roundtrip():
    c = Curve(creator_fee_bps=300)
    ev = c.create_event()
    assert isinstance(ev, CreateEvent)
    assert ev.mint == c.mint and ev.bonding_curve == c.curve and ev.symbol == "TST"
    assert ev.is_sol_quoted and not ev.mayhem and ev.creator_fee_bps == 300


def test_bonding_curve_account_decode():
    data = encode(BONDING_CURVE_DISC, BONDING_CURVE_SCHEMA, {
        "virtual_token_reserves": 900, "virtual_quote_reserves": 40, "real_token_reserves": 600,
        "real_quote_reserves": 10, "complete": True, "creator": pk("c"), "quote_mint": NATIVE_SOL_QUOTE,
        "creator_fee_bps": 30})
    acc = decode_curve_account(data + b"\0" * 50)
    assert (acc.virtual_token, acc.virtual_quote, acc.complete, acc.creator_fee_bps) == (900, 40, True, 30)
    assert decode_curve_account(b"\0" * 100) is None


def test_base58_roundtrip():
    for raw in (b"\0" * 32, bytes(range(32)), b"\0\0" + bytes(30)):
        assert b58decode(b58encode(raw)).rjust(32, b"\0") == raw
    assert b58encode(b58decode(PUMP_PROGRAM)) == PUMP_PROGRAM


def test_curve_progress():
    assert curve_progress_pct(793_100_000 * 10**6) == 0
    assert curve_progress_pct(0) == 100


@pytest.mark.parametrize("fee_bps", [0, 95, 125, 395])
@pytest.mark.parametrize("network", [0, 5_000, 105_000])
def test_curve_buy_spends_exactly_budget_and_flat_round_trip_costs_add_up(fee_bps, network):
    vq, vt = 40 * 10**9, 800_000_000 * 10**6
    budget = 25_000_000  # 0.025 SOL
    b = simulate_curve_buy(vq, vt, budget, fee_bps, network)
    assert b.curve_in + b.trade_fee + b.network_fee == budget
    # sell straight back against the post-buy reserves
    vq2, vt2 = vq + b.curve_in, vt - b.tokens_raw
    s = simulate_curve_sell(vq2, vt2, b.tokens_raw, fee_bps, network)
    loss = budget - s.proceeds
    assert loss == pytest.approx(b.trade_fee + b.network_fee + s.trade_fee + s.network_fee, abs=2)  # +-rounding


def test_slippage_attribution_grows_with_size():
    vq, vt = 30 * 10**9, 1_073_000_000 * 10**6
    small = simulate_curve_buy(vq, vt, 10_000_000, 125, 0)
    big = simulate_curve_buy(vq, vt, 5 * 10**9, 125, 0)
    assert 0 <= small.slippage / small.curve_in < big.slippage / big.curve_in


def test_buy_rejects_budget_below_network_fee():
    with pytest.raises(ValueError):
        simulate_curve_buy(10**10, 10**15, 1000, 125, 5000)
