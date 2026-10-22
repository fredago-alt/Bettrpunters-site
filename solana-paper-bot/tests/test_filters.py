import pytest

from conftest import T0, make_settings, make_snap
from paperbot.filters import evaluate

S = make_settings()


def reasons(**kw):
    return evaluate(make_snap(**kw), T0, S).reasons


def test_default_snapshot_passes():
    r = evaluate(make_snap(), T0, S)
    assert r.passed and r.reasons == []
    assert r.metrics["buy_sell_ratio"] == pytest.approx(3.0)


@pytest.mark.parametrize("kw,code", [
    (dict(liquidity=24_999), "liquidity_below_min"),
    (dict(liquidity=None), "liquidity_unavailable"),
    (dict(created=T0 - 30), "pair_too_new"),
    (dict(created=T0 - 31 * 60), "pair_too_old"),
    (dict(created=None), "pair_age_unavailable"),
    (dict(buys=5, sells=4), "txns_below_min"),
    (dict(buys=11, sells=10), "buy_sell_ratio_below_min"),
    (dict(buys=20, sells=0), "no_sells_observed"),
    (dict(price=None), "price_unavailable"),
    (dict(quote="WBTC"), "quote_not_allowed:WBTC"),
    (dict(change_m5=500), "price_change_m5_extreme"),
])
def test_rejections(kw, code):
    assert code in reasons(**kw)


def test_boundaries_inclusive():
    assert reasons(liquidity=25_000) == []
    assert reasons(buys=5, sells=4) == ["txns_below_min"]  # 9 txns
    assert reasons(buys=6, sells=5) == []  # 11 txns, ratio 1.2
    assert reasons(buys=12, sells=10) == []  # ratio exactly 1.2, 22 txns
    assert reasons(created=T0 - 60) == []
    assert reasons(created=T0 - 30 * 60) == []


def test_multiple_reasons_all_recorded():
    r = reasons(liquidity=100, buys=1, sells=5, quote="PUMP")
    assert {"liquidity_below_min", "txns_below_min", "buy_sell_ratio_below_min", "quote_not_allowed:PUMP"} <= set(r)
