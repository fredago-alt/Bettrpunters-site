-- 003_early_trades: every pump.fun trade in a token's first seconds, with its
-- slot, so entry/exit can be studied slot by slot ("how late is too late").
-- Append-only. Written only by the main sniper process (not by test variants).

CREATE TABLE pump_early_trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    mint            TEXT NOT NULL,
    slot            INTEGER NOT NULL,
    signature       TEXT,
    received_at     REAL NOT NULL,
    is_buy          INTEGER NOT NULL,
    by_creator      INTEGER NOT NULL,
    quote_amount    INTEGER NOT NULL,
    token_amount    INTEGER NOT NULL,
    virtual_quote   INTEGER NOT NULL,
    virtual_token   INTEGER NOT NULL,
    real_token      INTEGER,
    fee_bps         INTEGER
);
CREATE INDEX idx_pump_early_mint ON pump_early_trades(mint, slot, id);

CREATE TRIGGER pump_early_no_update BEFORE UPDATE ON pump_early_trades
BEGIN SELECT RAISE(ABORT, 'pump_early_trades are immutable'); END;
CREATE TRIGGER pump_early_no_delete BEFORE DELETE ON pump_early_trades
BEGIN SELECT RAISE(ABORT, 'pump_early_trades are immutable'); END;
