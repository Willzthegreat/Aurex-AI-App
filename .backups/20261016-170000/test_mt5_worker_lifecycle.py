import json
import os
import shutil
import threading
import time
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient


TEST_ROOT = Path.cwd() / f".worker-lifecycle-test-{os.getpid()}"
os.environ["AUREX_WORKER_DATA_DIR"] = str(TEST_ROOT)
os.environ["AUREX_CONNECTOR_SERVICE_TOKEN"] = "offline-test-token"

import mt5_session_worker as worker  # noqa: E402


class FakeProcess:
    def __init__(self, exits_on_terminate=True):
        self.pid = 4242
        self.exits_on_terminate = exits_on_terminate
        self.exited = False
        self.terminated = False
        self.killed = False
        self._handle = 9876

    def poll(self):
        return 0 if self.exited else None

    def terminate(self):
        self.terminated = True
        if self.exits_on_terminate:
            self.exited = True

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        if not self.exited:
            raise TimeoutError("fake process did not exit")
        return 0


class FakeJob:
    def __init__(self, process, terminates=True, raises=False, events=None):
        self.process = process
        self.terminates = terminates
        self.raises = raises
        self.terminated = False
        self.closed = False
        self.events = events if events is not None else []

    def assign(self, process_handle):
        self.events.append(("assign", process_handle))

    def terminate(self):
        if self.raises:
            raise OSError("job termination failed")
        self.events.append(("terminate", None))
        self.terminated = True
        if self.terminates:
            self.process.exited = True

    def is_empty(self):
        return self.process.exited

    def close(self):
        self.events.append(("close", None))
        self.closed = True
        return True


@pytest.fixture(autouse=True)
def clean_worker_state():
    worker.PROCESSES.clear()
    shutil.rmtree(TEST_ROOT, ignore_errors=True)
    for folder in (worker.CONNECTIONS_DIR, worker.TERMINALS_DIR, worker.STATE_DIR):
        folder.mkdir(parents=True, exist_ok=True)
    yield
    worker.PROCESSES.clear()
    shutil.rmtree(TEST_ROOT, ignore_errors=True)


def add_record(connection_id, revoked=False):
    record = {
        "connection_id": connection_id,
        "user_id": "customer-a",
        "provider": "MT5_OTHER",
        "label": "Offline test",
        "account_reference": "test-account",
        "server_name": "Test-Server",
        "encrypted_credentials": "encrypted-test-value",
        "revoked": revoked,
        "created_at": worker.now(),
        "local_port": 39001,
    }
    worker.write_record(record)
    worker.write_state(connection_id, status="CONNECTED")
    return record


def test_normal_shutdown_terminates_worker_and_owned_job():
    process = FakeProcess()
    job = FakeJob(process)
    managed = worker.ManagedProcess(process, job)
    assert managed.stop(timeout=0.2) is True
    assert process.terminated is False
    assert job.terminated is True
    assert job.closed is True


def test_worker_launch_is_suspended_until_job_assignment_then_resume(monkeypatch):
    process = FakeProcess()
    events = []
    job = FakeJob(process, events=events)
    monkeypatch.setattr(worker.os, "name", "nt")
    monkeypatch.setattr(worker.WindowsJob, "create", classmethod(lambda cls: job))
    monkeypatch.setattr(worker.subprocess, "Popen", lambda *args, **kwargs: (events.append(("popen", kwargs["creationflags"])), process)[1])
    monkeypatch.setattr(worker, "resume_suspended_process", lambda handle: events.append(("resume", handle)))
    managed = worker.launch_worker_process(str(uuid4()), {})
    assert events[0][0] == "popen"
    assert events[0][1] & 0x00000004
    assert [event[0] for event in events] == ["popen", "assign", "resume"]
    assert managed.process is process


def test_job_assignment_failure_aborts_unregistered_suspended_worker(monkeypatch):
    process = FakeProcess()
    job = FakeJob(process)
    job.assign = lambda handle: (_ for _ in ()).throw(OSError("assignment failed"))
    monkeypatch.setattr(worker.os, "name", "nt")
    monkeypatch.setattr(worker.WindowsJob, "create", classmethod(lambda cls: job))
    monkeypatch.setattr(worker.subprocess, "Popen", lambda *args, **kwargs: process)
    with pytest.raises(RuntimeError, match="launch failed"):
        worker.launch_worker_process(str(uuid4()), {})
    assert process.terminated is True
    assert process.exited is True
    assert job.closed is True


def test_resume_failure_terminates_assigned_job_before_registration(monkeypatch):
    process = FakeProcess()
    job = FakeJob(process)
    monkeypatch.setattr(worker.os, "name", "nt")
    monkeypatch.setattr(worker.WindowsJob, "create", classmethod(lambda cls: job))
    monkeypatch.setattr(worker.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(worker, "resume_suspended_process", lambda handle: (_ for _ in ()).throw(OSError("resume failed")))
    with pytest.raises(RuntimeError, match="launch failed"):
        worker.launch_worker_process(str(uuid4()), {})
    assert job.terminated is True
    assert process.exited is True
    assert job.closed is True


def test_api_error_and_handle_close_failure_are_not_success(monkeypatch):
    process = FakeProcess()
    job = FakeJob(process, raises=True)
    assert worker.ManagedProcess(process, job).stop(timeout=0.05) is False
    assert job.closed is False  # retained for a verified retry while descendants are unknown

    process = FakeProcess()
    job = FakeJob(process)
    job.close = lambda: False
    assert worker.ManagedProcess(process, job).stop(timeout=0.05) is False


def test_windows_api_signatures_and_close_errors_are_checked(monkeypatch):
    class ApiFunction:
        def __init__(self, result=True):
            self.argtypes = None
            self.restype = None
            self.result = result

        def __call__(self, *args):
            return self.result

    class FakeKernel32:
        def __init__(self):
            self.SetInformationJobObject = ApiFunction()
            self.AssignProcessToJobObject = ApiFunction()
            self.TerminateJobObject = ApiFunction()
            self.QueryInformationJobObject = ApiFunction()
            self.CloseHandle = ApiFunction(False)

    api = FakeKernel32()
    monkeypatch.setattr(worker.ctypes, "WinDLL", lambda *args, **kwargs: api)
    job = worker.WindowsJob(123)
    assert api.AssignProcessToJobObject.argtypes == [worker.ctypes.c_void_p, worker.ctypes.c_void_p]
    assert api.AssignProcessToJobObject.restype is worker.ctypes.c_int
    assert job.close() is False
    assert job.handle.value == 123


def test_process_identity_check_fails_closed_on_api_or_close_error(monkeypatch):
    class ApiFunction:
        def __init__(self, result=True):
            self.argtypes = None
            self.restype = None
            self.result = result

        def __call__(self, *args):
            return self.result

    class FakeKernel32:
        def __init__(self):
            self.OpenProcess = ApiFunction(777)
            self.GetExitCodeProcess = ApiFunction(False)
            self.CloseHandle = ApiFunction(False)

    api = FakeKernel32()
    monkeypatch.setattr(worker.os, "name", "nt")
    monkeypatch.setattr(worker.ctypes, "WinDLL", lambda *args, **kwargs: api)
    assert worker.process_is_running(9999) is True


def test_terminal_descendant_remaining_alive_blocks_cleanup():
    connection_id = str(uuid4())
    add_record(connection_id, revoked=True)
    terminal_dir = worker.TERMINALS_DIR / connection_id
    terminal_dir.mkdir(parents=True)
    process = FakeProcess()

    class LingeringJob(FakeJob):
        def is_empty(self):
            return False

    worker.PROCESSES[connection_id] = worker.ManagedProcess(process, LingeringJob(process))
    assert worker.stop_worker(connection_id) is False
    assert terminal_dir.exists()


def test_forced_termination_falls_back_but_requires_job_empty():
    process = FakeProcess(exits_on_terminate=False)
    job = FakeJob(process, terminates=True)
    managed = worker.ManagedProcess(process, job)
    assert managed.stop(timeout=0.05) is True
    assert job.terminated is True


def test_timeout_and_termination_failure_are_not_reported_as_cleanup_success():
    process = FakeProcess(exits_on_terminate=False)
    job = FakeJob(process, terminates=False)
    managed = worker.ManagedProcess(process, job)
    assert managed.stop(timeout=0.05) is False
    assert process.killed is True

    process = FakeProcess()
    failing_job = FakeJob(process, raises=True)
    assert worker.ManagedProcess(process, failing_job).stop(timeout=0.05) is False


def test_revoked_connection_cannot_restart_or_reconnect():
    connection_id = str(uuid4())
    add_record(connection_id, revoked=True)
    original_popen = worker.subprocess.Popen
    worker.subprocess.Popen = lambda *args, **kwargs: pytest.fail("revoked connection was restarted")
    try:
        worker.start_worker(connection_id)
    finally:
        worker.subprocess.Popen = original_popen
    assert connection_id not in worker.PROCESSES


def test_revocation_racing_startup_leaves_no_registered_worker(monkeypatch):
    connection_id = str(uuid4())
    add_record(connection_id)
    launch_entered = threading.Event()
    release_launch = threading.Event()
    process = FakeProcess()
    job = FakeJob(process)

    def blocked_launch(connection, env):
        launch_entered.set()
        assert release_launch.wait(timeout=2)
        return worker.ManagedProcess(process, job)

    monkeypatch.setattr(worker, "launch_worker_process", blocked_launch)
    starter = threading.Thread(target=worker.start_worker, args=(connection_id,))
    starter.start()
    assert launch_entered.wait(timeout=2)
    client = TestClient(worker.app)
    result = []
    revoker = threading.Thread(target=lambda: result.append(client.delete(
        f"/v1/connections/{connection_id}", headers={"Authorization": "Bearer offline-test-token"}
    )))
    revoker.start()
    time.sleep(0.05)
    release_launch.set()
    starter.join(timeout=2)
    revoker.join(timeout=2)
    assert result and result[0].status_code == 200
    assert connection_id not in worker.PROCESSES
    client.close()


def test_restart_with_unknown_live_persisted_worker_fails_closed(monkeypatch):
    connection_id = str(uuid4())
    add_record(connection_id, revoked=True)
    terminal_dir = worker.TERMINALS_DIR / connection_id
    terminal_dir.mkdir(parents=True)
    worker.write_state(connection_id, status="REVOKED", pid=7777)
    monkeypatch.setattr(worker, "process_is_running", lambda pid: pid == 7777)
    assert worker.stop_worker(connection_id) is False
    assert terminal_dir.exists()


def test_restart_with_confirmed_exited_persisted_worker_can_clean_owned_files(monkeypatch):
    connection_id = str(uuid4())
    add_record(connection_id, revoked=True)
    terminal_dir = worker.TERMINALS_DIR / connection_id
    terminal_dir.mkdir(parents=True)
    worker.write_state(connection_id, status="REVOKED", pid=8888)
    monkeypatch.setattr(worker, "process_is_running", lambda pid: False)
    assert worker.stop_worker(connection_id) is True
    assert not terminal_dir.exists()


def test_revoke_clears_credentials_verifies_cleanup_and_is_idempotent():
    connection_id = str(uuid4())
    add_record(connection_id)
    terminal_dir = worker.TERMINALS_DIR / connection_id
    terminal_dir.mkdir(parents=True)
    (terminal_dir / "terminal-data.txt").write_text("sensitive terminal data", encoding="utf-8")
    process = FakeProcess()
    worker.PROCESSES[connection_id] = worker.ManagedProcess(process, FakeJob(process))

    client = TestClient(worker.app)
    headers = {"Authorization": "Bearer offline-test-token"}
    response = client.delete(f"/v1/connections/{connection_id}", headers=headers)
    assert response.status_code == 200
    assert response.json()["cleanup_completed"] is True
    saved = json.loads(worker.file_for(connection_id).read_text(encoding="utf-8"))
    assert saved["revoked"] is True
    assert saved["encrypted_credentials"] == ""
    assert not terminal_dir.exists()
    assert client.delete(f"/v1/connections/{connection_id}", headers=headers).status_code == 200
    client.close()


def test_concurrent_revocation_is_idempotent_and_unrelated_directory_is_untouched():
    connection_id = str(uuid4())
    add_record(connection_id)
    unrelated = worker.DATA_DIR / "unrelated-customer-directory"
    unrelated.mkdir(parents=True)
    (unrelated / "keep.txt").write_text("keep", encoding="utf-8")
    process = FakeProcess()
    worker.PROCESSES[connection_id] = worker.ManagedProcess(process, FakeJob(process))
    results = []
    threads = [threading.Thread(target=lambda: results.append(worker.stop_worker(connection_id))) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results == [True, True]
    assert (unrelated / "keep.txt").exists()


def test_cleanup_failure_returns_503_and_keeps_tombstone_without_credentials(monkeypatch):
    connection_id = str(uuid4())
    add_record(connection_id)
    terminal_dir = worker.TERMINALS_DIR / connection_id
    terminal_dir.mkdir(parents=True)
    process = FakeProcess()
    worker.PROCESSES[connection_id] = worker.ManagedProcess(process, FakeJob(process))
    real_rmtree = worker.shutil.rmtree

    def fail_owned_tree(path, *args, **kwargs):
        if Path(path) == terminal_dir:
            raise OSError("offline cleanup failure")
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(worker.shutil, "rmtree", fail_owned_tree)
    client = TestClient(worker.app)
    response = client.delete(f"/v1/connections/{connection_id}", headers={"Authorization": "Bearer offline-test-token"})
    assert response.status_code == 503
    saved = json.loads(worker.file_for(connection_id).read_text(encoding="utf-8"))
    assert saved["revoked"] is True
    assert saved["encrypted_credentials"] == ""
    assert terminal_dir.exists()
    client.close()
