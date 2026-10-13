# Aurex MT5 Market Data Bridge

This repository contains a Windows-hosted Python service that reads market data from a running MetaTrader 5 terminal and serves it to the Aurex Floot backend.

## Important safety boundary

- This version exposes **authenticated market-data endpoints only**: health, symbol specifications, ticks, and candles.
- It does **not** expose account balances, positions, or order execution. Do not re-add these routes until per-customer MT5 account routing and authorization have been implemented and tested.
- Keep Aurex live trading disabled. A single connected MT5 terminal is one broker account; an API key does not create multi-user account isolation.
- Never expose this API key in browser code, a public GitHub repository, screenshots, or chat.
- Do not use a temporary Cloudflare Quick Tunnel for production. Use a stable, access-controlled HTTPS endpoint and a long random bridge key.

## Windows setup

Use a dedicated Windows VPS with MetaTrader 5 installed and logged into the intended **demo account**. Install Python 3.11+ and the matching MT5 Python package.

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Set secrets in the service host's environment (do not commit them to source control). The `AUREX_BRIDGE_API_KEY` value must exactly match the secret connected in Floot as `MT5_BRIDGE_API_KEY`.

```powershell
$env:AUREX_BRIDGE_API_KEY = "<set this securely in the Windows service environment>"
$env:MT5_DEFAULT_SYMBOL = "XAUUSDm"
uvicorn mt5_data_service:app --host 127.0.0.1 --port 8000
```

Bind to loopback and place a properly secured reverse proxy/tunnel in front of it. Do not open port 8000 directly to the public internet. Ensure the proxy forwards `X-Aurex-Bridge-Key` and TLS is enforced end to end.

## Endpoints

Every HTTP endpoint requires the `X-Aurex-Bridge-Key` header.

- `GET /health`
- `GET /api/tick/{symbol}`
- `GET /api/symbol/{symbol}`
- `GET /api/candles/{symbol}/{timeframe}?count=500`

Supported timeframes: `M1`, `M5`, `M15`, `M30`, `H1`, `H4`, `D1`, `W1`, `MN1`. Candle count is capped at 3000.

## Floot configuration

In the Floot project's connected secrets/resources, set:

- `MT5_BRIDGE_URL`: the HTTPS base URL of the bridge, with no trailing slash.
- `MT5_BRIDGE_API_KEY`: the same long random key as `AUREX_BRIDGE_API_KEY` on the Windows host.

Market requests are proxied through authenticated Floot server endpoints; the bridge key must never be exposed to the browser.

## Before any live trading

Implement and test a broker-session registry that maps each authenticated Floot user to a separately authorized MT5 account/terminal session; encrypt customer credentials at rest; enforce per-user access on every account, order, close, and reconciliation request; add idempotency and audit trails; test concurrent users and account crossover; then independently review the execution/risk controls. Until all of these are complete, leave `LIVE_TRADING_ENABLED` unset or set to anything other than `true`.
