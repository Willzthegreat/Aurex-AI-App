"""Configure missing connector secrets and run the managed connector locally."""
from __future__ import annotations

import os
import secrets
import subprocess
import sys
import time
import urllib.request
from pathlib import Path


def user_env(name: str) -> str:
    if os.name != "nt":
        return ""
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, _ = winreg.QueryValueEx(key, name)
            return str(value)
    except OSError:
        return ""


def persist_user_env(name: str, value: str) -> None:
    if os.name != "nt":
        return
    import winreg
    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
    except PermissionError:
        # Managed Windows environments may block registry writes. The current
        # connector process still receives the generated secret securely.
        pass


def ensure_secret(name: str, value: str) -> str:
    value = value or user_env(name) or secrets.token_urlsafe(48)
    os.environ[name] = value
    persist_user_env(name, value)
    return value


def main() -> int:
    token = ensure_secret("AUREX_CONNECTOR_SERVICE_TOKEN", os.getenv("AUREX_CONNECTOR_SERVICE_TOKEN", ""))
    ensure_secret("AUREX_WORKER_INTERNAL_KEY", os.getenv("AUREX_WORKER_INTERNAL_KEY", ""))
    encryption_key = os.getenv("AUREX_WORKER_ENCRYPTION_KEY", "") or user_env("AUREX_WORKER_ENCRYPTION_KEY") or secrets.token_hex(32)
    if len(encryption_key) != 64:
        raise SystemExit("AUREX_WORKER_ENCRYPTION_KEY must be 64 hex characters.")
    os.environ["AUREX_WORKER_ENCRYPTION_KEY"] = encryption_key
    persist_user_env("AUREX_WORKER_ENCRYPTION_KEY", encryption_key)

    terminal = os.getenv("MT5_TERMINAL_PATH", user_env("MT5_TERMINAL_PATH") or r"C:\Program Files\MetaTrader 5\terminal64.exe")
    if not Path(terminal).exists():
        raise SystemExit(f"MT5 terminal not found: {terminal}")
    os.environ["MT5_TERMINAL_PATH"] = terminal
    os.environ.setdefault("AUREX_CONNECTOR_HOST", "127.0.0.1")
    os.environ.setdefault("AUREX_CONNECTOR_PORT", "8010")
    os.environ.setdefault("AUREX_WORKER_DATA_DIR", str(Path.cwd() / "aurex-worker-data"))
    Path(os.environ["AUREX_WORKER_DATA_DIR"]).mkdir(parents=True, exist_ok=True)

    print("Starting Aurex managed MT5 connector on 127.0.0.1:8010.")
    print("Connector secrets configured without printing their values.")
    process = subprocess.Popen([sys.executable, "-m", "uvicorn", "mt5_session_worker:app", "--host", "127.0.0.1", "--port", "8010"], env=os.environ.copy())
    for _ in range(20):
        try:
            request = urllib.request.Request("http://127.0.0.1:8010/health", headers={"Authorization": f"Bearer {token}"})
            with urllib.request.urlopen(request, timeout=1) as response:
                if response.status == 200:
                    print("Authenticated connector health check passed.")
                    break
        except Exception:
            if process.poll() is not None:
                raise SystemExit("Managed connector exited during startup.")
            time.sleep(0.5)
    else:
        process.terminate()
        raise SystemExit("Managed connector did not pass its authenticated health check.")
    return process.wait()


if __name__ == "__main__":
    raise SystemExit(main())
