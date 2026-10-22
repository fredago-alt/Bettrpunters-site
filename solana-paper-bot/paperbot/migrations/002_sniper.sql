-- 002_sniper: pump.fun sniper PAPER TRADING tables.

-- One row per token whose create event was seen (mutable status summary).
CREATE TABLE pump_tokens (
    mint            TEXT PRIMARY KEY,
    bonding_curve   TEXT NOT NULL,
    name            TEXT,
    symbol          TEXT,
    creator         TEXT,
    quote_mint      TEXT,
    mayhem          INTEGER NOT NULL,
    creator_fee_bps INTEGER,
    chain_created_at REAL,
    seen_at         REAL NOT NULL,
    signature       TEXT,
    slot            INTEGER,
    status          TEXT NOT NULL,
    rejection       TEXT
);
CREATE INDEX idx_pump_tokens_seen ON pump_tokens(seen_at);

-- Immutable per-token activity bars (only while a token is young or held).
CREATE TABLE pump_bars (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    mint            TEXT NOT NULL,
    bar_start       REAL NOT NULL,
    bar_end         REAL NOT NULL,
    n_buys          INTEGER NOT NULL,
    n_sells         INTEGER NOT NULL,
    buy_lamports    INTEGER NOT NULL,
    sell_lamports   INTEGER NOT NULL,
    unique_buyers   INTEGER NOT NULL,
    open_price_sol  REAL,
    high_price_sol  REAL,
    low_price_sol   REAL,
    close_price_sol REAL,
    virtual_quote   INTEGER,
    virtual_token   INTEGER,
    real_token      INTEGER,
    creator_sold    INTEGER NOT NULL,
    sol_usd         REAL
);
CREATE INDEX idx_pump_bars_mint ON pump_bars(mint, bar_start);

CREATE TRIGGER pump_bars_no_update BEFORE UPDATE ON pump_bars
BEGIN SELECT RAISE(ABORT, 'pump_bars are immutable'); END;
CREATE TRIGGER pump_bars_no_delete BEFORE DELETE ON pump_bars
BEGIN SELECT RAISE(ABORT, 'pump_bars are immutable'); END;

ALTER TABLE positions ADD COLUMN entry_sol_usd REAL;
ALTER TABLE positions ADD COLUMN exit_sol_usd REAL;
ALTER TABLE positions ADD COLUMN entry_network_fee_aud REAL;
ALTER TABLE positions ADD COLUMN exit_network_fee_aud REAL;
ALTER TABLE positions ADD COLUMN entry_curve_in_lamports INTEGER;
ALTER TABLE positions ADD COLUMN tokens_raw INTEGER;
