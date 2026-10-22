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
    worker.LAUNCH_STORE = worker.LaunchStore(worker.LAUNCH_DB)
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
    monkeypatch.setattr(worker, "process_identity_from_handle", lambda handle, pid: {"pid": pid, "creation_time_100ns": 1})
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
    monkeypatch.setattr(worker, "process_identity_from_handle", lambda handle, pid: {"pid": pid, "creation_time_100ns": 1})
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
    monkeypatch.setattr(worker, "process_identity_from_handle", lambda handle, pid: {"pid": pid, "creation_time_100ns": 1})
    monkeypatch.setattr(worker, "resume_suspended_process", lambda handle: (_ for _ in ()).throw(OSError("resume failed")))
    with pytest.raises(RuntimeError, match="launch failed"):
        worker.launch_worker_process(str(uuid4()), {})
    assert job.terminated is True
    assert process.exited is True
    assert job.closed is True


def test_persistence_failure_before_resume_never_releases_worker(monkeypatch):
    process = FakeProcess()
    job = FakeJob(process)
    events = []
    monkeypatch.setattr(worker.os, "name", "nt")
    monkeypatch.setattr(worker.WindowsJob, "create", classmethod(lambda cls: job))
    monkeypatch.setattr(worker.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(worker, "process_identity_from_handle", lambda handle, pid: {"pid": pid, "creation_time_100ns": 1})
    monkeypatch.setattr(worker, "resume_suspended_process", lambda handle: events.append("resume"))
    monkeypatch.setattr(worker, "_persist_launch_state", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("journal failure")))
    with pytest.raises(RuntimeError):
        worker.launch_worker_process(str(uuid4()), {}, "journal-before-resume")
    assert events == []
    assert process.exited is True
    assert job.closed is True


def test_persistence_failure_after_resume_stops_job_and_does_not_register(monkeypatch):
    process = FakeProcess()
    job = FakeJob(process)
    events = []
    calls = []
    monkeypatch.setattr(worker.os, "name", "nt")
    monkeypatch.setattr(worker.WindowsJob, "create", classmethod(lambda cls: job))
    monkeypatch.setattr(worker.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(worker, "process_identity_from_handle", lambda handle, pid: {"pid": pid, "creation_time_100ns": 1})

    def fail_after_resume(*args, **kwargs):
        calls.append(args[2])
        if len(calls) == 2:
            raise OSError("post-resume journal failure")

    monkeypatch.setattr(worker, "_persist_launch_state", fail_after_resume)
    monkeypatch.setattr(worker, "resume_suspended_process", lambda handle: events.append("resume"))
    with pytest.raises(RuntimeError):
        worker.launch_worker_process(str(uuid4()), {}, "journal-after-resume")
    assert events == ["resume"]
    assert process.exited is True
    assert job.closed is True


def test_created_launch_record_is_recovery_required_and_cannot_duplicate(monkeypatch):
    connection_id = str(uuid4())
    add_record(connection_id)
    assert worker.LAUNCH_STORE.begin(connection_id, "created-only") is True
    monkeypatch.setattr(worker.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("duplicate launch"))
    worker.start_worker(connection_id)
    assert worker.LAUNCH_STORE.get(connection_id)["state"] == "RECOVERY_REQUIRED"


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


def test_windows_job_structures_match_64_bit_sdk_layout():
    accounting = worker._JobBasicAccountingInformation
    assert [field[1] for field in accounting._fields_] == [
        worker.ctypes.c_longlong, worker.ctypes.c_longlong,
        worker.ctypes.c_longlong, worker.ctypes.c_longlong,
        worker.ctypes.c_uint32,
        worker.ctypes.c_uint32, worker.ctypes.c_uint32, worker.ctypes.c_uint32,
    ]
    assert worker.ctypes.sizeof(accounting) == 48
    assert [getattr(accounting, name).offset for name, _ in accounting._fields_] == [0, 8, 16, 24, 32, 36, 40, 44]
    assert worker.ctypes.sizeof(worker._JobBasicLimitInformation) == 64
    assert worker.ctypes.sizeof(worker._JobIoCounters) == 48
    assert worker.ctypes.sizeof(worker._JobExtendedLimitInformation) == 144


def test_process_inspection_api_failure_fails_closed(monkeypatch):
    class ApiFunction:
        def __init__(self, result):
            self.argtypes = None
            self.restype = None
            self.result = result

        def __call__(self, *args):
            return self.result

    class FakeKernel32:
        def __init__(self):
            self.OpenProcess = ApiFunction(None)
            self.GetExitCodeProcess = ApiFunction(False)
            self.CloseHandle = ApiFunction(True)

    monkeypatch.setattr(worker.os, "name", "nt")
    monkeypatch.setattr(worker.ctypes, "WinDLL", lambda *args, **kwargs: FakeKernel32())
    assert worker.process_is_running(1234) is True


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

    def blocked_launch(connection, env, launch_id):
        launch_entered.set()
        assert release_launch.wait(timeout=2)
        return worker.ManagedProcess(process, job, {"pid": process.pid, "creation_time_100ns": 1}, launch_id="race-launch")

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
    worker.LAUNCH_STORE.begin(connection_id, "stale-live")
    worker.LAUNCH_STORE.update(connection_id, "stale-live", "ASSIGNED", {"pid": 7777, "creation_time_100ns": 1})
    monkeypatch.setattr(worker, "process_is_running", lambda pid: pid == 7777)
    assert worker.stop_worker(connection_id) is False
    assert terminal_dir.exists()


def test_restart_with_confirmed_exited_persisted_worker_can_clean_owned_files(monkeypatch):
    connection_id = str(uuid4())
    add_record(connection_id, revoked=True)
    terminal_dir = worker.TERMINALS_DIR / connection_id
    terminal_dir.mkdir(parents=True)
    worker.write_state(connection_id, status="REVOKED", pid=8888, termination_verified=True)
    worker.LAUNCH_STORE.begin(connection_id, "stopped-launch")
    worker.LAUNCH_STORE.update(connection_id, "stopped-launch", "STOPPED", {"pid": 8888, "creation_time_100ns": 1}, True)
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


def test_unverified_persisted_pid_never_authorizes_directory_cleanup():
    connection_id = str(uuid4())
    add_record(connection_id, revoked=True)
    terminal_dir = worker.TERMINALS_DIR / connection_id
    terminal_dir.mkdir(parents=True)
    worker.write_state(connection_id, status="REVOKED", pid=8888)
    worker.LAUNCH_STORE.begin(connection_id, "unverified-launch")
    worker.LAUNCH_STORE.update(connection_id, "unverified-launch", "RECOVERY_REQUIRED", {"pid": 8888, "creation_time_100ns": 1})
    assert worker.stop_worker(connection_id) is False
    assert terminal_dir.exists()


def test_launch_state_is_durable_before_resume(monkeypatch):
    connection_id = str(uuid4())
    add_record(connection_id)
    process = FakeProcess()
    job = FakeJob(process)
    events = []
    monkeypatch.setattr(worker.os, "name", "nt")
    monkeypatch.setattr(worker.WindowsJob, "create", classmethod(lambda cls: job))
    monkeypatch.setattr(worker.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(worker, "process_identity_from_handle", lambda handle, pid: {"pid": pid, "creation_time_100ns": 99})
    monkeypatch.setattr(worker, "resume_suspended_process", lambda handle: events.append("resume"))
    original = worker._persist_launch_state

    def observed(connection, launch_id, state, identity=None):
        result = original(connection, launch_id, state, identity)
        if state == "ASSIGNED":
            saved = worker.LAUNCH_STORE.get(connection)
            assert saved["launch_id"] == launch_id
            assert saved["creation_time_100ns"] == 99
            assert not events
        return result

    monkeypatch.setattr(worker, "_persist_launch_state", observed)
    assert worker.LAUNCH_STORE.begin(connection_id, "launch-before-resume") is True
    managed = worker.launch_worker_process(connection_id, {}, "launch-before-resume")
    assert managed.identity["creation_time_100ns"] == 99
    assert events == ["resume"]


def test_cleanup_function_requires_verified_termination():
    connection_id = str(uuid4())
    add_record(connection_id, revoked=True)
    terminal_dir = worker.TERMINALS_DIR / connection_id
    terminal_dir.mkdir(parents=True)
    assert worker.cleanup_connection_files(connection_id) is False
    assert terminal_dir.exists()


def test_missing_terminal_executable_never_deletes_existing_directory(monkeypatch):
    connection_id = str(uuid4())
    terminal_dir = worker.TERMINALS_DIR / connection_id
    terminal_dir.mkdir(parents=True)
    (terminal_dir / "sensitive-state.dat").write_text("keep", encoding="utf-8")
    monkeypatch.setattr(worker, "TERMINAL_SOURCE", terminal_dir / "missing-terminal64.exe")
    with pytest.raises(RuntimeError, match="verified cleanup"):
        worker.ensure_terminal_copy(connection_id)
    assert terminal_dir.exists()
    assert (terminal_dir / "sensitive-state.dat").exists()


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


def test_concurrent_authenticated_revocation_requests_are_idempotent():
    connection_id = str(uuid4())
    add_record(connection_id)
    process = FakeProcess()
    worker.PROCESSES[connection_id] = worker.ManagedProcess(process, FakeJob(process))
    results = []
    client = TestClient(worker.app)

    def revoke():
        results.append(client.delete(
            f"/v1/connections/{connection_id}",
            headers={"Authorization": "Bearer offline-test-token"},
        ).status_code)

    threads = [threading.Thread(target=revoke) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=3)
    assert sorted(results) == [200, 200]
    assert connection_id not in worker.PROCESSES
    client.close()


def test_durable_active_launch_prevents_duplicate_start(monkeypatch):
    connection_id = str(uuid4())
    add_record(connection_id)
    worker.write_state(
        connection_id,
        launch_id="launch-existing",
        launch_state="RUNNING",
        worker_identity={"pid": 1234, "creation_time_100ns": 55},
    )
    assert worker.LAUNCH_STORE.begin(connection_id, "launch-existing") is True
    worker.LAUNCH_STORE.update(connection_id, "launch-existing", "RUNNING", {"pid": 1234, "creation_time_100ns": 55})
    monkeypatch.setattr(worker, "process_identity_matches", lambda identity: True)
    monkeypatch.setattr(worker.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("duplicate worker started"))
    worker.start_worker(connection_id)
    assert connection_id not in worker.PROCESSES


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
