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

## Persistent multi-user MT5 connector (new)

`mt5_session_worker.py` is the managed connector service for an always-on Windows VPS. It accepts authenticated server-to-server connection requests, encrypts credentials at rest using AES-256-GCM, launches one isolated child process and portable MT5 terminal directory per customer connection, retries disconnected sessions with bounded exponential backoff, exposes connection health, and supports local credential revocation. It intentionally has no live order-execution endpoints.

### Connector host requirements

- Windows VPS with MetaTrader 5 desktop terminal installed and a valid `terminal64.exe` path.
- Python 3.11+ and packages from `requirements.txt`.
- Stable HTTPS reverse proxy in front of port 8010. Bind the service to loopback; do not expose it directly without TLS and network controls.
- Set these environment variables in the Windows service environment; do not put them in GitHub:
  - `AUREX_CONNECTOR_SERVICE_TOKEN`: long random bearer token shared with Floot as `AUREX_CONNECTOR_SERVICE_TOKEN`.
  - `AUREX_WORKER_ENCRYPTION_KEY`: separate 32-byte key encoded as 64 hex characters or base64.
  - `AUREX_WORKER_INTERNAL_KEY`: another random secret for child-process loopback calls.
  - `MT5_TERMINAL_PATH`: absolute path to `terminal64.exe`.
  - `AUREX_WORKER_DATA_DIR`: persistent local data directory with restricted Windows ACLs.
  - `AUREX_CONNECTOR_HOST=127.0.0.1` and `AUREX_CONNECTOR_PORT=8010`.
- Run `uvicorn mt5_session_worker:app --host 127.0.0.1 --port 8010` under a Windows service manager (for example, NSSM) so the supervisor restarts after host reboots.
- The service copies the installed terminal into a separate portable directory for each account. Budget disk space and memory for every concurrent customer session; monitor and cap concurrent sessions before public rollout.

### Floot connector settings

After the worker is deployed behind HTTPS, connect these server-side Floot variables through a secure resource: `AUREX_CONNECTOR_SERVICE_URL` (HTTPS base URL), `AUREX_CONNECTOR_SERVICE_TOKEN` (same bearer token), and `AUREX_CREDENTIAL_ENCRYPTION_KEY` (separate app-side 32-byte key). The worker's encryption key and internal key are configured only on the Windows host. Never commit secrets or paste them into chat.

### Connector API

- `GET /health`
- `POST /v1/connections` — create isolated MT5 session; requires numeric MT5 login, trading password and exact server name.
- `GET /v1/connections/{connection_id}` — connection health.
- `DELETE /v1/connections/{connection_id}` — revoke and stop the session.
- `GET /v1/connections/{connection_id}/tick/{symbol}`
- `GET /v1/connections/{connection_id}/candles/{symbol}/{timeframe}?count=500`

All connector endpoints require `Authorization: Bearer <AUREX_CONNECTOR_SERVICE_TOKEN>`. Child-process data endpoints are loopback-only and protected by `AUREX_WORKER_INTERNAL_KEY`.

### Aurex application API in this repository

`aurex_backend.py` provides the customer-facing FastAPI routes under `/api/mt5`. Mount it behind the existing Aurex authentication middleware so that middleware sets `request.state.aurex_user_id`; a browser-provided user ID is never accepted. It stores only connection ownership and non-secret metadata in `AUREX_CONNECTION_DB` (default `./aurex-connections.sqlite3`) and sends broker credentials directly to the managed connector, which encrypts them at rest.

Customer routes are:

- `GET /api/mt5/connections`
- `POST /api/mt5/connections`
- `GET /api/mt5/connections/{connection_id}`
- `GET /api/mt5/connections/{connection_id}/tick/{symbol}`
- `GET /api/mt5/connections/{connection_id}/candles/{symbol}/{timeframe}?count=200`
- `DELETE /api/mt5/connections/{connection_id}`

Set `AUREX_CONNECTOR_SERVICE_URL` and `AUREX_CONNECTOR_SERVICE_TOKEN` only in the Aurex server environment. No route in this module calls port `8000`.

The included customer API now owns account and session storage. `AUREX_AUTH_DB` is a persistent SQLite database containing Argon2 password hashes, opaque session-token hashes, and login throttling records; it must be outside public/static directories with a restricted Windows ACL. `AUREX_CONNECTION_DB` remains a separate ownership registry. Sessions are HttpOnly, SameSite=Lax cookies and are immediately revocable in SQLite. In production set `AUREX_AUTH_COOKIE_SECURE=true`, use HTTPS, and list the public browser origin in `AUREX_TRUSTED_ORIGINS`. No password, session token, or broker credential is logged or returned. See `aurex_auth.py`, `aurex_backend.py`, and `aurex.env.example`.

On Windows, create the data directory under a non-public path such as `C:\Aurex\data`, grant Modify access only to the dedicated Aurex service account (and administrators), and deny access to web/static identities. The same account should own the API process and its SQLite files.

The Next.js UI uses a server-side rewrite from `/api/*` to `AUREX_API_ORIGIN` (default `http://127.0.0.1:8001`). Run the customer API on loopback port 8001 and keep the managed connector on loopback port 8010; the legacy bridge may continue to use port 8000. Do not put connector tokens in Next.js public environment variables.

For a later local start, configure the environment from `aurex.env.example`, then run `uvicorn aurex_app:app --host 127.0.0.1 --port 8001` and, separately, `npm run dev`. These commands are documentation only and are not run by the offline test suite.

### Exness API adapter

The app keeps an adapter slot for an official broker API, but it does not fabricate Exness endpoints or accept Exness API secrets until account/region eligibility and the official API protocol are verified. The initial active integration path is isolated MT5 terminal sessions. API access varies by region and account eligibility; check the official Exness documentation before enabling the adapter.

### Important limits

- The worker does not submit or close live orders. Live execution remains disabled.
- The Floot connection UI and encrypted record storage can be used before the worker is deployed, but a record remains `PENDING_PROVISIONING` until the remote worker accepts it and confirms a healthy session.
- Validate Windows terminal startup, per-account login, disconnect/reconnect, concurrent customer isolation, credential revocation, resource limits, and account-crossing tests on a demo environment before production.
