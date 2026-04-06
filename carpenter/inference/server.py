"""InferenceServer — manages the llama-server process lifecycle.

Starts llama-server as a subprocess, polls /health until ready,
and stops it via the platform's graceful_kill.
"""

import logging
import os
import shutil
import subprocess
import time

import httpx

from .. import config

logger = logging.getLogger(__name__)

# Repacking roughly doubles the model file size in RAM.
# This headroom (MB) is reserved for KV cache + OS + other processes.
_REPACK_HEADROOM_MB = 1024


def _should_repack(model_path: str, repack_setting: bool | str) -> bool:
    """Decide whether to enable weight repacking.

    Args:
        model_path: Path to the GGUF model file.
        repack_setting: True, False, or "auto".

    Returns:
        True if repacking should be enabled.
    """
    if isinstance(repack_setting, bool):
        return repack_setting

    # "auto" — check available memory AND commit headroom
    try:
        meminfo = {}
        with open("/proc/meminfo") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2:
                    meminfo[parts[0].rstrip(":")] = int(parts[1])

        avail_kb = meminfo.get("MemAvailable")
        if avail_kb is None:
            return False  # Can't determine, be safe
        avail_mb = avail_kb // 1024

        model_size_mb = os.path.getsize(model_path) // (1024 * 1024)
        # Repacking needs ~model_size extra, plus headroom for KV + OS
        needed_mb = model_size_mb + _REPACK_HEADROOM_MB

        # Check physical memory
        can_repack = avail_mb >= needed_mb

        # Also check commit headroom (overcommit_memory=0 can deny
        # allocations even when physical RAM looks sufficient)
        commit_limit = meminfo.get("CommitLimit")
        committed = meminfo.get("Committed_AS")
        if commit_limit is not None and committed is not None:
            commit_headroom_mb = (commit_limit - committed) // 1024
            if commit_headroom_mb < needed_mb:
                can_repack = False
                logger.info(
                    "Repack auto-detect: commit headroom=%dMB < needed=%dMB → disabled",
                    commit_headroom_mb, needed_mb,
                )
                return False

        logger.info(
            "Repack auto-detect: available=%dMB, model=%dMB, needed=%dMB → %s",
            avail_mb, model_size_mb, needed_mb,
            "enabled" if can_repack else "disabled",
        )
        return can_repack
    except (OSError, ValueError, KeyError) as _exc:
        logger.info("Repack auto-detect failed, defaulting to disabled")
        return False


class InferenceServer:
    """Manages a local llama-server process."""

    def __init__(self):
        self._proc: subprocess.Popen | None = None

    @property
    def running(self) -> bool:
        """Check whether the llama-server process is alive."""
        return self._proc is not None and self._proc.poll() is None

    def start(self) -> bool:
        """Start the llama-server process.

        Reads configuration, resolves the binary path, builds the command,
        spawns the process, and polls /health until ready.

        Returns:
            True if the server is healthy and ready, False on failure.
        """
        if self.running:
            logger.info("Inference server already running (pid %d)", self._proc.pid)
            return True

        # Resolve binary
        binary = config.CONFIG.get("local_llama_cpp_path", "")
        if not binary:
            binary = shutil.which("llama-server")
        if not binary or not os.path.isfile(binary):
            logger.error("llama-server binary not found (config: local_llama_cpp_path, PATH)")
            return False

        # Resolve model
        model_path = config.CONFIG.get("local_model_path", "")
        if not model_path or not os.path.isfile(model_path):
            logger.error("GGUF model file not found: %s", model_path)
            return False

        host = config.CONFIG.get("local_server_host", "127.0.0.1")
        port = config.CONFIG.get("local_server_port", 8081)
        ctx_size = config.CONFIG.get("local_context_size", 8192)
        gpu_layers = config.CONFIG.get("local_gpu_layers", 0)
        parallel = config.CONFIG.get("local_parallel", 1)
        repack_setting = config.CONFIG.get("local_repack", "auto")
        extra_args = config.CONFIG.get("local_server_args", [])
        timeout = config.CONFIG.get("local_startup_timeout", 120)

        repack = _should_repack(model_path, repack_setting)

        cmd = [
            binary,
            "--model", model_path,
            "--host", str(host),
            "--port", str(port),
            "--ctx-size", str(ctx_size),
            "--parallel", str(parallel),
        ]
        if not repack:
            cmd.append("--no-repack")
        if gpu_layers > 0:
            cmd.extend(["--n-gpu-layers", str(gpu_layers)])
        if extra_args:
            cmd.extend(extra_args)

        logger.info("Starting inference server: %s", " ".join(cmd))
        try:
            self._proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
        except OSError as e:
            logger.error("Failed to start inference server: %s", e)
            return False

        # Poll /health until ready
        health_url = f"http://{host}:{port}/health"
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._proc.poll() is not None:
                # Read any output for diagnostics
                output = ""
                try:
                    output = self._proc.stdout.read().decode(errors="replace")[-2000:]
                except (OSError, ValueError) as _exc:
                    pass
                logger.error(
                    "Inference server exited during startup (code %d): %s",
                    self._proc.returncode, output,
                )
                self._proc = None
                return False
            if self.health_check():
                logger.info("Inference server ready (pid %d)", self._proc.pid)
                from ..executor import process_registry
                process_registry.register(self._proc)
                return True
            time.sleep(config.get_config("inference_server_health_check_interval", 1))

        logger.error("Inference server health check timed out after %ds", timeout)
        self.stop()
        return False

    def stop(self) -> None:
        """Stop the inference server process.

        Uses platform graceful_kill for SIGTERM/SIGKILL escalation.
        """
        if self._proc is None:
            return

        from ..executor import process_registry

        if self._proc.poll() is not None:
            process_registry.unregister(self._proc)
            self._proc = None
            return

        logger.info("Stopping inference server (pid %d)", self._proc.pid)
        process_registry.unregister(self._proc)
        try:
            from ..platform import get_platform
            get_platform().graceful_kill(self._proc)
        except (ImportError, OSError, RuntimeError) as _exc:
            # Fallback: direct terminate/kill
            try:
                self._proc.terminate()
                self._proc.wait(timeout=5)
            except OSError as _exc:
                try:
                    self._proc.kill()
                except OSError:
                    pass
        self._proc = None

    def health_check(self) -> bool:
        """Check if the server is healthy via GET /health.

        Returns:
            True if the server responds with 200, False otherwise.
        """
        host = config.CONFIG.get("local_server_host", "127.0.0.1")
        port = config.CONFIG.get("local_server_port", 8081)
        url = f"http://{host}:{port}/health"
        try:
            resp = httpx.get(url, timeout=5.0)
            return resp.status_code == 200
        except (httpx.ConnectError, httpx.TimeoutException, OSError):
            return False
