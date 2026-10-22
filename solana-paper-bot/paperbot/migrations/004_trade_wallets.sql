-- 004_trade_wallets: which wallet made each early trade (public on-chain data),
-- for testing whether early wallets' track records persist and can be copied.
ALTER TABLE pump_early_trades ADD COLUMN user TEXT;
CREATE INDEX idx_pump_early_user ON pump_early_trades(user);
