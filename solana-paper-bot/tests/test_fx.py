import pytest
import requests

from paperbot.fx import LiveFx, StaticFx, aud_to_usd, usd_to_aud


def test_usd_to_aud_known_values():
    assert usd_to_aud(1.0, 1.5) == pytest.approx(1.5)
    assert usd_to_aud(10.0, 1.42) == pytest.approx(14.2)
    assert usd_to_aud(0.000005387, 1.4217) == pytest.approx(0.0000076587, rel=1e-4)


def test_aud_to_usd_known_values():
    assert aud_to_usd(5.0, 1.25) == pytest.approx(4.0)
    assert aud_to_usd(50.0, 1.42) == pytest.approx(35.2112676, rel=1e-9)


@pytest.mark.parametrize("amount", [0.0, 0.01, 5.0, 50.0, 12345.678])
@pytest.mark.parametrize("rate", [0.9, 1.42, 1.6])
def test_round_trip_conversion_is_lossless(amount, rate):
    assert usd_to_aud(aud_to_usd(amount, rate), rate) == pytest.approx(amount)


def test_direction_is_not_inverted():
    # US$1 must be worth MORE than A$1 when AUD is the weaker currency.
    assert usd_to_aud(1.0, 1.42) > 1.0
    assert aud_to_usd(1.0, 1.42) < 1.0


@pytest.mark.parametrize("bad", [0, -1.4, 0.1, 10.0, float("nan"), float("inf")])
def test_rejects_insane_rates(bad):
    with pytest.raises(ValueError):
        usd_to_aud(1.0, bad)
    with pytest.raises(ValueError):
        StaticFx(bad)


class _Resp:
    def __init__(self, payload, status=200):
        self._p, self.status_code = payload, status

    def raise_for_status(self):
        if self.status_code != 200:
            raise requests.HTTPError(str(self.status_code))

    def json(self):
        return self._p


class _Session:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), 0

    def get(self, url, timeout):
        self.calls += 1
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def test_live_fx_parses_and_caches():
    sess = _Session([_Resp({"date": "2026-10-21", "rates": {"AUD": 1.4217}})])
    clock = [1000.0]
    fx = LiveFx(1.42, cache_seconds=60, session=sess, clock=lambda: clock[0])
    q = fx.get()
    assert q.aud_per_usd == 1.4217 and q.source == "frankfurter:2026-10-21"
    clock[0] += 30
    assert fx.get().aud_per_usd == 1.4217
    assert sess.calls == 1  # cached


def test_live_fx_falls_back_and_labels_source():
    sess = _Session([requests.ConnectionError("down")])
    q = LiveFx(1.42, session=sess).get()
    assert q.aud_per_usd == 1.42 and "fallback" in q.source


def test_live_fx_uses_stale_cache_on_failure_and_rejects_bad_rate():
    clock = [0.0]
    sess = _Session([_Resp({"rates": {"AUD": 1.5}}), _Resp({"rates": {"AUD": 99.0}})])
    fx = LiveFx(1.42, cache_seconds=10, session=sess, clock=lambda: clock[0])
    fx.get()
    clock[0] = 100
    q = fx.get()
    assert q.aud_per_usd == 1.5 and q.source.startswith("stale-cache")
