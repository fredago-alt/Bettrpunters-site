# Sniper test variants

Each `<name>.env` file here runs as a separate paper account (run id `sniper-<name>`)
alongside the main sniper when `SNIPER_ENABLED=true`; `scripts/keepalive.sh` starts and
restarts each one. Only `SNIPER_*` settings may be set, for example:

```
# variants/sl30.env
SNIPER_STOP_LOSS_PCT=-30
```

Variants trade on the same live stream but do not record market data (only the main
sniper writes `pump_bars` / `pump_early_trades`), so they never create duplicate data.
To stop a variant: delete its file, then `pkill -f "paperbot sniper --variant <name>"`.
