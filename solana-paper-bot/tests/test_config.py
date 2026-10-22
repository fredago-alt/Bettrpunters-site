from dataclasses import fields
from pathlib import Path

import pytest
from dotenv import dotenv_values

from paperbot.config import ConfigError, Settings


def test_defaults_match_initial_experimental_settings():
    s = Settings()
    assert s.starting_cash_aud == 50.0
    assert s.max_open_positions == 1
    assert s.max_position_aud == 5.0 and s.position_pct_of_cash == 0.10
    assert s.min_liquidity_usd == 25_000
    assert (s.min_pair_age_minutes, s.max_pair_age_minutes) == (1, 30)
    assert s.min_txns_m5 == 10 and s.min_buy_sell_ratio == 1.2
    assert (s.take_profit_pct, s.stop_loss_pct, s.max_hold_minutes) == (15, -10, 15)
    assert (s.fee_pct_per_side, s.slippage_pct_per_side) == (1.0, 1.5)
    s.validate()


def test_env_parsing():
    s = Settings.from_env({"PAPERBOT_MAX_POSITION_AUD": "3.5", "PAPERBOT_MIN_TXNS_M5": "20",
                           "PAPERBOT_REJECT_ZERO_SELLS": "false", "PAPERBOT_ALLOWED_QUOTE_SYMBOLS": "sol, usdc,usdt",
                           "PAPERBOT_FX_MODE": "static"})
    assert s.max_position_aud == 3.5 and s.min_txns_m5 == 20 and s.reject_zero_sells is False
    assert s.allowed_quote_symbols == ("SOL", "USDC", "USDT")


@pytest.mark.parametrize("env", [
    {"PAPERBOT_STOP_LOSS_PCT": "10"},
    {"PAPERBOT_TAKE_PROFIT_PCT": "-1"},
    {"PAPERBOT_MIN_PAIR_AGE_MINUTES": "40"},
    {"PAPERBOT_POSITION_PCT_OF_CASH": "1.5"},
    {"PAPERBOT_ENTRY_FILL_FAILURE_PROB": "1"},
    {"PAPERBOT_FX_MODE": "magic"},
    {"PAPERBOT_FX_STATIC_AUD_PER_USD": "0.01"},
    {"PAPERBOT_PROFILES_RATE_PER_MIN": "100"},
    {"PAPERBOT_DEXSCREENER_BASE_URL": "http://insecure.example"},
    {"PAPERBOT_MIN_POSITION_AUD": "10"},
])
def test_invalid_values_rejected(env):
    with pytest.raises(ConfigError):
        Settings.from_env(env)


def test_unparseable_value_rejected():
    with pytest.raises(ConfigError, match="PAPERBOT_MIN_TXNS_M5"):
        Settings.from_env({"PAPERBOT_MIN_TXNS_M5": "ten"})


def test_all_errors_reported_together():
    with pytest.raises(ConfigError) as e:
        Settings.from_env({"PAPERBOT_STOP_LOSS_PCT": "5", "PAPERBOT_TAKE_PROFIT_PCT": "0"})
    assert "stop_loss_pct" in str(e.value) and "take_profit_pct" in str(e.value)


def test_fingerprint_tracks_rules_not_costs():
    a = Settings()
    assert a.rules_fingerprint() == a.replace(fee_pct_per_side=0.5).rules_fingerprint()
    assert a.rules_fingerprint() != a.replace(take_profit_pct=20).rules_fingerprint()


def test_every_cost_assumption_is_labelled():
    labels = Settings().assumptions()
    for key in ("Fee per side", "Slippage per side", "Latency", "Entry fill failure", "Exit fill failure", "FX"):
        assert key in labels
        assert any(tag in labels[key] for tag in ("PLACEHOLDER", "ASSUMPTION"))


def test_env_example_is_valid_and_matches_defaults():
    env = dotenv_values(Path(__file__).resolve().parents[1] / ".env.example")
    documented = {k[len("PAPERBOT_"):].lower() for k in env if k.startswith("PAPERBOT_")}
    assert documented == {f.name for f in fields(Settings)}, "every setting must be documented in .env.example"
    assert Settings.from_env(env) == Settings()


def test_run_id_setting_gives_a_fresh_account_and_keeps_history():
    from paperbot.db import ensure_run, open_db

    s = Settings.from_env({"PAPERBOT_RUN_ID": "live-2"})
    assert s.run_id == "live-2"
    conn = open_db(":memory:")
    ensure_run(conn, "live", "live", Settings())
    conn.execute("UPDATE account SET cash_aud = 3.0 WHERE run_id = 'live'")
    ensure_run(conn, s.run_id, "live", s)
    cash = dict(conn.execute("SELECT run_id, cash_aud FROM account").fetchall())
    assert cash == {"live": 3.0, "live-2": 50.0}
    for bad in ("", "has space", "x" * 50, "../evil"):
        with pytest.raises(ConfigError):
            Settings.from_env({"PAPERBOT_RUN_ID": bad} if bad else {"PAPERBOT_RUN_ID": " "})
