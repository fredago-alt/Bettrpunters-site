import sqlite3

import pytest

from conftest import T0, make_settings, make_snap
from paperbot.db import connect, current_version, ensure_run, migrate, open_db
from paperbot.engine import PaperEngine
from paperbot.fx import StaticFx
from paperbot.store import insert_observation, record_discovery


def test_migrations_apply_once_and_are_idempotent(tmp_path):
    conn = connect(str(tmp_path / "a.db"))
    assert migrate(conn) == ["001_initial"]
    assert migrate(conn) == []
    assert current_version(conn) == 1
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"observations", "positions", "account", "order_events", "filter_decisions", "tokens"} <= tables


def test_observations_are_immutable():
    conn = open_db(":memory:")
    record_discovery(conn, "T", "solana", "test", T0)
    obs = insert_observation(conn, T0, snap=make_snap(token="T"))
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute("UPDATE observations SET price_usd = 1 WHERE id = ?", (obs.id,))
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute("DELETE FROM observations WHERE id = ?", (obs.id,))
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute("UPDATE discovery_events SET seen_at = 0")


def test_observation_timestamp_and_iso_recorded():
    conn = open_db(":memory:")
    record_discovery(conn, "T", "solana", "test", T0)
    obs = insert_observation(conn, T0 + 0.25, snap=make_snap(token="T"), aud_per_usd=1.5, fx_source="x")
    row = conn.execute("SELECT observed_at, observed_at_iso, aud_per_usd FROM observations WHERE id=?",
                       (obs.id,)).fetchone()
    assert row["observed_at"] == T0 + 0.25
    assert row["observed_at_iso"].endswith("+00:00") and ".250" in row["observed_at_iso"]
    assert row["aud_per_usd"] == 1.5


def test_missing_observation_recorded_for_vanished_token():
    conn = open_db(":memory:")
    record_discovery(conn, "T", "solana", "test", T0)
    obs = insert_observation(conn, T0 + 60, token_address="T", missing_reason="no_pair_returned")
    assert obs.status == "missing" and obs.price_usd is None and not obs.ok


def test_state_persists_across_reconnect(tmp_path):
    path = str(tmp_path / "p.db")
    s = make_settings(db_path=path)
    conn = open_db(path)
    ensure_run(conn, "live", "live", s)
    eng = PaperEngine(conn, "live", s, StaticFx(1.5))
    record_discovery(conn, "TOKEN1", "solana", "test", T0)
    eng.on_observation(insert_observation(conn, T0, snap=make_snap()))
    conn.close()

    conn2 = open_db(path)
    ensure_run(conn2, "live", "live", s)  # must not reset the balance
    eng2 = PaperEngine(conn2, "live", s, StaticFx(1.5))
    assert eng2.cash() == pytest.approx(45.0)
    assert eng2.summary().open_positions == 1
    eng2.on_observation(insert_observation(conn2, T0 + 900, snap=make_snap()))
    row = conn2.execute("SELECT status, exit_reason FROM positions").fetchone()
    assert tuple(row) == ("closed", "max_hold")


def test_changed_rules_require_new_run_id(tmp_path):
    conn = open_db(":memory:")
    ensure_run(conn, "live", "live", make_settings())
    with pytest.raises(RuntimeError, match="new PAPERBOT run id"):
        ensure_run(conn, "live", "live", make_settings(take_profit_pct=20))
    # cost assumptions are not strategy rules: allowed
    ensure_run(conn, "live", "live", make_settings(fee_pct_per_side=0.5))


def test_cash_cannot_go_negative():
    conn = open_db(":memory:")
    s = make_settings()
    ensure_run(conn, "r", "live", s)
    eng = PaperEngine(conn, "r", s, StaticFx(1.5))
    with pytest.raises(RuntimeError):
        eng._set_cash(-1.0, T0)
