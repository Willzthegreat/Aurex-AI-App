import asyncio
import os
from datetime import datetime, timezone

import MetaTrader5 as mt5
import pandas as pd
from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer


# ============================================================
# CONFIGURATION
# ============================================================

SYMBOL = "XAUUSDm"
CONNECTOR_TOKEN = os.getenv("AUREX_CONNECTOR_SERVICE_TOKEN")

if not CONNECTOR_TOKEN:
    raise RuntimeError(
        "AUREX_CONNECTOR_SERVICE_TOKEN must be set; refusing to start without bearer authentication."
    )

bearer = HTTPBearer(auto_error=False)


def require_bearer(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
):
    if (
        credentials is None
        or credentials.scheme.lower() != "bearer"
        or credentials.credentials != CONNECTOR_TOKEN
    ):
        raise HTTPException(
            status_code=401,
            detail="Bearer token required",
            headers={"WWW-Authenticate": "Bearer"},
        )

TIMEFRAMES = {
    "M1": mt5.TIMEFRAME_M1,
    "M5": mt5.TIMEFRAME_M5,
    "M15": mt5.TIMEFRAME_M15,
    "M30": mt5.TIMEFRAME_M30,
    "H1": mt5.TIMEFRAME_H1,
    "H4": mt5.TIMEFRAME_H4,
    "D1": mt5.TIMEFRAME_D1,
    "W1": mt5.TIMEFRAME_W1,
    "MN1": mt5.TIMEFRAME_MN1,
}


# ============================================================
# FASTAPI
# ============================================================

app = FastAPI(
    title="Exness MT5 Market Data Service",
    version="1.0.0",
)

from fastapi.middleware.cors import CORSMiddleware

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://floot.com"],
    allow_credentials=False,
    allow_methods=["GET"],
    allow_headers=["*"],
)



# Development CORS.
# We will restrict this later when connecting production.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============================================================
# MT5 CONNECTION
# ============================================================

def connect_mt5():
    """
    Connect to the already-installed MT5 EXNESS terminal.
    """

    if not mt5.initialize():
        raise RuntimeError(
            f"MT5 initialization failed: {mt5.last_error()}"
        )

    terminal = mt5.terminal_info()

    if terminal is None:
        raise RuntimeError(
            f"MT5 terminal information unavailable: {mt5.last_error()}"
        )

    if not terminal.connected:
        raise RuntimeError("MT5 terminal is not connected.")

    # Make sure our symbol is available.
    if not mt5.symbol_select(SYMBOL, True):
        raise RuntimeError(
            f"Could not select {SYMBOL}: {mt5.last_error()}"
        )

    print("======================================")
    print("MT5 DATA SERVICE")
    print("======================================")
    print("Broker:", terminal.company)
    print("Terminal:", terminal.name)
    print("Symbol:", SYMBOL)
    print("Connected:", terminal.connected)
    print("======================================")


# ============================================================
# HELPERS
# ============================================================

def get_tick():
    tick = mt5.symbol_info_tick(SYMBOL)

    if tick is None:
        raise HTTPException(
            status_code=503,
            detail=f"No tick data available for {SYMBOL}"
        )

    return {
        "symbol": SYMBOL,
        "bid": tick.bid,
        "ask": tick.ask,
        "spread": round(tick.ask - tick.bid, 5),
        "last": tick.last,
        "volume": tick.volume,
        "time": datetime.fromtimestamp(
            tick.time,
            tz=timezone.utc
        ).isoformat(),
        "time_msc": tick.time_msc,
    }


def get_candles(timeframe_name: str, count: int = 200):

    timeframe = TIMEFRAMES.get(timeframe_name.upper())

    if timeframe is None:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported timeframe: {timeframe_name}"
        )

    count = max(1, min(count, 5000))

    rates = mt5.copy_rates_from_pos(
        SYMBOL,
        timeframe,
        0,
        count
    )

    if rates is None or len(rates) == 0:
        raise HTTPException(
            status_code=503,
            detail=f"No candle data available: {mt5.last_error()}"
        )

    df = pd.DataFrame(rates)

    df["time"] = pd.to_datetime(
        df["time"],
        unit="s",
        utc=True
    )

    candles = []

    for _, row in df.iterrows():

        candles.append({
            "time": row["time"].isoformat(),
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
            "tick_volume": int(row["tick_volume"]),
            "spread": int(row["spread"]),
            "real_volume": int(row["real_volume"]),
        })

    return {
        "symbol": SYMBOL,
        "timeframe": timeframe_name.upper(),
        "count": len(candles),
        "candles": candles,
    }


# ============================================================
# STARTUP / SHUTDOWN
# ============================================================

@app.on_event("startup")
async def startup():

    connect_mt5()

    print("MT5 service started successfully.")


@app.on_event("shutdown")
async def shutdown():

    mt5.shutdown()

    print("MT5 connection closed.")


# ============================================================
# HEALTH CHECK
# ============================================================

@app.get("/health", dependencies=[Depends(require_bearer)])
def health():

    terminal = mt5.terminal_info()

    if terminal is None:
        return {
            "status": "disconnected",
            "mt5": False,
        }

    tick = mt5.symbol_info_tick(SYMBOL)

    return {
        "status": "ok" if terminal.connected else "disconnected",
        "mt5": terminal.connected,
        "broker": terminal.company,
        "symbol": SYMBOL,
        "has_tick": tick is not None,
        "server_ping_ms": round(
            terminal.ping_last / 1000,
            2
        ),
        "checked_at": datetime.now(
            timezone.utc
        ).isoformat(),
    }


# ============================================================
# LIVE TICK
# ============================================================

@app.get("/api/tick/{symbol}", dependencies=[Depends(require_bearer)])
def tick(symbol: str):

    if symbol.upper() != SYMBOL.upper():
        raise HTTPException(
            status_code=404,
            detail=f"Only {SYMBOL} is enabled currently."
        )

    return get_tick()


# ============================================================
# HISTORICAL CANDLES
# ============================================================

@app.get("/api/candles/{symbol}/{timeframe}", dependencies=[Depends(require_bearer)])
def candles(
    symbol: str,
    timeframe: str,
    count: int = 200,
):

    if symbol.upper() != SYMBOL.upper():
        raise HTTPException(
            status_code=404,
            detail=f"Only {SYMBOL} is enabled currently."
        )

    return get_candles(
        timeframe,
        count
    )


# ============================================================
# WEBSOCKET LIVE MARKET DATA
# ============================================================

@app.websocket("/ws/market/{symbol}")
async def market_websocket(
    websocket: WebSocket,
    symbol: str,
):

    authorization = websocket.headers.get("authorization", "")
    scheme, _, token = authorization.partition(" ")
    # Browser WebSocket clients cannot set an Authorization header. Support
    # the standard subprotocol form (`bearer, <token>`) and a query-token
    # fallback for legacy clients, while keeping the endpoint fail-closed.
    protocols = [item.strip() for item in websocket.headers.get("sec-websocket-protocol", "").split(",")]
    query_token = websocket.query_params.get("access_token")
    authenticated = (
        (scheme.lower() == "bearer" and token == CONNECTOR_TOKEN)
        or CONNECTOR_TOKEN in protocols
        or query_token == CONNECTOR_TOKEN
    )
    if not authenticated:
        await websocket.close(code=1008, reason="Bearer token required")
        return

    await websocket.accept()

    if symbol.upper() != SYMBOL.upper():

        await websocket.send_json({
            "type": "error",
            "message": f"Only {SYMBOL} is enabled."
        })

        await websocket.close()
        return

    print(f"WebSocket connected: {symbol}")

    last_time_msc = None

    try:

        while True:

            tick = mt5.symbol_info_tick(SYMBOL)

            if tick is not None:

                if tick.time_msc != last_time_msc:

                    last_time_msc = tick.time_msc

                    await websocket.send_json({
                        "type": "tick",
                        "symbol": SYMBOL,
                        "bid": tick.bid,
                        "ask": tick.ask,
                        "spread": round(
                            tick.ask - tick.bid,
                            5
                        ),
                        "time": datetime.fromtimestamp(
                            tick.time,
                            tz=timezone.utc
                        ).isoformat(),
                        "time_msc": tick.time_msc,
                    })

            await asyncio.sleep(0.25)

    except WebSocketDisconnect:

        print(f"WebSocket disconnected: {symbol}")

    except Exception as error:

        print(
            f"WebSocket error for {symbol}:",
            error
        )

        try:
            await websocket.close()
        except Exception:
            pass
