-- 001_initial: core schema for the PAPER TRADING simulator.
-- All timestamps are UTC unix seconds (REAL). Observation-type tables are
-- append-only: triggers below abort any UPDATE or DELETE.

CREATE TABLE runs (
    run_id            TEXT PRIMARY KEY,
    kind              TEXT NOT NULL CHECK (kind IN ('live', 'replay')),
    created_at        REAL NOT NULL,
    rules_fingerprint TEXT NOT NULL,
    settings_json     TEXT NOT NULL,
    notes             TEXT
);

-- Mutable per-token summary. History lives in discovery_events/observations.
CREATE TABLE tokens (
    token_address        TEXT PRIMARY KEY,
    chain_id             TEXT NOT NULL,
    symbol               TEXT,
    name                 TEXT,
    first_seen_at        REAL NOT NULL,
    first_seen_source    TEXT NOT NULL,
    last_seen_in_feed_at REAL,
    last_observed_at     REAL,
    last_status          TEXT,
    last_rejection       TEXT
);

-- Every time a token shows up in a discovery feed.
CREATE TABLE discovery_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    token_address TEXT NOT NULL,
    seen_at       REAL NOT NULL,
    source        TEXT NOT NULL,
    raw_json      TEXT
);
CREATE INDEX idx_discovery_token ON discovery_events(token_address, seen_at);

-- Immutable market observations. status='missing' means we asked for the
-- token/pair and the API returned nothing (disappeared / delisted / no pool).
CREATE TABLE observations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    token_address   TEXT NOT NULL,
    pair_address    TEXT,
    observed_at     REAL NOT NULL,
    observed_at_iso TEXT NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('ok', 'missing')),
    reason          TEXT,
    symbol          TEXT,
    dex_id          TEXT,
    quote_symbol    TEXT,
    price_usd       REAL,
    price_native    REAL,
    liquidity_usd   REAL,
    fdv_usd         REAL,
    market_cap_usd  REAL,
    volume_m5_usd   REAL,
    volume_h1_usd   REAL,
    buys_m5         INTEGER,
    sells_m5        INTEGER,
    buys_h1         INTEGER,
    sells_h1        INTEGER,
    price_change_m5 REAL,
    price_change_h1 REAL,
    pair_created_at REAL,
    aud_per_usd     REAL,
    fx_source       TEXT,
    raw_json        TEXT
);
CREATE INDEX idx_obs_time ON observations(observed_at);
CREATE INDEX idx_obs_token_time ON observations(token_address, observed_at);
CREATE INDEX idx_obs_pair_time ON observations(pair_address, observed_at);

CREATE TABLE filter_decisions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id         TEXT NOT NULL,
    observation_id INTEGER NOT NULL,  -- no FK: replay DBs reference the source DB's ids
    token_address  TEXT NOT NULL,
    decided_at     REAL NOT NULL,
    passed         INTEGER NOT NULL CHECK (passed IN (0, 1)),
    reasons_json   TEXT NOT NULL
);
CREATE INDEX idx_fd_run ON filter_decisions(run_id, decided_at);

CREATE TABLE account (
    run_id            TEXT PRIMARY KEY REFERENCES runs(run_id),
    currency          TEXT NOT NULL DEFAULT 'AUD' CHECK (currency = 'AUD'),
    starting_cash_aud REAL NOT NULL,
    cash_aud          REAL NOT NULL,
    updated_at        REAL NOT NULL
);

-- Lifecycle: pending -> open -> closed, or pending -> abandoned.
CREATE TABLE positions (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id                TEXT NOT NULL REFERENCES runs(run_id),
    token_address         TEXT NOT NULL,
    pair_address          TEXT NOT NULL,
    symbol                TEXT,
    status                TEXT NOT NULL CHECK (status IN ('pending', 'open', 'closed', 'abandoned')),
    signal_observation_id INTEGER NOT NULL,
    signal_at             REAL NOT NULL,
    signal_price_usd      REAL NOT NULL,
    abandon_reason        TEXT,
    opened_at             REAL,
    entry_observation_id  INTEGER,
    qty                   REAL,
    entry_ref_price_usd   REAL,
    entry_exec_price_usd  REAL,
    entry_aud_per_usd     REAL,
    entry_fx_source       TEXT,
    entry_notional_aud    REAL,
    entry_fee_aud         REAL,
    entry_slippage_aud    REAL,
    cost_aud              REAL,
    last_mark_at          REAL,
    last_ref_price_usd    REAL,
    last_liquidity_usd    REAL,
    last_nlv_aud          REAL,
    failed_exit_attempts  INTEGER NOT NULL DEFAULT 0,
    pending_exit_reason   TEXT,
    pending_exit_at       REAL,
    closed_at             REAL,
    exit_reason           TEXT,
    exit_observation_id   INTEGER,
    exit_ref_price_usd    REAL,
    exit_exec_price_usd   REAL,
    exit_aud_per_usd      REAL,
    exit_gross_aud        REAL,
    exit_fee_aud          REAL,
    exit_slippage_aud     REAL,
    proceeds_aud          REAL,
    pnl_aud               REAL,
    pnl_pct               REAL,
    hold_seconds          REAL
);
CREATE INDEX idx_pos_run_status ON positions(run_id, status);
CREATE INDEX idx_pos_token ON positions(run_id, token_address);

CREATE TABLE order_events (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id         TEXT NOT NULL,
    position_id    INTEGER,
    token_address  TEXT NOT NULL,
    side           TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
    event          TEXT NOT NULL CHECK (event IN ('signal', 'filled', 'failed', 'abandoned', 'written_off')),
    reason         TEXT,
    at             REAL NOT NULL,
    observation_id INTEGER,
    ref_price_usd  REAL,
    details_json   TEXT
);
CREATE INDEX idx_oe_run ON order_events(run_id, at);

CREATE TABLE equity_snapshots (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id               TEXT NOT NULL,
    at                   REAL NOT NULL,
    cash_aud             REAL NOT NULL,
    positions_value_aud  REAL NOT NULL,
    equity_aud           REAL NOT NULL,
    realised_pnl_aud     REAL NOT NULL,
    unrealised_pnl_aud   REAL NOT NULL
);
CREATE INDEX idx_eq_run ON equity_snapshots(run_id, at);

-- Append-only enforcement.
CREATE TRIGGER observations_no_update BEFORE UPDATE ON observations
BEGIN SELECT RAISE(ABORT, 'observations are immutable'); END;
CREATE TRIGGER observations_no_delete BEFORE DELETE ON observations
BEGIN SELECT RAISE(ABORT, 'observations are immutable'); END;
CREATE TRIGGER discovery_no_update BEFORE UPDATE ON discovery_events
BEGIN SELECT RAISE(ABORT, 'discovery_events are immutable'); END;
CREATE TRIGGER discovery_no_delete BEFORE DELETE ON discovery_events
BEGIN SELECT RAISE(ABORT, 'discovery_events are immutable'); END;
CREATE TRIGGER fd_no_update BEFORE UPDATE ON filter_decisions
BEGIN SELECT RAISE(ABORT, 'filter_decisions are immutable'); END;
CREATE TRIGGER oe_no_update BEFORE UPDATE ON order_events
BEGIN SELECT RAISE(ABORT, 'order_events are immutable'); END;
