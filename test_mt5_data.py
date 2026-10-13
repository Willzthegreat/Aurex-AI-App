import MetaTrader5 as mt5
from datetime import datetime, timezone
import pandas as pd

SYMBOL = "XAUUSDm"

if not mt5.initialize():
    print("MT5 initialization failed:", mt5.last_error())
    raise SystemExit(1)

try:
    print("======================================")
    print("EXNESS MT5 DATA FEED TEST")
    print("======================================")

    terminal = mt5.terminal_info()
    account = mt5.account_info()

    print("\nMT5 Connected:", terminal.connected)
    print("Broker:", terminal.company)
    print("Server:", account.server)

    # Select the correct Exness symbol
    if not mt5.symbol_select(SYMBOL, True):
        print(f"\nCould not select {SYMBOL}")
        print("Error:", mt5.last_error())
        raise SystemExit(1)

    print(f"\nSymbol: {SYMBOL}")
    print("Symbol selected successfully.")

    # -----------------------------------
    # CURRENT LIVE PRICE
    # -----------------------------------

    tick = mt5.symbol_info_tick(SYMBOL)

    if tick is None:
        print("\nNo live tick received.")
        print("Error:", mt5.last_error())
        raise SystemExit(1)

    tick_time = datetime.fromtimestamp(
        tick.time,
        tz=timezone.utc
    )

    print("\n======================================")
    print("LIVE PRICE")
    print("======================================")
    print("Bid:", tick.bid)
    print("Ask:", tick.ask)
    print("Spread:", round(tick.ask - tick.bid, 5))
    print("UTC Time:", tick_time)

    # -----------------------------------
    # MULTIPLE TIMEFRAMES
    # -----------------------------------

    timeframes = {
        "M1": mt5.TIMEFRAME_M1,
        "M5": mt5.TIMEFRAME_M5,
        "M15": mt5.TIMEFRAME_M15,
        "H1": mt5.TIMEFRAME_H1,
        "H4": mt5.TIMEFRAME_H4,
        "D1": mt5.TIMEFRAME_D1,
    }

    print("\n======================================")
    print("LATEST MARKET CANDLES")
    print("======================================")

    for name, timeframe in timeframes.items():

        rates = mt5.copy_rates_from_pos(
            SYMBOL,
            timeframe,
            0,
            5
        )

        if rates is None or len(rates) == 0:
            print(f"\n{name}: NO DATA")
            print("Error:", mt5.last_error())
            continue

        df = pd.DataFrame(rates)

        df["time"] = pd.to_datetime(
            df["time"],
            unit="s",
            utc=True
        )

        print(f"\n{name}")
        print("--------------------------------------")

        print(
            df[
                [
                    "time",
                    "open",
                    "high",
                    "low",
                    "close",
                    "tick_volume"
                ]
            ].to_string(index=False)
        )

finally:
    mt5.shutdown()