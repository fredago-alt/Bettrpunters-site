"""Sniper settings (env prefix SNIPER_). All strategy values are hypotheses to
test and all cost/latency/fill values are labelled assumptions."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass, fields
from typing import Mapping

from ..config import ConfigError, _coerce, assert_no_live_trading_env

ENV_PREFIX = "SNIPER_"


@dataclass(frozen=True)
class SniperSettings:
    # Read-only Solana RPC. The public endpoint throttles; a free Helius/QuickNode
    # key is recommended. URLs may contain an API key: keep them in .env only.
    rpc_http_url: str = "https://api.mainnet-beta.solana.com"
    rpc_ws_url: str = "wss://api.mainnet-beta.solana.com"
    run_id: str = "sniper"

    # virtual account (AUD)
    starting_cash_aud: float = 50.0
    max_open_positions: int = 1
    position_pct_of_cash: float = 0.10
    max_position_aud: float = 5.0
    min_position_aud: float = 0.50

    # entry rules (hypotheses)
    min_age_seconds: float = 3.0
    max_age_seconds: float = 90.0
    min_buys: int = 10
    min_unique_buyers: int = 8
    min_net_inflow_sol: float = 3.0
    min_buy_sell_ratio: float = 2.0
    max_creator_buy_sol: float = 2.0
    max_curve_progress_pct: float = 70.0
    reject_creator_sold: bool = True

    # exits (hypotheses; on executable net value after fees)
    take_profit_pct: float = 30.0
    stop_loss_pct: float = -15.0
    max_hold_seconds: float = 120.0
    exit_on_creator_sell: bool = True

    # execution assumptions
    latency_ms: float = 500.0
    entry_fill_failure_prob: float = 0.15
    exit_fill_failure_prob: float = 0.05
    max_entry_drift_pct: float = 15.0
    protocol_fee_bps: int = 95  # observed on mainnet Oct 2026; per-token values from events take precedence
    default_creator_fee_bps: int = 30
    network_fee_lamports: int = 5_000
    priority_fee_lamports: int = 100_000  # PLACEHOLDER: real snipers often pay far more
    random_seed: int = 42

    # data
    bar_seconds: float = 5.0
    track_window_seconds: float = 600.0
    account_refresh_seconds: float = 2.0
    sol_price_refresh_seconds: float = 30.0
    stream_idle_reconnect_seconds: float = 60.0

    STRATEGY_FIELDS = (
        "max_open_positions", "position_pct_of_cash", "max_position_aud", "min_position_aud", "min_age_seconds",
        "max_age_seconds", "min_buys", "min_unique_buyers", "min_net_inflow_sol", "min_buy_sell_ratio",
        "max_creator_buy_sol", "max_curve_progress_pct", "reject_creator_sold", "take_profit_pct",
        "stop_loss_pct", "max_hold_seconds", "exit_on_creator_sell", "max_entry_drift_pct",
    )

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "SniperSettings":
        env = os.environ if env is None else env
        assert_no_live_trading_env(env)
        kwargs: dict[str, object] = {}
        for f in fields(cls):
            key = ENV_PREFIX + f.name.upper()
            if env.get(key, "") == "":
                continue
            try:
                kwargs[f.name] = _coerce(env[key].strip(), f.default)
            except ValueError as exc:
                raise ConfigError(f"{key}={env[key]!r}: {exc}") from None
        s = cls(**kwargs)  # type: ignore[arg-type]
        s.validate()
        return s

    def validate(self) -> None:
        errs: list[str] = []

        def need(cond: bool, msg: str) -> None:
            if not cond:
                errs.append(msg)

        need(self.rpc_http_url.startswith("https://"), "rpc_http_url must be https")
        need(self.rpc_ws_url.startswith("wss://"), "rpc_ws_url must be wss")
        need(bool(self.run_id) and self.run_id != "live", "run_id must be set and differ from the DEX Screener run")
        need(self.starting_cash_aud > 0, "starting_cash_aud must be > 0")
        need(self.max_open_positions >= 1, "max_open_positions must be >= 1")
        need(0 < self.position_pct_of_cash <= 1, "position_pct_of_cash must be in (0, 1]")
        need(0 <= self.min_position_aud <= self.max_position_aud, "need 0 <= min_position_aud <= max_position_aud")
        need(0 <= self.min_age_seconds < self.max_age_seconds, "need 0 <= min_age_seconds < max_age_seconds")
        need(self.max_age_seconds <= self.track_window_seconds, "max_age_seconds must be <= track_window_seconds")
        need(self.min_buys >= 0 and self.min_unique_buyers >= 0, "min_buys/min_unique_buyers must be >= 0")
        need(self.min_buy_sell_ratio >= 0, "min_buy_sell_ratio must be >= 0")
        need(0 < self.max_curve_progress_pct <= 100, "max_curve_progress_pct must be in (0, 100]")
        need(self.take_profit_pct > 0, "take_profit_pct must be > 0")
        need(-100 < self.stop_loss_pct < 0, "stop_loss_pct must be in (-100, 0)")
        need(self.max_hold_seconds > 0, "max_hold_seconds must be > 0")
        need(self.latency_ms >= 0, "latency_ms must be >= 0")
        need(0 <= self.entry_fill_failure_prob < 1, "entry_fill_failure_prob must be in [0, 1)")
        need(0 <= self.exit_fill_failure_prob < 1, "exit_fill_failure_prob must be in [0, 1)")
        need(self.max_entry_drift_pct > 0, "max_entry_drift_pct must be > 0")
        need(0 <= self.protocol_fee_bps <= 1000, "protocol_fee_bps must be in [0, 1000]")
        need(0 <= self.default_creator_fee_bps <= 1000, "default_creator_fee_bps must be in [0, 1000]")
        need(self.network_fee_lamports >= 0 and self.priority_fee_lamports >= 0, "fees must be >= 0")
        need(self.bar_seconds >= 1, "bar_seconds must be >= 1")
        need(self.account_refresh_seconds >= 0.5, "account_refresh_seconds must be >= 0.5")
        need(self.sol_price_refresh_seconds >= 5, "sol_price_refresh_seconds must be >= 5")
        need(self.stream_idle_reconnect_seconds >= 10, "stream_idle_reconnect_seconds must be >= 10")
        if errs:
            raise ConfigError("Invalid sniper configuration:\n  - " + "\n  - ".join(errs))

    def strategy_rules(self) -> dict[str, object]:
        d = asdict(self)
        return {k: d[k] for k in self.STRATEGY_FIELDS}

    def rules_fingerprint(self) -> str:
        return hashlib.sha256(json.dumps(self.strategy_rules(), sort_keys=True).encode()).hexdigest()[:16]

    @property
    def network_fee_total(self) -> int:
        return self.network_fee_lamports + self.priority_fee_lamports

    def assumptions(self) -> dict[str, str]:
        return {
            "Trading fee": f"per-token protocol + creator fee read from on-chain events (fallback "
                           f"{self.protocol_fee_bps}+{self.default_creator_fee_bps} bps) - MEASURED where available",
            "Slippage": "exact bonding-curve price impact for our order size against the latest reserves - COMPUTED",
            "Network fee": f"{self.network_fee_lamports} + {self.priority_fee_lamports} lamports priority per tx "
                           "(PLACEHOLDER; competitive snipers pay more and use bundles)",
            "Latency": f"{self.latency_ms} ms from signal to fill against the reserves current at that time (ASSUMPTION)",
            "Entry fill failure": f"{self.entry_fill_failure_prob:.0%} random + price drift > "
                                  f"{self.max_entry_drift_pct}% abandons (ASSUMPTION)",
            "Exit fill failure": f"{self.exit_fill_failure_prob:.0%} random, retried (ASSUMPTION)",
            "Own price impact": "on-chain reserves never include our simulated buy; when valuing or selling we add "
                                "it back into the curve, and assume it did not change other traders' behaviour "
                                "(ASSUMPTION; tiny size)",
            "Graduation": "a position open when the curve completes is sold at the last curve reserves (ASSUMPTION)",
            "Token account rent": "~0.002 SOL ATA rent ignored (refundable when the account is closed)",
            "Not modelled": "mayhem-mode and non-SOL-quoted curves are skipped",
        }
