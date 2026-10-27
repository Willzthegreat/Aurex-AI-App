import json
import os
import urllib.error
import urllib.request
import winreg

CONNECTIONS = {
    "DEMO": ("3485c306-62c9-49dc-879f-697c55073ac0", 57763),
    "REAL": ("5b948644-f627-4843-a536-5f385a1e8abb", 57762),
}

def user_env(name):
    value = os.environ.get(name, "").strip()
    if value:
        return value
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, _ = winreg.QueryValueEx(key, name)
            return str(value).strip()
    except OSError:
        return ""

service_token = user_env("AUREX_CONNECTOR_SERVICE_TOKEN")
internal_key = user_env("AUREX_WORKER_INTERNAL_KEY")
if not service_token or not internal_key:
    raise SystemExit("Required connector credentials are not available to this diagnostic process.")

def get(url, headers=None):
    request = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(request, timeout=12) as response:
        return response.status, json.loads(response.read().decode("utf-8"))

status, payload = get("http://127.0.0.1:8010/health", {"Authorization": f"Bearer {service_token}"})
print("connector_health", status, payload.get("status"), payload.get("managed_sessions"))

for label, (connection_id, port) in CONNECTIONS.items():
    headers = {"Authorization": f"Bearer {service_token}"}
    internal_headers = {"X-Aurex-Internal-Key": internal_key}
    status, state = get(f"http://127.0.0.1:8010/v1/connections/{connection_id}", headers)
    print(label, "status", status, state.get("status"), "port", port)
    status, health = get(f"http://127.0.0.1:{port}/internal/health", internal_headers)
    print(label, "internal_health", status, health.get("status"), health.get("connected"))
    status, symbols_payload = get(f"http://127.0.0.1:8010/v1/connections/{connection_id}/symbols", headers)
    symbols = symbols_payload.get("symbols", [])
    names = [item.get("symbol") for item in symbols if isinstance(item, dict)]
    if symbols:
        print(label, "first_symbol_item", symbols[0])
    preferred = next((name for name in ("XAUUSDm", "US30m") if name in names), names[0] if names else "")
    print(label, "symbols", status, "count", len(names), "has_XAUUSDm", "XAUUSDm" in names, "has_US30m", "US30m" in names, "selected", preferred)
    if not preferred:
        continue
    status, tick = get(f"http://127.0.0.1:8010/v1/connections/{connection_id}/tick/{preferred}", headers)
    print(label, "tick", status, tick.get("symbol"), "bid_type", type(tick.get("bid")).__name__, "ask_type", type(tick.get("ask")).__name__, "time_present", bool(tick.get("time")))
    status, candles = get(f"http://127.0.0.1:8010/v1/connections/{connection_id}/candles/{preferred}/M15?count=20", headers)
    rows = candles.get("candles", [])
    print(label, "candles", status, candles.get("symbol"), candles.get("timeframe"), "count", len(rows), "first_has_ohlc", bool(rows and all(key in rows[0] for key in ("open", "high", "low", "close"))))
