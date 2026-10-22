"""Aurex managed MT5 session supervisor.

Run this service on an always-on Windows VPS with MetaTrader 5 installed.
Each customer connection runs in a separate child process and a separate
portable terminal directory. This service intentionally exposes no order
execution routes; it is for connection lifecycle and market data only.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import ctypes
import hmac
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any
from uuid import UUID, uuid4

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from pydantic import BaseModel, Field
import uvicorn

DATA_DIR = Path(os.getenv("AUREX_WORKER_DATA_DIR", "./aurex-worker-data")).resolve()
CONNECTIONS_DIR = DATA_DIR / "connections"
TERMINALS_DIR = DATA_DIR / "terminals"
STATE_DIR = DATA_DIR / "state"
LAUNCH_DB = Path(os.getenv("AUREX_WORKER_LAUNCH_DB", str(DATA_DIR / "launches.sqlite3"))).resolve()
TERMINAL_SOURCE = Path(os.getenv("MT5_TERMINAL_PATH", r"C:\Program Files\MetaTrader 5\terminal64.exe"))
SERVICE_TOKEN = os.getenv("AUREX_CONNECTOR_SERVICE_TOKEN", "").strip()
WORKER_KEY_TEXT = os.getenv("AUREX_WORKER_ENCRYPTION_KEY", "").strip()
INTERNAL_KEY = os.getenv("AUREX_WORKER_INTERNAL_KEY", "").strip()
LOCK = RLock()
PROCESSES: dict[str, "ManagedProcess"] = {}
SUPERVISOR_TASK: asyncio.Task | None = None
ACTIVE_LAUNCH_STATES = {"CREATED", "ASSIGNED", "RUNNING", "READY"}

for folder in (CONNECTIONS_DIR, TERMINALS_DIR, STATE_DIR):
    folder.mkdir(parents=True, exist_ok=True)


class _JobBasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("per_process_user_time", ctypes.c_longlong),
        ("per_job_user_time", ctypes.c_longlong),
        ("limit_flags", ctypes.c_uint32),
        ("minimum_working_set_size", ctypes.c_size_t),
        ("maximum_working_set_size", ctypes.c_size_t),
        ("active_process_limit", ctypes.c_uint32),
        ("affinity", ctypes.c_size_t),
        ("priority_class", ctypes.c_uint32),
        ("scheduling_class", ctypes.c_uint32),
    ]


class _JobIoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in (
        "read_operation_count", "write_operation_count", "other_operation_count",
        "read_transfer_count", "write_transfer_count", "other_transfer_count")]


class _JobExtendedLimitInformation(ctypes.Structure):
    _fields_ = [("basic_limit_information", _JobBasicLimitInformation), ("io_info", _JobIoCounters)]


class _JobBasicAccountingInformation(ctypes.Structure):
    _fields_ = [
        ("total_user_time", ctypes.c_longlong),
        ("total_kernel_time", ctypes.c_longlong),
        ("this_period_total_user_time", ctypes.c_longlong),
        ("this_period_total_kernel_time", ctypes.c_longlong),
        ("total_processes", ctypes.c_uint32),
        ("active_processes", ctypes.c_uint32),
        ("total_terminated_processes", ctypes.c_uint32),
    ]


class _FileTime(ctypes.Structure):
    _fields_ = [("low", ctypes.c_uint32), ("high", ctypes.c_uint32)]


class LaunchStore:
    """Transactional launch authority separate from customer JSON records."""

    def __init__(self, path: Path):
        self.path = path
        if str(path) != ":memory:":
            path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS launches (
                connection_id TEXT PRIMARY KEY,
                launch_id TEXT NOT NULL,
                state TEXT NOT NULL,
                pid INTEGER,
                creation_time_100ns INTEGER,
                termination_verified INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            )""")

    def _db(self) -> sqlite3.Connection:
        db = sqlite3.connect(str(self.path), timeout=5, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=5000")
        if str(self.path) != ":memory:":
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
        db.execute("""CREATE TABLE IF NOT EXISTS launches (
            connection_id TEXT PRIMARY KEY, launch_id TEXT NOT NULL,
            state TEXT NOT NULL, pid INTEGER,
            creation_time_100ns INTEGER,
            termination_verified INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL
        )""")
        return db

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row else None

    def get(self, connection_id: str) -> dict[str, Any] | None:
        with self._db() as db:
            return self._row(db.execute("SELECT * FROM launches WHERE connection_id=?", (connection_id,)).fetchone())

    def begin(self, connection_id: str, launch_id: str) -> bool:
        now_value = datetime.now(timezone.utc).isoformat()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT state FROM launches WHERE connection_id=?", (connection_id,)).fetchone()
            if current and current["state"] in ACTIVE_LAUNCH_STATES | {"RECOVERY_REQUIRED", "REVOKED"}:
                db.execute("ROLLBACK")
                return False
            db.execute("""INSERT INTO launches
                (connection_id,launch_id,state,pid,creation_time_100ns,termination_verified,updated_at)
                VALUES (?,?,?,NULL,NULL,0,?)
                ON CONFLICT(connection_id) DO UPDATE SET launch_id=excluded.launch_id,
                state=excluded.state,pid=NULL,creation_time_100ns=NULL,
                termination_verified=0,updated_at=excluded.updated_at""",
                (connection_id, launch_id, "CREATED", now_value))
            db.execute("COMMIT")
        return True

    def update(self, connection_id: str, launch_id: str, state: str,
               identity: dict[str, Any] | None = None,
               termination_verified: bool | None = None) -> None:
        now_value = datetime.now(timezone.utc).isoformat()
        pid = identity.get("pid") if identity else None
        creation = identity.get("creation_time_100ns") if identity else None
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT launch_id FROM launches WHERE connection_id=?", (connection_id,)).fetchone()
            if not current or current["launch_id"] != launch_id:
                db.execute("ROLLBACK")
                raise RuntimeError("Launch identity no longer owns this connection.")
            effective_state = state
            if current and current["launch_id"] == launch_id:
                existing_state = db.execute("SELECT state FROM launches WHERE connection_id=?", (connection_id,)).fetchone()["state"]
                if existing_state == "REVOKED" and state != "REVOKED":
                    # Revocation is monotonic. Cleanup may record verified termination,
                    # but it must never make the connection startable again.
                    effective_state = "REVOKED"
            if termination_verified is None:
                db.execute("""UPDATE launches SET state=?, pid=COALESCE(?,pid),
                    creation_time_100ns=COALESCE(?,creation_time_100ns), updated_at=?
                    WHERE connection_id=? AND launch_id=?""",
                    (effective_state, pid, creation, now_value, connection_id, launch_id))
            else:
                db.execute("""UPDATE launches SET state=?, pid=COALESCE(?,pid),
                    creation_time_100ns=COALESCE(?,creation_time_100ns),
                    termination_verified=?, updated_at=?
                    WHERE connection_id=? AND launch_id=?""",
                    (effective_state, pid, creation, int(termination_verified), now_value, connection_id, launch_id))
            db.execute("COMMIT")

    def revoke(self, connection_id: str, launch_id: str = "") -> None:
        now_value = datetime.now(timezone.utc).isoformat()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT launch_id FROM launches WHERE connection_id=?", (connection_id,)).fetchone()
            if current:
                db.execute("UPDATE launches SET state='REVOKED', updated_at=? WHERE connection_id=?", (now_value, connection_id))
            else:
                db.execute("INSERT INTO launches (connection_id,launch_id,state,updated_at) VALUES (?,?,?,?)",
                           (connection_id, launch_id, "REVOKED", now_value))
            db.execute("COMMIT")


LAUNCH_STORE = LaunchStore(LAUNCH_DB)


class WindowsJob:
    """Own a worker process and its descendants without killing unrelated processes."""

    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
    _JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION = 1
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000

    def __init__(self, handle: int):
        self.handle = ctypes.c_void_p(handle)
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._configure_api()

    def _configure_api(self) -> None:
        handle = ctypes.c_void_p
        bool_type = ctypes.c_int
        dword = ctypes.c_uint32
        ptr = ctypes.c_void_p
        self._kernel32.SetInformationJobObject.argtypes = [handle, dword, ptr, dword]
        self._kernel32.SetInformationJobObject.restype = bool_type
        self._kernel32.AssignProcessToJobObject.argtypes = [handle, handle]
        self._kernel32.AssignProcessToJobObject.restype = bool_type
        self._kernel32.TerminateJobObject.argtypes = [handle, dword]
        self._kernel32.TerminateJobObject.restype = bool_type
        self._kernel32.QueryInformationJobObject.argtypes = [handle, dword, ptr, dword, ctypes.POINTER(dword)]
        self._kernel32.QueryInformationJobObject.restype = bool_type
        self._kernel32.CloseHandle.argtypes = [handle]
        self._kernel32.CloseHandle.restype = bool_type

    @classmethod
    def create(cls) -> "WindowsJob":
        if os.name != "nt":
            raise RuntimeError("Windows job objects are only available on Windows.")
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        kernel32.CreateJobObjectW.restype = ctypes.c_void_p
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        raw_handle = int(handle.value) if isinstance(handle, ctypes.c_void_p) else int(handle)
        try:
            job = cls(raw_handle)
        except Exception:
            kernel32.CloseHandle(ctypes.c_void_p(raw_handle))
            raise
        limits = _JobExtendedLimitInformation()
        limits.basic_limit_information.limit_flags = cls._JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not job._kernel32.SetInformationJobObject(
            job.handle, cls._JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(limits), ctypes.sizeof(limits)
        ):
            job.close()
            raise ctypes.WinError(ctypes.get_last_error())
        return job

    def assign(self, process_handle: int) -> None:
        if not self._kernel32.AssignProcessToJobObject(self.handle, ctypes.c_void_p(int(process_handle))):
            raise ctypes.WinError(ctypes.get_last_error())

    def terminate(self) -> None:
        if not self._kernel32.TerminateJobObject(self.handle, 1):
            raise ctypes.WinError(ctypes.get_last_error())

    def is_empty(self) -> bool:
        info = _JobBasicAccountingInformation()
        ok = self._kernel32.QueryInformationJobObject(
            self.handle, self._JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION,
            ctypes.byref(info), ctypes.sizeof(info), None
        )
        if not ok:
            raise ctypes.WinError(ctypes.get_last_error())
        return info.active_processes == 0

    def close(self) -> bool:
        if not self.handle:
            return True
        if not self._kernel32.CloseHandle(self.handle):
            return False
        self.handle = ctypes.c_void_p()
        return True


def resume_suspended_process(process_handle: int) -> None:
    """Resume a CREATE_SUSPENDED process after Job Object assignment."""
    if os.name != "nt":
        return
    ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
    ntdll.NtResumeProcess.argtypes = [ctypes.c_void_p]
    ntdll.NtResumeProcess.restype = ctypes.c_long
    status = ntdll.NtResumeProcess(ctypes.c_void_p(int(process_handle)))
    if status < 0:
        raise OSError(f"NtResumeProcess failed with NTSTATUS 0x{status & 0xffffffff:08x}")


class ManagedProcess:
    """Process plus optional Windows Job Object, with idempotent verified stopping."""

    def __init__(self, process: Any, job: Any | None = None, identity: dict[str, Any] | None = None,
                 launch_id: str = ""):
        self.process = process
        self.job = job
        self.identity = identity or {"pid": int(process.pid)}
        self.launch_id = launch_id
        self.stopping = False
        self._stop_lock = threading.Lock()

    def poll(self) -> Any:
        return self.process.poll()

    def is_stopped(self) -> bool:
        process_stopped = self.process.poll() is not None
        if not process_stopped:
            return False
        return self.job is None or bool(self.job.is_empty())

    def stop(self, timeout: float = 8.0) -> bool:
        with self._stop_lock:
            self.stopping = True
            stopped = False
            closed = True
            try:
                if self.job is not None and not self.is_stopped():
                    self.job.terminate()
                elif self.process.poll() is None:
                    self.process.terminate()
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline and not self.is_stopped():
                    time.sleep(0.05)
                if not self.is_stopped():
                    # A Job Object should terminate the full tree. The direct
                    # kill is only a fallback for the worker itself; success
                    # still requires the Job Object to report no descendants.
                    self.process.kill()
                    with contextlib.suppress(Exception):
                        self.process.wait(timeout=max(0.1, deadline - time.monotonic()))
                stopped = self.is_stopped()
            except Exception:
                stopped = False
            finally:
                # Keep a non-empty Job Object open so a later revocation retry
                # can verify/terminate lingering descendants. Startup aborts
                # use _abort_unregistered_process and always close their job.
                if self.job is not None and stopped:
                    try:
                        closed = bool(self.job.close())
                    except Exception:
                        closed = False
            return stopped and closed


def cleanup_connection_files(connection_id: str, *, termination_verified: bool = False) -> bool:
    """Remove only this connection's terminal/temp files after process exit."""
    if not termination_verified:
        return False
    terminal_dir = TERMINALS_DIR / connection_id
    paths = (file_for(connection_id).with_suffix(".tmp"), STATE_DIR / f"{connection_id}.tmp")
    try:
        for path in paths:
            if path.exists():
                path.unlink()
        if terminal_dir.exists():
            shutil.rmtree(terminal_dir)
        return not terminal_dir.exists() and all(not path.exists() for path in paths)
    except OSError:
        return False


def process_is_running(pid: int) -> bool:
    """Return True when a persisted PID is alive or cannot be safely inspected."""
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
    kernel32.GetExitCodeProcess.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int
    handle = kernel32.OpenProcess(0x1000, 0, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        # No handle is not proof of exit: access, policy, and transient API
        # errors must fail closed for cleanup decisions.
        return True
    try:
        exit_code = ctypes.c_uint32()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return True
        running = exit_code.value == 259  # STILL_ACTIVE
    finally:
        if not kernel32.CloseHandle(handle):
            return True
    return running


def process_identity_from_handle(process_handle: int, pid: int) -> dict[str, Any]:
    """Return a PID plus Windows creation time, never just a PID."""
    identity = {"pid": int(pid)}
    if os.name != "nt":
        return identity
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetProcessTimes.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(_FileTime), ctypes.POINTER(_FileTime),
        ctypes.POINTER(_FileTime), ctypes.POINTER(_FileTime),
    ]
    kernel32.GetProcessTimes.restype = ctypes.c_int
    creation = _FileTime()
    exit_time = _FileTime()
    kernel_time = _FileTime()
    user_time = _FileTime()
    if not kernel32.GetProcessTimes(
        ctypes.c_void_p(int(process_handle)), ctypes.byref(creation),
        ctypes.byref(exit_time), ctypes.byref(kernel_time), ctypes.byref(user_time)
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    identity["creation_time_100ns"] = (int(creation.high) << 32) | int(creation.low)
    return identity


def process_identity_matches(identity: dict[str, Any]) -> bool | None:
    """Return False only when the recorded process is definitely not the same."""
    try:
        pid = int(identity["pid"])
        creation_time = int(identity["creation_time_100ns"])
    except (KeyError, TypeError, ValueError):
        return None
    if pid <= 0:
        return False
    if os.name != "nt":
        return None
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.GetProcessTimes.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(_FileTime), ctypes.POINTER(_FileTime),
        ctypes.POINTER(_FileTime), ctypes.POINTER(_FileTime),
    ]
    kernel32.GetProcessTimes.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int
    handle = kernel32.OpenProcess(0x1000, 0, pid)
    if not handle:
        return False if ctypes.get_last_error() == 87 else None
    try:
        current = _FileTime()
        unused = [_FileTime(), _FileTime(), _FileTime()]
        if not kernel32.GetProcessTimes(ctypes.c_void_p(handle), ctypes.byref(current), *[ctypes.byref(item) for item in unused]):
            return None
        observed = (int(current.high) << 32) | int(current.low)
        return observed == creation_time
    finally:
        if not kernel32.CloseHandle(ctypes.c_void_p(handle)):
            return None


def _persist_launch_state(connection_id: str, launch_id: str, launch_state: str,
                          identity: dict[str, Any] | None = None,
                          termination_verified: bool | None = None) -> None:
    """Update the single transactional launch authority, then UI diagnostics."""
    LAUNCH_STORE.update(connection_id, launch_id, launch_state, identity, termination_verified)
    patch: dict[str, Any] = {"launch_id": launch_id}
    if identity is not None:
        patch["pid"] = identity["pid"]
    if termination_verified is not None:
        patch["termination_verified"] = termination_verified
    write_state(connection_id, **patch)


def _persist_termination_verified(connection_id: str, managed: ManagedProcess) -> None:
    launch_id = managed.launch_id
    current = LAUNCH_STORE.get(connection_id)
    if not launch_id:
        if current is None:
            launch_id = str(uuid4())
            if not LAUNCH_STORE.begin(connection_id, launch_id):
                current = LAUNCH_STORE.get(connection_id)
        if current is not None:
            launch_id = current["launch_id"]
        managed.launch_id = launch_id
    _persist_launch_state(connection_id, launch_id, "STOPPED", managed.identity, True)

def now() -> str:
    return datetime.now(timezone.utc).isoformat()

def key_bytes() -> bytes:
    if not WORKER_KEY_TEXT:
        raise RuntimeError("AUREX_WORKER_ENCRYPTION_KEY is required.")
    if len(WORKER_KEY_TEXT) == 64:
        try:
            key = bytes.fromhex(WORKER_KEY_TEXT)
            if len(key) == 32:
                return key
        except ValueError:
            pass
    try:
        key = base64.b64decode(WORKER_KEY_TEXT, validate=True)
    except Exception as exc:
        raise RuntimeError("Worker encryption key must be 32 bytes encoded as 64 hex characters or base64.") from exc
    if len(key) != 32:
        raise RuntimeError("Worker encryption key must decode to exactly 32 bytes.")
    return key

def encrypt_json(value: dict[str, Any]) -> str:
    nonce = os.urandom(12)
    ciphertext = AESGCM(key_bytes()).encrypt(nonce, json.dumps(value).encode("utf-8"), b"aurex-mt5-worker-v1")
    return "v1." + base64.b64encode(nonce).decode() + "." + base64.b64encode(ciphertext).decode()

def decrypt_json(envelope: str) -> dict[str, Any]:
    try:
        version, nonce, ciphertext = envelope.split(".", 2)
        if version != "v1":
            raise ValueError("Unknown credential envelope version")
        raw = AESGCM(key_bytes()).decrypt(base64.b64decode(nonce), base64.b64decode(ciphertext), b"aurex-mt5-worker-v1")
        result = json.loads(raw.decode("utf-8"))
        if not isinstance(result, dict):
            raise ValueError("Credential payload must be an object")
        return result
    except Exception as exc:
        raise RuntimeError("Could not decrypt this broker credential record.") from exc

def file_for(connection_id: str) -> Path:
    try:
        normalized = str(UUID(connection_id))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid connection ID.") from exc
    return CONNECTIONS_DIR / f"{normalized}.json"

def read_record(connection_id: str) -> dict[str, Any]:
    path = file_for(connection_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Broker connection not found.")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("Invalid record")
        return data
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Broker connection record could not be read.") from exc

def write_record(record: dict[str, Any]) -> None:
    path = file_for(str(record["connection_id"]))
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(record, separators=(",", ":")), encoding="utf-8")
    os.replace(temp, path)

def read_state(connection_id: str) -> dict[str, Any]:
    path = STATE_DIR / f"{UUID(connection_id)}.json"
    if not path.exists():
        return {"connection_id": connection_id, "status": "CONNECTING", "message": "Worker is starting.", "last_health_at": now()}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {"connection_id": connection_id, "status": "ERROR", "message": "Worker state file could not be read.", "last_health_at": now()}

def write_state(connection_id: str, **patch: Any) -> dict[str, Any]:
    with LOCK:
        current = read_state(connection_id)
        current.update(patch)
        current["connection_id"] = connection_id
        current["last_health_at"] = now()
        path = STATE_DIR / f"{UUID(connection_id)}.json"
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(current, separators=(",", ":")), encoding="utf-8")
        os.replace(temp, path)
        return current

def pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def ensure_terminal_copy(connection_id: str) -> Path:
    """Create only a new per-connection terminal directory; never delete here."""
    terminal_target = TERMINALS_DIR / connection_id
    terminal_exe = terminal_target / TERMINAL_SOURCE.name
    if not terminal_exe.exists():
        if terminal_target.exists():
            raise RuntimeError("Terminal directory exists without its executable; verified cleanup is required before reuse.")
        shutil.copytree(TERMINAL_SOURCE.parent, terminal_target, ignore=shutil.ignore_patterns("Logs", "Tester", "Bases"))
    if not terminal_exe.exists():
        raise RuntimeError("Copied terminal executable was not found.")
    return terminal_exe


def _abort_unregistered_process(process: Any, job: Any | None, assigned: bool) -> bool:
    """Stop a launch that was never registered; never remove its terminal files."""
    exited = False
    job_empty = job is None
    closed = True
    try:
        if assigned and job is not None:
            with contextlib.suppress(Exception):
                job.terminate()
        elif process is not None and process.poll() is None:
            with contextlib.suppress(Exception):
                process.terminate()
        with contextlib.suppress(Exception):
            process.wait(timeout=4)
        if process is not None and process.poll() is None:
            with contextlib.suppress(Exception):
                process.kill()
            with contextlib.suppress(Exception):
                process.wait(timeout=4)
        exited = process is not None and process.poll() is not None
        if job is not None:
            with contextlib.suppress(Exception):
                job_empty = bool(job.is_empty())
    finally:
        if job is not None:
            try:
                closed = bool(job.close())
            except Exception:
                closed = False
    return exited and job_empty and closed


def launch_worker_process(connection_id: str, env: dict[str, str], launch_id: str | None = None) -> ManagedProcess:
    """Create suspended, assign to its Job Object, then resume the worker."""
    job = None
    process = None
    assigned = False
    persist_durable_state = launch_id is not None
    launch_id = launch_id or str(uuid4())
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    if os.name == "nt":
        creationflags |= getattr(subprocess, "CREATE_SUSPENDED", 0x00000004)
        job = WindowsJob.create()
    try:
        process = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--session-worker", connection_id],
            env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, creationflags=creationflags,
            close_fds=(os.name != "nt"),
        )
        if job is not None:
            job.assign(process._handle)
            assigned = True
            identity = process_identity_from_handle(process._handle, process.pid)
            if persist_durable_state:
                _persist_launch_state(connection_id, launch_id, "ASSIGNED", identity)
            resume_suspended_process(process._handle)
            if persist_durable_state:
                _persist_launch_state(connection_id, launch_id, "RUNNING", identity)
        else:
            identity = process_identity_from_handle(getattr(process, "_handle", 0), process.pid)
            if persist_durable_state:
                _persist_launch_state(connection_id, launch_id, "RUNNING", identity)
        return ManagedProcess(process, job, identity, launch_id if persist_durable_state else "")
    except Exception as exc:
        if process is None and job is not None:
            try:
                verified = bool(job.close())
            except Exception:
                verified = False
        else:
            verified = _abort_unregistered_process(process, job, assigned) if process is not None else False
        if not verified:
            raise RuntimeError("Worker launch failed and process cleanup could not be verified.") from exc
        raise RuntimeError("Worker launch failed before the session was registered.") from exc

def start_worker(connection_id: str) -> None:
    with LOCK:
        existing = PROCESSES.get(connection_id)
        if existing:
            if existing.stopping:
                return
            if not existing.is_stopped():
                return
            if not existing.stop():
                return
            _persist_termination_verified(connection_id, existing)
            PROCESSES.pop(connection_id, None)
            if not cleanup_connection_files(connection_id, termination_verified=True):
                return
        record = read_record(connection_id)
        if record.get("revoked"):
            return
        launch = LAUNCH_STORE.get(connection_id)
        if launch and launch["state"] == "REVOKED":
            return
        if launch and launch["state"] in ACTIVE_LAUNCH_STATES:
            if launch["pid"] is None or launch["creation_time_100ns"] is None:
                LAUNCH_STORE.update(connection_id, launch["launch_id"], "RECOVERY_REQUIRED")
                return
            identity = {"pid": launch["pid"], "creation_time_100ns": launch["creation_time_100ns"]}
            status = process_identity_matches(identity)
            if status is not False:
                if status is None:
                    LAUNCH_STORE.update(connection_id, launch["launch_id"], "RECOVERY_REQUIRED")
                return
            # A stale identity cannot authorize directory reuse unless the
            # prior process tree was already verified empty.
            if not launch["termination_verified"] and (TERMINALS_DIR / connection_id).exists():
                LAUNCH_STORE.update(connection_id, launch["launch_id"], "RECOVERY_REQUIRED")
                return
            LAUNCH_STORE.update(connection_id, launch["launch_id"], "STALE", termination_verified=True)
        port = int(record.get("local_port") or pick_free_port())
        record["local_port"] = port
        launch_id = str(UUID(str(uuid4())))
        write_record(record)
        if not LAUNCH_STORE.begin(connection_id, launch_id):
            return
        write_state(connection_id, launch_id=launch_id, termination_verified=False)
        env = os.environ.copy()
        env["AUREX_SESSION_CONNECTION_ID"] = connection_id
        env["AUREX_SESSION_LOCAL_PORT"] = str(port)
        managed = launch_worker_process(connection_id, env, launch_id)
        managed.launch_id = launch_id
        process = managed.process
        PROCESSES[connection_id] = managed
        _persist_launch_state(connection_id, launch_id, "READY", managed.identity)
        write_state(connection_id, status="CONNECTING", message="Starting isolated MT5 terminal session.", local_port=port, pid=process.pid)

def stop_worker(connection_id: str) -> bool:
    with LOCK:
        managed = PROCESSES.get(connection_id)
        if managed is not None:
            managed.stopping = True
    if managed is None:
        record = read_record(connection_id)
        launch = LAUNCH_STORE.get(connection_id)
        if launch is None:
            if (TERMINALS_DIR / connection_id).exists():
                return False
            return cleanup_connection_files(connection_id, termination_verified=True)
        if launch["state"] == "STOPPED" and launch["termination_verified"]:
            return cleanup_connection_files(connection_id, termination_verified=True)
        if launch["state"] == "REVOKED" and launch["termination_verified"]:
            return cleanup_connection_files(connection_id, termination_verified=True)
        if launch["state"] == "REVOKED" and launch["pid"] is None and not (TERMINALS_DIR / connection_id).exists():
            LAUNCH_STORE.update(connection_id, launch["launch_id"], "STOPPED", termination_verified=True)
            return cleanup_connection_files(connection_id, termination_verified=True)
        if launch["pid"] is None or launch["creation_time_100ns"] is None:
            LAUNCH_STORE.update(connection_id, launch["launch_id"], "RECOVERY_REQUIRED")
            return False
        identity = {"pid": launch["pid"], "creation_time_100ns": launch["creation_time_100ns"]}
        identity_status = process_identity_matches(identity)
        if identity_status is not False:
            if identity_status is None:
                LAUNCH_STORE.update(connection_id, launch["launch_id"], "RECOVERY_REQUIRED")
            return False
        LAUNCH_STORE.update(connection_id, launch["launch_id"], "RECOVERY_REQUIRED")
        return False
    stopped = managed is None or managed.stop()  # type: ignore[union-attr]
    if stopped:
        if managed is not None:
            _persist_termination_verified(connection_id, managed)
        with LOCK:
            if managed is not None and PROCESSES.get(connection_id) is managed:
                PROCESSES.pop(connection_id, None)
        cleaned = cleanup_connection_files(connection_id, termination_verified=True)
        if cleaned and managed is None:
            write_state(connection_id, termination_verified=True)
        return cleaned
    return False

async def supervise() -> None:
    while True:
        try:
            for path in CONNECTIONS_DIR.glob("*.json"):
                try:
                    record = json.loads(path.read_text(encoding="utf-8"))
                    connection_id = str(UUID(str(record.get("connection_id", ""))))
                    if record.get("revoked"):
                        stop_worker(connection_id)
                        continue
                    process = PROCESSES.get(connection_id)
                    if process is None or (not process.stopping and process.is_stopped()):
                        state = read_state(connection_id)
                        if state.get("status") not in ("REVOKED",):
                            start_worker(connection_id)
                except Exception as exc:
                    print("Aurex worker supervisor recovered from record error:", type(exc).__name__)
        except Exception as exc:
            print("Aurex worker supervisor error:", type(exc).__name__)
        await asyncio.sleep(5)

def verify_service_token(authorization: str | None = Header(default=None)) -> bool:
    if not SERVICE_TOKEN:
        raise HTTPException(status_code=503, detail="Connector service authentication is not configured.")
    supplied = authorization[7:].strip() if authorization and authorization.lower().startswith("bearer ") else ""
    if not supplied or not hmac.compare_digest(supplied, SERVICE_TOKEN):
        raise HTTPException(status_code=401, detail="Connector service authentication failed.")
    return True

class NewConnection(BaseModel):
    connection_id: str = Field(min_length=36, max_length=36)
    user_id: str = Field(min_length=1, max_length=80)
    provider: str = Field(pattern="^(EXNESS|MT5_OTHER)$")
    connection_method: str = Field(pattern="^MT5_TERMINAL$")
    label: str = Field(min_length=2, max_length=80)
    account_reference: str = Field(min_length=2, max_length=120)
    server_name: str = Field(min_length=2, max_length=160)
    credentials: dict[str, str]

app = FastAPI(title="Aurex Managed MT5 Connector", version="1.0.0")

@app.on_event("startup")
async def startup() -> None:
    global SUPERVISOR_TASK
    if "--session-worker" not in sys.argv:
        SUPERVISOR_TASK = asyncio.create_task(supervise())

@app.on_event("shutdown")
async def shutdown() -> None:
    if SUPERVISOR_TASK and SUPERVISOR_TASK is not asyncio.current_task():
        SUPERVISOR_TASK.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await SUPERVISOR_TASK
    if "--session-worker" not in sys.argv:
        for connection_id in list(PROCESSES):
            if not stop_worker(connection_id):
                print("Aurex worker shutdown could not verify process cleanup.")

@app.get("/health")
def health(_: bool = Depends(verify_service_token)):
    return {"status": "ok", "service": "aurex-managed-mt5-connector", "managed_sessions": len(PROCESSES), "checked_at": now()}

@app.post("/v1/connections")
def create_connection(body: NewConnection, _: bool = Depends(verify_service_token)):
    connection_id = str(UUID(body.connection_id))
    if body.connection_method != "MT5_TERMINAL":
        raise HTTPException(status_code=422, detail="This worker only supports isolated MT5 terminal sessions. API connectors must use a verified broker-specific adapter.")
    if body.provider not in ("EXNESS", "MT5_OTHER"):
        raise HTTPException(status_code=422, detail="Unsupported broker provider.")
    credentials = body.credentials
    login = str(credentials.get("login", "")).strip()
    password = str(credentials.get("password", ""))
    server_name = str(credentials.get("serverName", body.server_name)).strip()
    if not login.isdigit() or not password or not server_name:
        raise HTTPException(status_code=422, detail="A numeric MT5 login, trading password and exact server name are required.")
    if not TERMINAL_SOURCE.exists():
        raise HTTPException(status_code=503, detail="MT5_TERMINAL_PATH must point to the installed terminal64.exe on this Windows host.")
    path = file_for(connection_id)
    with LOCK:
        if path.exists():
            old = json.loads(path.read_text(encoding="utf-8"))
            if old.get("user_id") != body.user_id:
                raise HTTPException(status_code=409, detail="Connection ID is already assigned to another customer.")
            if old.get("revoked"):
                raise HTTPException(status_code=409, detail="This connection ID has been revoked; create a new connection.")
            start_worker(connection_id)
            state = read_state(connection_id)
            return {"connection_id": connection_id, "status": state.get("status", "CONNECTING"), "message": state.get("message", "Connection is starting.")}
        record = {
            "connection_id": connection_id,
            "user_id": body.user_id,
            "provider": body.provider,
            "connection_method": body.connection_method,
            "label": body.label,
            "account_reference": body.account_reference,
            "server_name": server_name,
            "encrypted_credentials": encrypt_json({"login": login, "password": password, "serverName": server_name}),
            "revoked": False,
            "created_at": now(),
            "local_port": pick_free_port(),
        }
        write_record(record)
        write_state(connection_id, status="CONNECTING", message="Connection accepted; isolated terminal is starting.", local_port=record["local_port"])
        start_worker(connection_id)
    return {"connection_id": connection_id, "status": "CONNECTING", "message": "Connection accepted; isolated MT5 terminal is starting."}

@app.get("/v1/connections/{connection_id}")
def connection_status(connection_id: str, _: bool = Depends(verify_service_token)):
    record = read_record(connection_id)
    state = read_state(connection_id)
    return {"connection_id": connection_id, "status": "REVOKED" if record.get("revoked") else state.get("status", "CONNECTING"), "message": state.get("message", ""), "last_health_at": state.get("last_health_at"), "last_connected_at": state.get("last_connected_at")}

@app.delete("/v1/connections/{connection_id}")
def revoke_connection(connection_id: str, _: bool = Depends(verify_service_token)):
    with LOCK:
        record = read_record(connection_id)
        record["revoked"] = True
        record["encrypted_credentials"] = ""
        record.setdefault("revoked_at", now())
        write_record(record)
        managed = PROCESSES.get(connection_id)
        LAUNCH_STORE.revoke(connection_id, managed.launch_id if managed is not None else "")
    cleanup_verified = stop_worker(connection_id)
    write_state(connection_id, status="REVOKED", message="Connection revoked; terminal worker cleanup verified." if cleanup_verified else "Connection revoked; cleanup is incomplete and requires retry.", worker_stopped=cleanup_verified, cleanup_completed=cleanup_verified)
    if not cleanup_verified:
        raise HTTPException(status_code=503, detail="Connection revoked, but worker cleanup could not be verified.")
    return {"ok": True, "connection_id": connection_id, "status": "REVOKED", "worker_stopped": True, "cleanup_completed": True}

@app.get("/v1/connections/{connection_id}/tick/{symbol}")
def connection_tick(connection_id: str, symbol: str, _: bool = Depends(verify_service_token)):
    import re
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,32}", symbol):
        raise HTTPException(status_code=422, detail="Invalid symbol.")
    record = read_record(connection_id)
    if record.get("revoked"):
        raise HTTPException(status_code=410, detail="Connection has been revoked.")
    state = read_state(connection_id)
    if state.get("status") != "CONNECTED":
        raise HTTPException(status_code=503, detail="This broker session is not connected.")
    port = int(record["local_port"])
    import urllib.request
    try:
        request = urllib.request.Request(f"http://127.0.0.1:{port}/internal/tick/{symbol}", headers={"X-Aurex-Internal-Key": INTERNAL_KEY})
        with urllib.request.urlopen(request, timeout=3) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception:
        raise HTTPException(status_code=503, detail="Unable to read tick from the isolated terminal session.")

@app.get("/v1/connections/{connection_id}/candles/{symbol}/{timeframe}")
def connection_candles(connection_id: str, symbol: str, timeframe: str, count: int = Query(200, ge=1, le=3000), _: bool = Depends(verify_service_token)):
    import re
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,32}", symbol):
        raise HTTPException(status_code=422, detail="Invalid symbol.")
    if timeframe.upper() not in {"M1", "M5", "M15", "M30", "H1", "H4", "D1", "W1", "MN1"}:
        raise HTTPException(status_code=422, detail="Unsupported timeframe.")
    record = read_record(connection_id)
    if record.get("revoked"):
        raise HTTPException(status_code=410, detail="Connection has been revoked.")
    state = read_state(connection_id)
    if state.get("status") != "CONNECTED":
        raise HTTPException(status_code=503, detail="This broker session is not connected.")
    port = int(record["local_port"])
    import urllib.request
    try:
        url = f"http://127.0.0.1:{port}/internal/candles/{symbol}/{timeframe}?count={max(1, min(int(count), 3000))}"
        request = urllib.request.Request(url, headers={"X-Aurex-Internal-Key": INTERNAL_KEY})
        with urllib.request.urlopen(request, timeout=8) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception:
        raise HTTPException(status_code=503, detail="Unable to read candles from the isolated terminal session.")

def session_worker(connection_id: str) -> None:
    import MetaTrader5 as mt5
    from fastapi import FastAPI
    connection_id = str(UUID(connection_id))
    record_path = file_for(connection_id)
    state_path = STATE_DIR / f"{connection_id}.json"
    local_port = int(os.getenv("AUREX_SESSION_LOCAL_PORT", "0"))
    terminal_target = TERMINALS_DIR / connection_id
    terminal_exe = terminal_target / TERMINAL_SOURCE.name

    def read_local_record() -> dict[str, Any]:
        return json.loads(record_path.read_text(encoding="utf-8"))

    def update(status: str, message: str, **extra: Any) -> None:
        current = {}
        if state_path.exists():
            try: current = json.loads(state_path.read_text(encoding="utf-8"))
            except Exception: current = {}
        current.update({"connection_id": connection_id, "status": status, "message": message, "last_health_at": now(), **extra})
        temp = state_path.with_suffix(".tmp")
        temp.write_text(json.dumps(current, separators=(",", ":")), encoding="utf-8")
        os.replace(temp, state_path)

    session_app = FastAPI(title="Aurex isolated MT5 session", docs_url=None, redoc_url=None, openapi_url=None)

    def authorized_internal(x_aurex_internal_key: str | None = Header(default=None, alias="X-Aurex-Internal-Key")):
        expected = INTERNAL_KEY
        if not expected or not x_aurex_internal_key or not hmac.compare_digest(x_aurex_internal_key, expected):
            raise HTTPException(status_code=401, detail="Internal session access denied.")

    def validate_symbol(symbol: str) -> str:
        import re
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,32}", symbol):
            raise HTTPException(status_code=400, detail="Invalid symbol.")
        if not mt5.symbol_select(symbol, True):
            raise HTTPException(status_code=404, detail=f"Symbol {symbol} is unavailable.")
        return symbol

    @session_app.get("/internal/health")
    def internal_health(_: bool = Depends(authorized_internal)):
        terminal = mt5.terminal_info()
        return {"status": "CONNECTED" if terminal and terminal.connected else "RECONNECTING", "connected": bool(terminal and terminal.connected), "checked_at": now()}

    @session_app.get("/internal/tick/{symbol}")
    def internal_tick(symbol: str, _: bool = Depends(authorized_internal)):
        validate_symbol(symbol)
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            raise HTTPException(status_code=503, detail="No tick available.")
        return {"symbol": symbol, "bid": tick.bid, "ask": tick.ask, "spread": round(tick.ask - tick.bid, 8), "last": tick.last, "time": datetime.fromtimestamp(tick.time, tz=timezone.utc).isoformat(), "time_msc": tick.time_msc}

    @session_app.get("/internal/candles/{symbol}/{timeframe}")
    def internal_candles(symbol: str, timeframe: str, count: int = 200, _: bool = Depends(authorized_internal)):
        import pandas as pd
        from datetime import datetime
        frames = {"M1": mt5.TIMEFRAME_M1, "M5": mt5.TIMEFRAME_M5, "M15": mt5.TIMEFRAME_M15, "M30": mt5.TIMEFRAME_M30, "H1": mt5.TIMEFRAME_H1, "H4": mt5.TIMEFRAME_H4, "D1": mt5.TIMEFRAME_D1, "W1": mt5.TIMEFRAME_W1, "MN1": mt5.TIMEFRAME_MN1}
        validate_symbol(symbol)
        frame = frames.get(timeframe.upper())
        if frame is None: raise HTTPException(status_code=400, detail="Unsupported timeframe.")
        rates = mt5.copy_rates_from_pos(symbol, frame, 0, max(1, min(int(count), 3000)))
        if rates is None or len(rates) == 0: raise HTTPException(status_code=503, detail="No candle history available.")
        df = pd.DataFrame(rates)
        df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
        candles = [{"time": row["time"].isoformat(), "open": float(row["open"]), "high": float(row["high"]), "low": float(row["low"]), "close": float(row["close"]), "tick_volume": int(row["tick_volume"]), "spread": int(row["spread"]), "real_volume": int(row["real_volume"])} for _, row in df.iterrows()]
        return {"symbol": symbol, "timeframe": timeframe.upper(), "count": len(candles), "candles": candles}

    def run_terminal() -> None:
        delay = 2
        while True:
            try:
                record = read_local_record()
                if record.get("revoked"):
                    update("REVOKED", "Connection revoked.")
                    return
                credentials = decrypt_json(record["encrypted_credentials"])
                ensure_terminal_copy(connection_id)
                update("CONNECTING", "Authenticating with broker terminal.")
                login = int(str(credentials["login"]))
                ok = mt5.initialize(str(terminal_exe), login=login, password=credentials["password"], server=credentials["serverName"], timeout=60000, portable=True)
                if not ok:
                    message = f"MT5 initialization/login failed ({mt5.last_error()})."
                    update("RECONNECTING", message)
                    mt5.shutdown()
                    time.sleep(delay)
                    delay = min(delay * 2, 60)
                    continue
                account = mt5.account_info()
                terminal = mt5.terminal_info()
                if account is None or terminal is None or not terminal.connected or int(account.login) != login:
                    update("RECONNECTING", "Broker login/account verification failed.")
                    mt5.shutdown()
                    time.sleep(delay)
                    delay = min(delay * 2, 60)
                    continue
                update("CONNECTED", "MT5 terminal authenticated; automatic reconnect supervisor active.", last_connected_at=now(), account_login=str(account.login), broker=account.company, server=account.server)
                delay = 2
                while True:
                    current = read_local_record()
                    if current.get("revoked"):
                        mt5.shutdown()
                        update("REVOKED", "Connection revoked; terminal session stopped.")
                        return
                    terminal = mt5.terminal_info()
                    account = mt5.account_info()
                    if terminal is None or not terminal.connected or account is None or int(account.login) != login:
                        update("RECONNECTING", "Broker connection lost; automatic reconnect is in progress.")
                        mt5.shutdown()
                        break
                    update("CONNECTED", "MT5 terminal authenticated; connection healthy.", account_login=str(account.login), broker=account.company, server=account.server)
                    time.sleep(5)
                time.sleep(delay)
                delay = min(delay * 2, 60)
            except Exception as exc:
                update("RECONNECTING", "Session worker encountered a recoverable error: " + type(exc).__name__)
                try: mt5.shutdown()
                except Exception: pass
                time.sleep(delay)
                delay = min(delay * 2, 60)

    import threading
    thread = threading.Thread(target=run_terminal, daemon=True)
    thread.start()
    uvicorn.run(session_app, host="127.0.0.1", port=local_port, log_level="error", access_log=False)

if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--session-worker":
        session_worker(sys.argv[2])
    else:
        if not SERVICE_TOKEN:
            print("ERROR: Set AUREX_CONNECTOR_SERVICE_TOKEN before starting the service.", file=sys.stderr)
            sys.exit(2)
        if not WORKER_KEY_TEXT:
            print("ERROR: Set AUREX_WORKER_ENCRYPTION_KEY before starting the service.", file=sys.stderr)
            sys.exit(2)
        if not INTERNAL_KEY:
            print("ERROR: Set AUREX_WORKER_INTERNAL_KEY before starting the service.", file=sys.stderr)
            sys.exit(2)
        uvicorn.run(app, host=os.getenv("AUREX_CONNECTOR_HOST", "127.0.0.1"), port=int(os.getenv("AUREX_CONNECTOR_PORT", "8010")), log_level="info")
