"""USD <-> AUD conversion.

Token prices from DEX Screener are in USD; the virtual account is in AUD.
Rates are expressed as AUD per 1 USD (e.g. 1.42 means US$1 = A$1.42).
Every fill records the rate and its source so results can be audited.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from typing import Callable

import requests

log = logging.getLogger(__name__)

MIN_SANE_RATE = 0.5
MAX_SANE_RATE = 3.0


def _check_rate(aud_per_usd: float) -> None:
    if not (isinstance(aud_per_usd, (int, float)) and math.isfinite(aud_per_usd)):
        raise ValueError(f"FX rate must be a finite number, got {aud_per_usd!r}")
    if not (MIN_SANE_RATE <= aud_per_usd <= MAX_SANE_RATE):
        raise ValueError(f"FX rate {aud_per_usd} AUD/USD outside sanity bounds [{MIN_SANE_RATE}, {MAX_SANE_RATE}]")


def usd_to_aud(amount_usd: float, aud_per_usd: float) -> float:
    _check_rate(aud_per_usd)
    return amount_usd * aud_per_usd


def aud_to_usd(amount_aud: float, aud_per_usd: float) -> float:
    _check_rate(aud_per_usd)
    return amount_aud / aud_per_usd


@dataclass(frozen=True)
class FxQuote:
    aud_per_usd: float
    source: str  # e.g. "frankfurter:2026-10-21", "static-placeholder", "stale-cache"
    fetched_at: float


class StaticFx:
    def __init__(self, aud_per_usd: float, label: str = "static-placeholder"):
        _check_rate(aud_per_usd)
        self._quote = FxQuote(aud_per_usd, label, time.time())

    def get(self) -> FxQuote:
        return self._quote


class LiveFx:
    """ECB reference rate via frankfurter.dev, cached, with static fallback.

    ECB rates update once per business day, so this is a daily reference
    rate, not a tradable quote (ASSUMPTION: adequate for A$5 positions).
    """

    URL = "https://api.frankfurter.dev/v1/latest?base=USD&symbols=AUD"

    def __init__(
        self,
        fallback_aud_per_usd: float,
        cache_seconds: float = 3600.0,
        session: requests.Session | None = None,
        timeout: float = 10.0,
        clock: Callable[[], float] = time.time,
    ):
        _check_rate(fallback_aud_per_usd)
        self.fallback = fallback_aud_per_usd
        self.cache_seconds = cache_seconds
        self.session = session or requests.Session()
        self.timeout = timeout
        self.clock = clock
        self._cached: FxQuote | None = None

    def get(self) -> FxQuote:
        now = self.clock()
        if self._cached and now - self._cached.fetched_at < self.cache_seconds:
            return self._cached
        try:
            resp = self.session.get(self.URL, timeout=self.timeout)
            resp.raise_for_status()
            data = resp.json()
            rate = float(data["rates"]["AUD"])
            _check_rate(rate)
            self._cached = FxQuote(rate, f"frankfurter:{data.get('date', '?')}", now)
            return self._cached
        except Exception as exc:  # network, JSON, bounds
            log.warning("FX fetch failed (%s); using %s", exc, "stale cache" if self._cached else "static fallback")
            if self._cached:
                return FxQuote(self._cached.aud_per_usd, "stale-cache:" + self._cached.source, self._cached.fetched_at)
            return FxQuote(self.fallback, "static-fallback-placeholder", now)


def make_fx(settings) -> StaticFx | LiveFx:
    if settings.fx_mode == "static":
        return StaticFx(settings.fx_static_aud_per_usd)
    return LiveFx(settings.fx_static_aud_per_usd, settings.fx_cache_seconds, timeout=settings.http_timeout_seconds)
