import asyncio
import hmac
import os
import re
from datetime import datetime, timezone
from typing import Annotated

import MetaTrader5 as mt5
import pandas as pd
from fastapi import Depends, FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

# This bridge intentionally serves authenticated market data only.
# Do not add order execution here until each customer has a separately
# isolated MT5 terminal/account route and server-side authorization.

DEFAULT_SYMBOL = os.getenv("MT5_DEFAULT_SYMBOL", "XAUUSDm")
SYMBOL_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,32}$")

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

app = FastAPI(title="Aurex Authenticated MT5 Market Data Bridge", version="1.1.0")

# The Floot backend calls this service server-to-server. Browser CORS access
# is deliberately not enabled; never expose the bridge API key in frontend code.


def verify_bridge_key(
    x_aurex_bridge_key: Annotated[str | None, Header(alias="X-Aurex-Bridge-Key")] = None,
):
    expected = os.getenv("AUREX_BRIDGE_API_KEY", "").strip()
    if not expected:
        raise HTTPException(status_code=503, detail="Bridge authentication is not configured.")
    if not x_aurex_bridge_key or not hmac.compare_digest(x_aurex_bridge_key, expected):
        raise HTTPException(status_code=401, detail="Invalid bridge credentials.")
    return True


def validate_symbol(symbol: str) -> str:
    if not SYMBOL_PATTERN.fullmatch(symbol):
        raise HTTPException(status_code=400, detail="Invalid symbol.")
    if not mt5.symbol_select(symbol, True):
        raise HTTPException(status_code=404, detail=f"Symbol {symbol} is unavailable in the connected MT5 terminal.")
    info = mt5.symbol_info(symbol)
    if info is None:
        raise HTTPException(status_code=404, detail=f"Symbol {symbol} was not found.")
    return symbol


def connect_mt5():
    if not mt5.initialize():
        raise RuntimeError(f"MT5 initialization failed: {mt5.last_error()}")
    terminal = mt5.terminal_info()
    if terminal is None or not terminal.connected:
        raise RuntimeError("MT5 terminal is not connected.")
    validate_symbol(DEFAULT_SYMBOL)
    print("Aurex MT5 bridge connected:", terminal.company, terminal.name, DEFAULT_SYMBOL)


def get_tick(symbol: str):
    validate_symbol(symbol)
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        raise HTTPException(status_code=503, detail=f"No tick data available for {symbol}.")
    return {
        "symbol": symbol,
        "bid": tick.bid,
        "ask": tick.ask,
        "spread": round(tick.ask - tick.bid, 8),
        "last": tick.last,
        "volume": tick.volume,
        "time": datetime.fromtimestamp(tick.time, tz=timezone.utc).isoformat(),
        "time_msc": tick.time_msc,
    }


def get_candles(symbol: str, timeframe_name: str, count: int = 200):
    validate_symbol(symbol)
    timeframe = TIMEFRAMES.get(timeframe_name.upper())
    if timeframe is None:
        raise HTTPException(status_code=400, detail=f"Unsupported timeframe: {timeframe_name}")
    count = max(1, min(int(count), 3000))
    rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, count)
    if rates is None or len(rates) == 0:
        raise HTTPException(status_code=503, detail=f"No candle data available: {mt5.last_error()}")
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
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
    return {"symbol": symbol, "timeframe": timeframe_name.upper(), "count": len(candles), "candles": candles}


@app.on_event("startup")
async def startup():
    connect_mt5()


@app.on_event("shutdown")
async def shutdown():
    mt5.shutdown()


@app.get("/health")
def health(_: bool = Depends(verify_bridge_key)):
    terminal = mt5.terminal_info()
    if terminal is None:
        return {"status": "disconnected", "mt5": False}
    tick = mt5.symbol_info_tick(DEFAULT_SYMBOL)
    return {
        "status": "ok" if terminal.connected else "disconnected",
        "mt5": bool(terminal.connected),
        "broker": terminal.company,
        "symbol": DEFAULT_SYMBOL,
        "has_tick": tick is not None,
        "server_ping_ms": round(terminal.ping_last / 1000, 2),
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/api/tick/{symbol}")
def tick(symbol: str, _: bool = Depends(verify_bridge_key)):
    return get_tick(symbol)


@app.get("/api/symbol/{symbol}")
def symbol_info(symbol: str, _: bool = Depends(verify_bridge_key)):
    validate_symbol(symbol)
    info = mt5.symbol_info(symbol)
    tick = mt5.symbol_info_tick(symbol)
    if info is None:
        raise HTTPException(status_code=404, detail="Symbol specifications are unavailable.")
    return {
        "symbol": symbol,
        "bid": tick.bid if tick else None,
        "ask": tick.ask if tick else None,
        "spread": (tick.ask - tick.bid) if tick else None,
        "point": info.point,
        "digits": info.digits,
        "tick_size": info.trade_tick_size,
        "tick_value": info.trade_tick_value,
        "volume_min": info.volume_min,
        "volume_max": info.volume_max,
        "volume_step": info.volume_step,
        "trade_contract_size": info.trade_contract_size,
        "trade_mode": info.trade_mode,
    }


@app.get("/api/candles/{symbol}/{timeframe}")
def candles(symbol: str, timeframe: str, count: int = 200, _: bool = Depends(verify_bridge_key)):
    return get_candles(symbol, timeframe, count)


@app.websocket("/ws/market/{symbol}")
async def market_websocket(websocket: WebSocket, symbol: str):
    # Browser WebSocket clients cannot set arbitrary headers. Keep this route
    # for trusted non-browser clients only; the web app uses the authenticated
    # same-origin Floot HTTP proxy instead.
    expected = os.getenv("AUREX_BRIDGE_API_KEY", "").strip()
    supplied = websocket.headers.get("x-aurex-bridge-key", "")
    if not expected or not supplied or not hmac.compare_digest(supplied, expected):
        await websocket.close(code=4401, reason="Bridge authentication required")
        return
    try:
        validate_symbol(symbol)
    except HTTPException:
        await websocket.close(code=4404, reason="Symbol unavailable")
        return
    await websocket.accept()
    last_time_msc = None
    try:
        while True:
            tick = mt5.symbol_info_tick(symbol)
            if tick is not None and tick.time_msc != last_time_msc:
                last_time_msc = tick.time_msc
                await websocket.send_json({
                    "type": "tick",
                    "symbol": symbol,
                    "bid": tick.bid,
                    "ask": tick.ask,
                    "spread": round(tick.ask - tick.bid, 8),
                    "time": datetime.fromtimestamp(tick.time, tz=timezone.utc).isoformat(),
                    "time_msc": tick.time_msc,
                })
            await asyncio.sleep(0.25)
    except WebSocketDisconnect:
        pass
    except Exception:
        try:
            await websocket.close(code=1011)
        except Exception:
            pass
