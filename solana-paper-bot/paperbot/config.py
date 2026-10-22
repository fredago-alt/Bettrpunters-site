"""Environment-based configuration with validation.

Every number here is an experimental hypothesis or a labelled placeholder
assumption, not a proven-profitable setting.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass, fields
from typing import Mapping

# Environment variable names that suggest someone is trying to wire up real
# trading. Their presence is a hard configuration error: this project is paper
# trading only and has no code path that could use them.
FORBIDDEN_ENV_VARS = (
    "PRIVATE_KEY",
    "SOLANA_PRIVATE_KEY",
    "WALLET_PRIVATE_KEY",
    "SEED_PHRASE",
    "MNEMONIC",
    "WALLET_KEYPAIR",
    "KEYPAIR_PATH",
    "LIVE_TRADING",
    "ENABLE_LIVE_TRADING",
    "PAPERBOT_LIVE",
)

ENV_PREFIX = "PAPERBOT_"


class ConfigError(ValueError):
    """Raised when configuration is invalid or unsafe."""


@dataclass(frozen=True)
class Settings:
    # --- storage -----------------------------------------------------------
    db_path: str = "data/paperbot.sqlite3"

    # --- virtual account (AUD) ----------------------------------------------
    starting_cash_aud: float = 50.0
    max_open_positions: int = 1
    position_pct_of_cash: float = 0.10
    max_position_aud: float = 5.0
    min_position_aud: float = 0.50

    # --- entry filters (hypotheses) -----------------------------------------
    min_liquidity_usd: float = 25_000.0
    min_pair_age_minutes: float = 1.0
    max_pair_age_minutes: float = 30.0
    min_txns_m5: int = 10
    min_buy_sell_ratio: float = 1.2
    max_price_change_m5_pct: float = 300.0
    allowed_quote_symbols: tuple[str, ...] = ("SOL", "USDC")
    reject_zero_sells: bool = True

    # --- exits (hypotheses) --------------------------------------------------
    take_profit_pct: float = 15.0
    stop_loss_pct: float = -10.0
    max_hold_minutes: float = 15.0
    # A position whose pair returns no price data for this long is written
    # off at stale_recovery_pct of cost (ASSUMPTION: 0% = assume unsellable).
    stale_writeoff_minutes: float = 60.0
    stale_recovery_pct: float = 0.0

    # --- execution cost model (PLACEHOLDER ASSUMPTIONS) ---------------------
    fee_pct_per_side: float = 1.0
    slippage_pct_per_side: float = 1.5
    # Extra slippage proportional to order size / pool liquidity (0 disables).
    slippage_impact_factor: float = 0.0
    latency_seconds: float = 3.0
    entry_fill_failure_prob: float = 0.05
    exit_fill_failure_prob: float = 0.05
    # Entry is abandoned if the fill-time reference price drifted up more
    # than this from the signal price.
    max_entry_drift_pct: float = 10.0
    random_seed: int = 42

    # --- FX ------------------------------------------------------------------
    fx_mode: str = "live"  # "live" (frankfurter.dev, cached) or "static"
    fx_static_aud_per_usd: float = 1.42  # PLACEHOLDER fallback rate
    fx_cache_seconds: float = 3600.0

    # --- scanner / API -------------------------------------------------------
    # Discovery feeds: TOKEN-PROFILES (default) and optionally TOKEN-BOOSTS.
    discovery_feeds: tuple[str, ...] = ("TOKEN-PROFILES",)
    poll_interval_seconds: float = 15.0  # discovery feed + open positions
    track_poll_interval_seconds: float = 30.0  # re-observe tokens that could still qualify
    slow_track_poll_interval_seconds: float = 600.0  # re-observe tokens too old to qualify
    # A pending entry with no usable observation within this time is abandoned.
    fill_timeout_seconds: float = 90.0
    track_window_minutes: float = 120.0
    store_raw_json: bool = False
    dexscreener_base_url: str = "https://api.dexscreener.com"
    http_timeout_seconds: float = 10.0
    http_max_retries: int = 4
    profiles_rate_per_min: int = 50  # documented limit 60/min, kept below
    pairs_rate_per_min: int = 250  # documented limit 300/min, kept below

    log_level: str = "INFO"

    # ------------------------------------------------------------------------
    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        env = os.environ if env is None else env
        assert_no_live_trading_env(env)
        kwargs: dict[str, object] = {}
        for f in fields(cls):
            key = ENV_PREFIX + f.name.upper()
            if key not in env or env[key] == "":
                continue
            raw = env[key].strip()
            try:
                kwargs[f.name] = _coerce(raw, f.default)
            except ValueError as exc:
                raise ConfigError(f"{key}={raw!r}: {exc}") from None
        settings = cls(**kwargs)  # type: ignore[arg-type]
        settings.validate()
        return settings

    def validate(self) -> None:
        errs: list[str] = []

        def need(cond: bool, msg: str) -> None:
            if not cond:
                errs.append(msg)

        need(self.starting_cash_aud > 0, "starting_cash_aud must be > 0")
        need(self.max_open_positions >= 1, "max_open_positions must be >= 1")
        need(0 < self.position_pct_of_cash <= 1, "position_pct_of_cash must be in (0, 1]")
        need(self.max_position_aud > 0, "max_position_aud must be > 0")
        need(0 <= self.min_position_aud <= self.max_position_aud, "min_position_aud must be in [0, max_position_aud]")
        need(self.min_liquidity_usd >= 0, "min_liquidity_usd must be >= 0")
        need(0 <= self.min_pair_age_minutes < self.max_pair_age_minutes, "need 0 <= min_pair_age_minutes < max_pair_age_minutes")
        need(self.min_txns_m5 >= 0, "min_txns_m5 must be >= 0")
        need(self.min_buy_sell_ratio >= 0, "min_buy_sell_ratio must be >= 0")
        need(self.max_price_change_m5_pct > 0, "max_price_change_m5_pct must be > 0")
        need(len(self.allowed_quote_symbols) > 0, "allowed_quote_symbols must not be empty")
        need(self.take_profit_pct > 0, "take_profit_pct must be > 0")
        need(-100 < self.stop_loss_pct < 0, "stop_loss_pct must be in (-100, 0)")
        need(self.max_hold_minutes > 0, "max_hold_minutes must be > 0")
        need(self.stale_writeoff_minutes > 0, "stale_writeoff_minutes must be > 0")
        need(0 <= self.stale_recovery_pct <= 100, "stale_recovery_pct must be in [0, 100]")
        need(0 <= self.fee_pct_per_side < 50, "fee_pct_per_side must be in [0, 50)")
        need(0 <= self.slippage_pct_per_side < 50, "slippage_pct_per_side must be in [0, 50)")
        need(self.slippage_impact_factor >= 0, "slippage_impact_factor must be >= 0")
        need(self.latency_seconds >= 0, "latency_seconds must be >= 0")
        need(self.fill_timeout_seconds > self.latency_seconds, "fill_timeout_seconds must be > latency_seconds")
        need(0 <= self.entry_fill_failure_prob < 1, "entry_fill_failure_prob must be in [0, 1)")
        need(0 <= self.exit_fill_failure_prob < 1, "exit_fill_failure_prob must be in [0, 1)")
        need(self.max_entry_drift_pct > 0, "max_entry_drift_pct must be > 0")
        need(self.fx_mode in ("live", "static"), "fx_mode must be 'live' or 'static'")
        need(0.5 <= self.fx_static_aud_per_usd <= 3.0, "fx_static_aud_per_usd must be in [0.5, 3.0] (sanity bound)")
        need(self.fx_cache_seconds > 0, "fx_cache_seconds must be > 0")
        need(len(self.discovery_feeds) > 0 and set(self.discovery_feeds) <= {"TOKEN-PROFILES", "TOKEN-BOOSTS"},
             "discovery_feeds must be a non-empty subset of TOKEN-PROFILES,TOKEN-BOOSTS")
        need(self.poll_interval_seconds >= 1, "poll_interval_seconds must be >= 1")
        need(self.profiles_rate_per_min * self.poll_interval_seconds / 60.0 >= len(self.discovery_feeds),
             "profiles_rate_per_min too low for the number of discovery feeds per poll")
        need(self.track_poll_interval_seconds >= self.poll_interval_seconds,
             "track_poll_interval_seconds must be >= poll_interval_seconds")
        need(self.slow_track_poll_interval_seconds >= self.track_poll_interval_seconds,
             "slow_track_poll_interval_seconds must be >= track_poll_interval_seconds")
        need(self.track_window_minutes >= self.max_pair_age_minutes, "track_window_minutes must be >= max_pair_age_minutes")
        need(self.dexscreener_base_url.startswith("https://"), "dexscreener_base_url must be https")
        need(self.http_timeout_seconds > 0, "http_timeout_seconds must be > 0")
        need(0 <= self.http_max_retries <= 10, "http_max_retries must be in [0, 10]")
        need(1 <= self.profiles_rate_per_min <= 60, "profiles_rate_per_min must be in [1, 60] (DEX Screener limit)")
        need(1 <= self.pairs_rate_per_min <= 300, "pairs_rate_per_min must be in [1, 300] (DEX Screener limit)")
        need(self.log_level.upper() in ("DEBUG", "INFO", "WARNING", "ERROR"), "log_level invalid")
        if errs:
            raise ConfigError("Invalid configuration:\n  - " + "\n  - ".join(errs))

    # Rules that define the strategy. Freezing compares this fingerprint so an
    # out-of-sample run cannot silently use retuned rules.
    STRATEGY_FIELDS = (
        "position_pct_of_cash", "max_position_aud", "min_position_aud", "max_open_positions",
        "min_liquidity_usd", "min_pair_age_minutes", "max_pair_age_minutes", "min_txns_m5",
        "min_buy_sell_ratio", "max_price_change_m5_pct", "allowed_quote_symbols", "reject_zero_sells",
        "take_profit_pct", "stop_loss_pct", "max_hold_minutes", "stale_writeoff_minutes",
        "stale_recovery_pct", "max_entry_drift_pct",
    )

    def strategy_rules(self) -> dict[str, object]:
        d = asdict(self)
        return {k: (list(d[k]) if isinstance(d[k], tuple) else d[k]) for k in self.STRATEGY_FIELDS}

    def rules_fingerprint(self) -> str:
        blob = json.dumps(self.strategy_rules(), sort_keys=True).encode()
        return hashlib.sha256(blob).hexdigest()[:16]

    def assumptions(self) -> dict[str, str]:
        """Human-readable labels for every cost/latency/fill assumption."""
        return {
            "Fee per side": f"{self.fee_pct_per_side}% of notional (PLACEHOLDER, not measured)",
            "Slippage per side": f"{self.slippage_pct_per_side}% vs reference price (PLACEHOLDER, not measured)",
            "Size-impact slippage": f"factor {self.slippage_impact_factor} x order/liquidity (ASSUMPTION)",
            "Latency": f"{self.latency_seconds}s between signal and fill price (ASSUMPTION); in replay the "
                       f"effective delay is at least the recording cadence ({self.poll_interval_seconds}s positions / "
                       f"{self.track_poll_interval_seconds}s tracked tokens)",
            "Entry fill failure": f"{self.entry_fill_failure_prob:.0%} random + drift > {self.max_entry_drift_pct}% (ASSUMPTION)",
            "Exit fill failure": f"{self.exit_fill_failure_prob:.0%} random; missing pair data = failed exit (ASSUMPTION)",
            "Stale position": f"written off after {self.stale_writeoff_minutes} min without data at {self.stale_recovery_pct}% recovery (ASSUMPTION)",
            "Reference price": "DEX Screener priceUsd (last trade, not a quote); executable price = reference +/- slippage",
            "FX": f"mode={self.fx_mode}; fallback {self.fx_static_aud_per_usd} AUD per USD (PLACEHOLDER)",
        }

    def replace(self, **changes: object) -> "Settings":
        d = asdict(self)
        d.update(changes)
        d["allowed_quote_symbols"] = tuple(d["allowed_quote_symbols"])
        d["discovery_feeds"] = tuple(d["discovery_feeds"])
        s = Settings(**d)  # type: ignore[arg-type]
        s.validate()
        return s


def assert_no_live_trading_env(env: Mapping[str, str]) -> None:
    present = [k for k in FORBIDDEN_ENV_VARS if env.get(k)]
    if present:
        raise ConfigError(
            "PAPER TRADING ONLY: refusing to start because these environment variables are set: "
            + ", ".join(present)
            + ". This project never uses wallets, keys or live trading. Unset them."
        )


def _coerce(raw: str, default: object) -> object:
    if isinstance(default, bool):
        low = raw.lower()
        if low in ("1", "true", "yes", "on"):
            return True
        if low in ("0", "false", "no", "off"):
            return False
        raise ValueError("expected a boolean")
    if isinstance(default, int):
        return int(raw)
    if isinstance(default, float):
        return float(raw)
    if isinstance(default, tuple):
        return tuple(s.strip().upper() for s in raw.split(",") if s.strip())
    return raw


def load_settings(env_file: str | None = ".env") -> Settings:
    """Load .env (if present) into the process environment, then build Settings."""
    if env_file and os.path.exists(env_file):
        from dotenv import load_dotenv

        load_dotenv(env_file, override=False)
    return Settings.from_env()

