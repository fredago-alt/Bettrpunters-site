"""Read-only client for the documented public DEX Screener API.

Endpoints used (https://docs.dexscreener.com/api/reference):
  GET /token-profiles/latest/v1                     (60 req/min)
  GET /token-boosts/latest/v1                       (60 req/min, optional feed)
  GET /tokens/v1/{chainId}/{addr1,...,addr30}        (300 req/min)
  GET /latest/dex/pairs/{chainId}/{pair1,...,pair30} (300 req/min)

This module only ever performs HTTP GETs for market data.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable

import requests

log = logging.getLogger(__name__)

MAX_BATCH = 30
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class ApiError(RuntimeError):
    pass


class RateLimiter:
    """Simple sliding-window limiter: at most ``per_minute`` calls in any 60s."""

    def __init__(self, per_minute: int, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        self.per_minute = per_minute
        self.clock = clock
        self.sleep = sleep
        self._calls: list[float] = []
        self._lock = threading.Lock()

    def acquire(self) -> None:
        with self._lock:
            while True:
                now = self.clock()
                self._calls = [t for t in self._calls if now - t < 60.0]
                if len(self._calls) < self.per_minute:
                    self._calls.append(now)
                    return
                self.sleep(max(0.01, 60.0 - (now - self._calls[0])))


@dataclass(frozen=True)
class PairSnapshot:
    """Normalised view of one DEX Screener pair object. Missing fields are None."""

    chain_id: str
    dex_id: str | None
    pair_address: str
    token_address: str
    symbol: str | None
    name: str | None
    quote_symbol: str | None
    price_usd: float | None
    price_native: float | None
    liquidity_usd: float | None
    fdv_usd: float | None
    market_cap_usd: float | None
    volume_m5_usd: float | None
    volume_h1_usd: float | None
    buys_m5: int | None
    sells_m5: int | None
    buys_h1: int | None
    sells_h1: int | None
    price_change_m5: float | None
    price_change_h1: float | None
    pair_created_at: float | None  # unix seconds
    raw: dict

    @classmethod
    def from_api(cls, d: dict) -> "PairSnapshot":
        txns = d.get("txns") or {}
        vol = d.get("volume") or {}
        chg = d.get("priceChange") or {}
        liq = d.get("liquidity") or {}
        base = d.get("baseToken") or {}
        quote = d.get("quoteToken") or {}
        created = _num(d.get("pairCreatedAt"))
        return cls(
            chain_id=str(d.get("chainId", "")),
            dex_id=d.get("dexId"),
            pair_address=str(d.get("pairAddress", "")),
            token_address=str(base.get("address", "")),
            symbol=base.get("symbol"),
            name=base.get("name"),
            quote_symbol=(quote.get("symbol") or "").upper() or None,
            price_usd=_num(d.get("priceUsd")),
            price_native=_num(d.get("priceNative")),
            liquidity_usd=_num(liq.get("usd")),
            fdv_usd=_num(d.get("fdv")),
            market_cap_usd=_num(d.get("marketCap")),
            volume_m5_usd=_num(vol.get("m5")),
            volume_h1_usd=_num(vol.get("h1")),
            buys_m5=_int((txns.get("m5") or {}).get("buys")),
            sells_m5=_int((txns.get("m5") or {}).get("sells")),
            buys_h1=_int((txns.get("h1") or {}).get("buys")),
            sells_h1=_int((txns.get("h1") or {}).get("sells")),
            price_change_m5=_num(chg.get("m5")),
            price_change_h1=_num(chg.get("h1")),
            pair_created_at=created / 1000.0 if created else None,
            raw=d,
        )


def _num(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and f not in (float("inf"), float("-inf")) else None


def _int(v: Any) -> int | None:
    f = _num(v)
    return int(f) if f is not None else None


def chunks(items: list[str], n: int = MAX_BATCH) -> Iterable[list[str]]:
    for i in range(0, len(items), n):
        yield items[i:i + n]


class DexScreenerClient:
    def __init__(self, base_url: str = "https://api.dexscreener.com", timeout: float = 10.0,
                 max_retries: int = 4, profiles_rate_per_min: int = 50, pairs_rate_per_min: int = 250,
                 session: requests.Session | None = None, sleep: Callable[[float], None] = time.sleep,
                 backoff_base: float = 1.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.session = session or requests.Session()
        self.session.headers.setdefault("Accept", "application/json")
        self.sleep = sleep
        self.backoff_base = backoff_base
        self.profiles_limiter = RateLimiter(profiles_rate_per_min, sleep=sleep)
        self.pairs_limiter = RateLimiter(pairs_rate_per_min, sleep=sleep)
        self.stats = {"requests": 0, "retries": 0, "rate_limited": 0, "errors": 0}

    @classmethod
    def from_settings(cls, s) -> "DexScreenerClient":
        return cls(s.dexscreener_base_url, s.http_timeout_seconds, s.http_max_retries,
                   s.profiles_rate_per_min, s.pairs_rate_per_min)

    # -- low level ----------------------------------------------------------
    def _get(self, path: str, limiter: RateLimiter) -> Any:
        url = self.base_url + path
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            limiter.acquire()
            self.stats["requests"] += 1
            try:
                resp = self.session.get(url, timeout=self.timeout)
            except (requests.ConnectionError, requests.Timeout) as exc:
                last_exc = exc
                wait = self._backoff(attempt)
                log.warning("GET %s failed (%s); retry %d in %.1fs", path, exc, attempt + 1, wait)
            else:
                if resp.status_code == 200:
                    try:
                        return resp.json()
                    except ValueError as exc:
                        self.stats["errors"] += 1
                        raise ApiError(f"Malformed JSON from {path}") from exc
                if resp.status_code not in RETRYABLE_STATUS:
                    self.stats["errors"] += 1
                    raise ApiError(f"GET {path} -> HTTP {resp.status_code}: {resp.text[:200]}")
                last_exc = ApiError(f"HTTP {resp.status_code}")
                if resp.status_code == 429:
                    self.stats["rate_limited"] += 1
                    wait = self._retry_after(resp) or max(self._backoff(attempt), 5.0)
                else:
                    wait = self._backoff(attempt)
                log.warning("GET %s -> %d; retry %d in %.1fs", path, resp.status_code, attempt + 1, wait)
            if attempt < self.max_retries:
                self.stats["retries"] += 1
                self.sleep(wait)
        self.stats["errors"] += 1
        raise ApiError(f"GET {path} failed after {self.max_retries + 1} attempts: {last_exc}")

    def _backoff(self, attempt: int) -> float:
        return self.backoff_base * (2 ** attempt) + random.uniform(0, self.backoff_base)

    @staticmethod
    def _retry_after(resp: requests.Response) -> float | None:
        v = resp.headers.get("Retry-After")
        try:
            return min(float(v), 120.0) if v is not None else None
        except ValueError:
            return None

    # -- endpoints ----------------------------------------------------------
    FEEDS = {"TOKEN-PROFILES": "/token-profiles/latest/v1", "TOKEN-BOOSTS": "/token-boosts/latest/v1"}

    def latest_feed(self, feed: str, chain_id: str = "solana") -> list[dict]:
        data = self._get(self.FEEDS[feed], self.profiles_limiter)
        if isinstance(data, dict):  # API has returned both a list and a single object historically
            data = [data]
        if not isinstance(data, list):
            raise ApiError(f"unexpected {feed} payload")
        return [p for p in data if isinstance(p, dict) and p.get("chainId") == chain_id and p.get("tokenAddress")]

    def latest_token_profiles(self, chain_id: str = "solana") -> list[dict]:
        return self.latest_feed("TOKEN-PROFILES", chain_id)

    def pairs_for_tokens(self, token_addresses: list[str], chain_id: str = "solana") -> list[PairSnapshot]:
        out: list[PairSnapshot] = []
        for batch in chunks(sorted(set(token_addresses))):
            data = self._get(f"/tokens/v1/{chain_id}/{','.join(batch)}", self.pairs_limiter)
            if not isinstance(data, list):
                raise ApiError("unexpected tokens payload")
            out.extend(PairSnapshot.from_api(d) for d in data if isinstance(d, dict))
        return [p for p in out if p.chain_id == chain_id and p.pair_address]

    def pairs_by_address(self, pair_addresses: list[str], chain_id: str = "solana") -> list[PairSnapshot]:
        out: list[PairSnapshot] = []
        for batch in chunks(sorted(set(pair_addresses))):
            data = self._get(f"/latest/dex/pairs/{chain_id}/{','.join(batch)}", self.pairs_limiter)
            pairs = (data or {}).get("pairs") if isinstance(data, dict) else None
            out.extend(PairSnapshot.from_api(d) for d in (pairs or []) if isinstance(d, dict))
        return [p for p in out if p.chain_id == chain_id and p.pair_address]


def best_pair_per_token(pairs: list[PairSnapshot]) -> dict[str, PairSnapshot]:
    """Pick the deepest-liquidity pair for each token (pairs without liquidity rank last)."""
    best: dict[str, PairSnapshot] = {}
    for p in pairs:
        cur = best.get(p.token_address)
        if cur is None or (p.liquidity_usd or -1) > (cur.liquidity_usd or -1):
            best[p.token_address] = p
    return best
