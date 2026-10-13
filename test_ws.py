import asyncio
import json
import websockets


async def main():
    url = "ws://127.0.0.1:8000/ws/market/XAUUSDm"

    print(f"Connecting to {url}...")

    try:
        async with websockets.connect(url) as ws:
            print("WebSocket connected.")
            print("Waiting for Exness MT5 ticks...\n")

            for _ in range(10):
                message = await ws.recv()
                data = json.loads(message)

                print(
                    f"{data['symbol']} | "
                    f"Bid: {data['bid']} | "
                    f"Ask: {data['ask']} | "
                    f"Spread: {data['spread']} | "
                    f"Time: {data['time']}"
                )

    except Exception as e:
        print(f"WebSocket error: {e}")


if __name__ == "__main__":
    asyncio.run(main())