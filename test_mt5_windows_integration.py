"""Disposable Windows-only lifecycle checks; never launch MT5 or contact a broker."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

import pytest


pytestmark = pytest.mark.skipif(os.name != "nt", reason="requires Windows process APIs")
CREATE_SUSPENDED = 0x00000004


def run_harness(source: str) -> str:
    with tempfile.TemporaryDirectory(prefix="aurex-win-integration-") as temp:
        root = Path(temp)
        environment = os.environ.copy()
        environment["AUREX_WORKER_DATA_DIR"] = str(root / "worker-data")
        environment["AUREX_WORKER_LAUNCH_DB"] = str(root / "launches.sqlite3")
        environment["AUREX_CONNECTOR_SERVICE_TOKEN"] = "offline-integration-token"
        result = subprocess.run(
            [sys.executable, "-c", textwrap.dedent(source)],
            cwd=Path.cwd(), env=environment,
            capture_output=True, text=True, timeout=45, check=False,
        )
        assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
        return result.stdout


def test_real_job_object_assignment_resume_and_kill_on_close():
    run_harness(
        r'''
        import subprocess, sys, time
        from pathlib import Path
        import mt5_session_worker as worker

        marker = worker.DATA_DIR / "resumed.txt"
        child_code = "from pathlib import Path; import time; Path(r'%s').write_text('resumed'); time.sleep(30)" % marker
        process = subprocess.Popen(
            [sys.executable, "-c", child_code],
            creationflags=0x00000004,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        job = None
        try:
            job = worker.WindowsJob.create()
            job.assign(process._handle)
            assert not marker.exists()
            worker.resume_suspended_process(process._handle)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and not marker.exists():
                time.sleep(0.05)
            assert marker.exists()
            assert not job.is_empty()
            assert job.terminate() is None
            process.wait(timeout=5)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and not job.is_empty():
                time.sleep(0.05)
            assert job.is_empty()
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            if job is not None and job.handle:
                assert job.close()

        close_marker = worker.DATA_DIR / "close-kill.txt"
        close_code = "from pathlib import Path; import time; Path(r'%s').write_text('started'); time.sleep(30)" % close_marker
        process = subprocess.Popen(
            [sys.executable, "-c", close_code],
            creationflags=0x00000004,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        job = None
        try:
            job = worker.WindowsJob.create()
            job.assign(process._handle)
            worker.resume_suspended_process(process._handle)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and not close_marker.exists():
                time.sleep(0.05)
            assert close_marker.exists()
            assert job.close()
            process.wait(timeout=5)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            if job is not None and job.handle:
                job.close()
        '''
    )


def test_real_process_creation_time_mismatch_fails_closed():
    run_harness(
        r'''
        import subprocess, sys
        import mt5_session_worker as worker

        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            identity = worker.process_identity_from_handle(process._handle, process.pid)
            assert worker.process_identity_matches(identity) is True
            mismatched = dict(identity)
            mismatched["creation_time_100ns"] += 1
            assert worker.process_identity_matches(mismatched) is False
            assert worker.process_identity_matches({"pid": process.pid}) is None
            assert process.poll() is None
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)
        '''
    )


def test_real_ambiguous_recovery_requires_recovery_state_and_does_not_kill_child():
    run_harness(
        r'''
        import subprocess, sys
        from pathlib import Path
        from uuid import uuid4
        import mt5_session_worker as worker

        worker.LAUNCH_STORE = worker.LaunchStore(worker.LAUNCH_DB)
        connection_id = str(uuid4())
        worker.write_record({
            "connection_id": connection_id, "user_id": "integration", "provider": "MT5_OTHER",
            "label": "integration", "account_reference": "integration", "server_name": "test",
            "encrypted_credentials": "offline", "revoked": True, "created_at": worker.now(),
            "local_port": 39099,
        })
        (worker.TERMINALS_DIR / connection_id).mkdir(parents=True)
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            identity = worker.process_identity_from_handle(process._handle, process.pid)
            identity["creation_time_100ns"] += 1
            assert worker.LAUNCH_STORE.begin(connection_id, "ambiguous")
            worker.LAUNCH_STORE.update(connection_id, "ambiguous", "ASSIGNED", identity)
            assert worker.stop_worker(connection_id) is False
            assert worker.LAUNCH_STORE.get(connection_id)["state"] == "RECOVERY_REQUIRED"
            assert process.poll() is None
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)
        '''
    )


def test_terminal_copy_isolation_harness_uses_distinct_disposable_directories():
    run_harness(
        r'''
        from pathlib import Path
        import mt5_session_worker as worker

        source = worker.DATA_DIR / "fake-terminal-source"
        source.mkdir(parents=True)
        worker.TERMINAL_SOURCE = source / "terminal64.exe"
        worker.TERMINAL_SOURCE.write_text("fake executable", encoding="utf-8")
        (source / "shared.dat").write_text("source", encoding="utf-8")
        worker.TERMINALS_DIR = worker.DATA_DIR / "terminals-under-test"
        worker.TERMINALS_DIR.mkdir()

        first = worker.ensure_terminal_copy("customer-one")
        second = worker.ensure_terminal_copy("customer-two")
        assert first.parent != second.parent
        assert first.read_text(encoding="utf-8") == "fake executable"
        assert second.read_text(encoding="utf-8") == "fake executable"
        (first.parent / "customer.dat").write_text("one", encoding="utf-8")
        assert not (second.parent / "customer.dat").exists()
        assert (source / "shared.dat").read_text(encoding="utf-8") == "source"
        '''
    )
