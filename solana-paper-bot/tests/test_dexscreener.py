import pytest
import requests

from paperbot.dexscreener import ApiError, DexScreenerClient, PairSnapshot, RateLimiter, best_pair_per_token


class Resp:
    def __init__(self, status=200, payload=None, headers=None, bad_json=False):
        self.status_code, self._p, self.headers, self.bad = status, payload, headers or {}, bad_json
        self.text = "err"

    def json(self):
        if self.bad:
            raise ValueError("bad json")
        return self._p


class Session:
    def __init__(self, responses):
        self.responses, self.urls, self.headers = list(responses), [], {}

    def get(self, url, timeout):
        self.urls.append(url)
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def client(responses, **kw):
    sleeps = []
    c = DexScreenerClient(session=Session(responses), sleep=sleeps.append, backoff_base=0.01, **kw)
    return c, sleeps


PROFILE = {"chainId": "solana", "tokenAddress": "AAA"}


def test_retries_server_errors_then_succeeds():
    c, sleeps = client([Resp(500), requests.ConnectionError("x"), Resp(200, [PROFILE])])
    assert c.latest_token_profiles() == [PROFILE]
    assert c.stats["retries"] == 2 and len(sleeps) == 2


def test_honours_retry_after_on_429():
    c, sleeps = client([Resp(429, headers={"Retry-After": "7"}), Resp(200, [PROFILE])])
    c.latest_token_profiles()
    assert 7.0 in sleeps and c.stats["rate_limited"] == 1


def test_gives_up_after_max_retries():
    c, _ = client([Resp(503)] * 3, max_retries=2)
    with pytest.raises(ApiError, match="after 3 attempts"):
        c.latest_token_profiles()


def test_no_retry_on_client_error():
    c, sleeps = client([Resp(404)])
    with pytest.raises(ApiError, match="404"):
        c.latest_token_profiles()
    assert sleeps == []


def test_malformed_json_raises():
    c, _ = client([Resp(200, bad_json=True)])
    with pytest.raises(ApiError, match="Malformed"):
        c.latest_token_profiles()


def test_filters_profiles_to_solana():
    c, _ = client([Resp(200, [PROFILE, {"chainId": "base", "tokenAddress": "B"}, {"chainId": "solana"}])])
    assert c.latest_token_profiles() == [PROFILE]


def test_token_batches_of_30():
    tokens = [f"T{i:03d}" for i in range(65)]
    c, _ = client([Resp(200, [])] * 3)
    c.pairs_for_tokens(tokens)
    assert len(c.session.urls) == 3
    assert all(len(u.rsplit("/", 1)[1].split(",")) <= 30 for u in c.session.urls)


def test_pairs_by_address_parses_pairs_key():
    raw = {"chainId": "solana", "pairAddress": "P", "baseToken": {"address": "T"}, "priceUsd": "0.5"}
    c, _ = client([Resp(200, {"pairs": [raw]})])
    (p,) = c.pairs_by_address(["P"])
    assert p.price_usd == 0.5 and p.token_address == "T"


def test_pairs_by_address_handles_null_pairs():
    c, _ = client([Resp(200, {"pairs": None})])
    assert c.pairs_by_address(["P"]) == []


def test_snapshot_parsing_handles_missing_and_bad_fields():
    p = PairSnapshot.from_api({"chainId": "solana", "pairAddress": "P", "baseToken": {"address": "T"},
                               "priceUsd": "not-a-number", "txns": {"m5": {"buys": "4"}}, "pairCreatedAt": 1_700_000_000_000})
    assert p.price_usd is None and p.liquidity_usd is None
    assert p.buys_m5 == 4 and p.sells_m5 is None
    assert p.pair_created_at == 1_700_000_000.0


def test_best_pair_prefers_liquidity():
    mk = lambda pair, liq: PairSnapshot.from_api({"chainId": "solana", "pairAddress": pair, "baseToken": {"address": "T"},
                                                  "liquidity": {"usd": liq} if liq else {}})
    assert best_pair_per_token([mk("A", None), mk("B", 10), mk("C", 5)])["T"].pair_address == "B"


def test_rate_limiter_blocks_when_window_full():
    now = [0.0]
    sleeps = []

    def sleep(d):
        sleeps.append(d)
        now[0] += d

    rl = RateLimiter(3, clock=lambda: now[0], sleep=sleep)
    for _ in range(3):
        rl.acquire()
    assert sleeps == []
    rl.acquire()
    assert sleeps and sum(sleeps) >= 59.9
