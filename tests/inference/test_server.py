"""Tests for carpenter.inference.server — InferenceServer lifecycle."""

import subprocess
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from carpenter import config
from carpenter.inference.server import InferenceServer, _should_repack
from carpenter.inference import get_inference_server


@pytest.fixture
def server():
    """Return a fresh InferenceServer instance."""
    return InferenceServer()


@pytest.fixture
def local_config(monkeypatch):
    """Set up config for a local inference server."""
    monkeypatch.setitem(config.CONFIG, "local_llama_cpp_path", "/usr/bin/llama-server")
    monkeypatch.setitem(config.CONFIG, "local_model_path", "/tmp/test-model.gguf")
    monkeypatch.setitem(config.CONFIG, "local_server_host", "127.0.0.1")
    monkeypatch.setitem(config.CONFIG, "local_server_port", 8081)
    monkeypatch.setitem(config.CONFIG, "local_context_size", 8192)
    monkeypatch.setitem(config.CONFIG, "local_gpu_layers", 0)
    monkeypatch.setitem(config.CONFIG, "local_parallel", 1)
    monkeypatch.setitem(config.CONFIG, "local_repack", "auto")
    monkeypatch.setitem(config.CONFIG, "local_server_args", [])
    monkeypatch.setitem(config.CONFIG, "local_startup_timeout", 5)


# -- _should_repack() --

def test_should_repack_bool_true():
    """Explicit True bypasses auto-detect."""
    assert _should_repack("/fake/model.gguf", True) is True


def test_should_repack_bool_false():
    """Explicit False bypasses auto-detect."""
    assert _should_repack("/fake/model.gguf", False) is False


def test_should_repack_auto_enough_memory(monkeypatch, tmp_path):
    """Auto mode enables repacking when sufficient memory is available."""
    model = tmp_path / "model.gguf"
    model.write_bytes(b"\x00")  # tiny file, mock getsize

    # Pretend 500 MB model; 4000 MB available (needs 500 + 1024 = 1524 MB)
    monkeypatch.setattr("os.path.getsize", lambda p: 500 * 1024 * 1024)
    meminfo = "MemTotal:        8000000 kB\nMemAvailable:    4096000 kB\n"
    _real_open = open
    monkeypatch.setattr(
        "builtins.open",
        lambda path, *a, **kw: __import__("io").StringIO(meminfo)
        if path == "/proc/meminfo"
        else _real_open(path, *a, **kw),
    )

    assert _should_repack(str(model), "auto") is True


def test_should_repack_auto_insufficient_memory(monkeypatch, tmp_path):
    """Auto mode disables repacking when memory is tight."""
    model = tmp_path / "model.gguf"
    model.write_bytes(b"\x00")

    # Pretend 2000 MB model; 2500 MB available (needs 2000 + 1024 = 3024 MB)
    monkeypatch.setattr("os.path.getsize", lambda p: 2000 * 1024 * 1024)
    meminfo = "MemTotal:        8000000 kB\nMemAvailable:    2560000 kB\n"
    _real_open = open
    monkeypatch.setattr(
        "builtins.open",
        lambda path, *a, **kw: __import__("io").StringIO(meminfo)
        if path == "/proc/meminfo"
        else _real_open(path, *a, **kw),
    )

    assert _should_repack(str(model), "auto") is False


def test_should_repack_auto_no_memavailable(monkeypatch, tmp_path):
    """Auto mode returns False when MemAvailable is missing."""
    model = tmp_path / "model.gguf"
    model.write_bytes(b"\x00")

    meminfo = "MemTotal:        8000000 kB\nMemFree:         4000000 kB\n"
    _real_open = open
    monkeypatch.setattr(
        "builtins.open",
        lambda path, *a, **kw: __import__("io").StringIO(meminfo)
        if path == "/proc/meminfo"
        else _real_open(path, *a, **kw),
    )

    assert _should_repack(str(model), "auto") is False


def test_should_repack_auto_commit_limit_exceeded(monkeypatch, tmp_path):
    """Auto mode disables repacking when commit headroom is too tight."""
    model = tmp_path / "model.gguf"
    model.write_bytes(b"\x00")

    # Pretend 500 MB model; plenty of MemAvailable but commit limit tight
    monkeypatch.setattr("os.path.getsize", lambda p: 500 * 1024 * 1024)
    meminfo = (
        "MemTotal:        8000000 kB\n"
        "MemAvailable:    4096000 kB\n"
        "CommitLimit:     7000000 kB\n"
        "Committed_AS:    6800000 kB\n"  # Only ~195 MB headroom
    )
    _real_open = open
    monkeypatch.setattr(
        "builtins.open",
        lambda path, *a, **kw: __import__("io").StringIO(meminfo)
        if path == "/proc/meminfo"
        else _real_open(path, *a, **kw),
    )

    assert _should_repack(str(model), "auto") is False


def test_should_repack_auto_read_error(monkeypatch):
    """Auto mode returns False on any exception."""
    monkeypatch.setattr("os.path.getsize", lambda p: 500 * 1024 * 1024)
    _real_open = open

    def fail_open(path, *a, **kw):
        if path == "/proc/meminfo":
            raise OSError("no such file")
        return _real_open(path, *a, **kw)

    monkeypatch.setattr("builtins.open", fail_open)

    assert _should_repack("/fake/model.gguf", "auto") is False


# -- start() --

def test_start_missing_binary(server, monkeypatch):
    """start() returns False when binary not found."""
    monkeypatch.setitem(config.CONFIG, "local_llama_cpp_path", "")
    monkeypatch.setattr("shutil.which", lambda name: None)
    monkeypatch.setitem(config.CONFIG, "local_model_path", "/tmp/test.gguf")
    assert server.start() is False


def test_start_missing_model(server, monkeypatch):
    """start() returns False when model file not found."""
    monkeypatch.setitem(config.CONFIG, "local_llama_cpp_path", "/usr/bin/llama-server")
    monkeypatch.setattr("os.path.isfile", lambda p: p == "/usr/bin/llama-server")
    monkeypatch.setitem(config.CONFIG, "local_model_path", "/nonexistent/model.gguf")
    assert server.start() is False


def test_start_success(server, local_config, monkeypatch):
    """start() spawns process and returns True on successful health check."""
    monkeypatch.setattr("os.path.isfile", lambda p: True)

    # Force repack=False so we can verify --no-repack in command
    monkeypatch.setitem(config.CONFIG, "local_repack", False)

    captured_cmds = []
    mock_proc = MagicMock()
    mock_proc.poll.return_value = None
    mock_proc.pid = 12345

    def fake_popen(cmd, **kw):
        captured_cmds.append(cmd)
        return mock_proc

    monkeypatch.setattr("subprocess.Popen", fake_popen)

    import httpx
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    monkeypatch.setattr("httpx.get", lambda url, timeout: mock_resp)

    assert server.start() is True
    assert server.running is True

    cmd = captured_cmds[0]
    assert "--parallel" in cmd
    par_idx = cmd.index("--parallel")
    assert cmd[par_idx + 1] == "1"
    assert "--no-repack" in cmd


def test_start_with_repack_enabled(server, local_config, monkeypatch):
    """start() omits --no-repack when repack is enabled."""
    monkeypatch.setattr("os.path.isfile", lambda p: True)
    monkeypatch.setitem(config.CONFIG, "local_repack", True)

    captured_cmds = []
    mock_proc = MagicMock()
    mock_proc.poll.return_value = None
    mock_proc.pid = 12345

    def fake_popen(cmd, **kw):
        captured_cmds.append(cmd)
        return mock_proc

    monkeypatch.setattr("subprocess.Popen", fake_popen)

    import httpx
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    monkeypatch.setattr("httpx.get", lambda url, timeout: mock_resp)

    assert server.start() is True
    assert "--no-repack" not in captured_cmds[0]


def test_start_health_timeout(server, local_config, monkeypatch):
    """start() stops and returns False when health check times out."""
    monkeypatch.setattr("os.path.isfile", lambda p: True)

    mock_proc = MagicMock()
    mock_proc.poll.return_value = None
    mock_proc.pid = 12345
    monkeypatch.setattr("subprocess.Popen", lambda cmd, **kw: mock_proc)

    import httpx
    monkeypatch.setattr("httpx.get", MagicMock(side_effect=httpx.ConnectError("refused")))

    monkeypatch.setitem(config.CONFIG, "local_startup_timeout", 2)

    assert server.start() is False
    assert server._proc is None


def test_start_process_exits_during_startup(server, local_config, monkeypatch):
    """start() returns False when process exits during health polling."""
    monkeypatch.setattr("os.path.isfile", lambda p: True)

    mock_proc = MagicMock()
    mock_proc.poll.return_value = 1
    mock_proc.returncode = 1
    monkeypatch.setattr("subprocess.Popen", lambda cmd, **kw: mock_proc)

    assert server.start() is False


# -- stop() --

def test_stop_delegates_to_platform(server, monkeypatch):
    """stop() uses platform.graceful_kill."""
    mock_proc = MagicMock()
    mock_proc.poll.return_value = None
    server._proc = mock_proc

    mock_platform = MagicMock()
    monkeypatch.setattr(
        "carpenter.platform.get_platform",
        lambda: mock_platform,
    )

    server.stop()

    mock_platform.graceful_kill.assert_called_once_with(mock_proc)
    assert server._proc is None


def test_stop_noop_when_not_running(server):
    """stop() does nothing when no process."""
    server.stop()


def test_stop_noop_when_already_dead(server):
    """stop() cleans up when process already exited."""
    mock_proc = MagicMock()
    mock_proc.poll.return_value = 0
    server._proc = mock_proc

    server.stop()
    assert server._proc is None


# -- health_check() --

def test_health_check_success(server, monkeypatch):
    """health_check returns True on 200 response."""
    monkeypatch.setitem(config.CONFIG, "local_server_host", "127.0.0.1")
    monkeypatch.setitem(config.CONFIG, "local_server_port", 8081)

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    monkeypatch.setattr("httpx.get", lambda url, timeout: mock_resp)

    assert server.health_check() is True


def test_health_check_connection_error(server, monkeypatch):
    """health_check returns False on connection error."""
    import httpx
    monkeypatch.setitem(config.CONFIG, "local_server_host", "127.0.0.1")
    monkeypatch.setitem(config.CONFIG, "local_server_port", 8081)
    monkeypatch.setattr("httpx.get", MagicMock(side_effect=httpx.ConnectError("refused")))

    assert server.health_check() is False


# -- running property --

def test_running_alive(server):
    """running returns True when process is alive."""
    mock_proc = MagicMock()
    mock_proc.poll.return_value = None
    server._proc = mock_proc
    assert server.running is True


def test_running_dead(server):
    """running returns False when process has exited."""
    mock_proc = MagicMock()
    mock_proc.poll.return_value = 0
    server._proc = mock_proc
    assert server.running is False


def test_running_no_process(server):
    """running returns False when no process started."""
    assert server.running is False


# -- singleton --

def test_get_inference_server_returns_singleton(monkeypatch):
    """get_inference_server returns the same instance on repeated calls."""
    import carpenter.inference as inf_mod
    monkeypatch.setattr(inf_mod, "_instance", None)

    s1 = get_inference_server()
    s2 = get_inference_server()
    assert s1 is s2


# -- process_registry integration --

class TestProcessRegistryIntegration:
    """Verify InferenceServer registers/unregisters with process_registry."""

    def test_start_registers_proc(self, server, local_config, monkeypatch):
        """start() registers the subprocess with process_registry."""
        monkeypatch.setattr("os.path.isfile", lambda p: True)
        monkeypatch.setitem(config.CONFIG, "local_repack", False)

        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_proc.pid = 99999
        monkeypatch.setattr("subprocess.Popen", lambda cmd, **kw: mock_proc)

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        monkeypatch.setattr("httpx.get", lambda url, timeout: mock_resp)

        from carpenter.executor import process_registry
        registered = set()
        monkeypatch.setattr(process_registry, "register", lambda p: registered.add(id(p)))

        assert server.start() is True
        assert id(mock_proc) in registered

    def test_stop_unregisters_proc(self, server, monkeypatch):
        """stop() unregisters the subprocess from process_registry."""
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        server._proc = mock_proc

        mock_platform = MagicMock()
        monkeypatch.setattr(
            "carpenter.platform.get_platform", lambda: mock_platform,
        )

        from carpenter.executor import process_registry
        unregistered = set()
        monkeypatch.setattr(process_registry, "unregister", lambda p: unregistered.add(id(p)))

        server.stop()
        assert id(mock_proc) in unregistered

    def test_stop_unregisters_already_dead_proc(self, server, monkeypatch):
        """stop() unregisters even when process already exited."""
        mock_proc = MagicMock()
        mock_proc.poll.return_value = 0
        server._proc = mock_proc

        from carpenter.executor import process_registry
        unregistered = set()
        monkeypatch.setattr(process_registry, "unregister", lambda p: unregistered.add(id(p)))

        server.stop()
        assert id(mock_proc) in unregistered
        assert server._proc is None
