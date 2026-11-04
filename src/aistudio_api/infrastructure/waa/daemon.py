"""Lifecycle-bound manager for the optional local WAA signer binary."""

from __future__ import annotations

import atexit
import contextlib
import ctypes
import os
import signal
import socket
import subprocess
import time
from pathlib import Path

from aistudio_api.infrastructure.gateway.model_defaults import get_active_waa_server
from aistudio_api.infrastructure.utils.logger import get_logger

logger = get_logger("waa.daemon")
_process: subprocess.Popen[bytes] | None = None


def _set_parent_death_signal() -> None:
    try:
        ctypes.CDLL(None).prctl(1, signal.SIGTERM)
    except Exception:
        pass


def _healthy(url: str) -> bool:
    import urllib.request

    try:
        with urllib.request.urlopen(f"{url.rstrip('/')}/health", timeout=0.5) as response:
            return response.status == 200
    except Exception:
        return False


def ensure_signer_ready() -> None:
    global _process
    server = get_active_waa_server()
    url = str(server.get("url") or "")
    if "127.0.0.1:8081" not in url and "localhost:8081" not in url:
        return
    if _healthy(url):
        return

    root = Path(__file__).resolve().parents[4]
    binary = root / "waa-signer" / "bin" / "waa-signer"
    if not binary.is_file() or not os.access(binary, os.X_OK):
        logger.warning("本地 WAA signer 未运行，且没有可执行文件: %s", binary)
        return

    command = [str(binary), "-listen", "127.0.0.1:8081"]
    proxy = str(server.get("proxy_url") or "").strip()
    if proxy and proxy.lower() != "direct":
        command.extend(["-proxy", proxy])
    try:
        _process = subprocess.Popen(
            command,
            cwd=str(binary.parent.parent),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            preexec_fn=_set_parent_death_signal,
        )
        for _ in range(30):
            time.sleep(0.1)
            if _healthy(url):
                logger.info("本地 WAA signer 已就绪: pid=%d", _process.pid)
                return
        logger.warning("本地 WAA signer 启动后未通过健康检查: %s", url)
    except Exception as exc:
        logger.warning("启动本地 WAA signer 失败: %s", exc)


def stop_signer_daemon() -> None:
    global _process
    process = _process
    _process = None
    if process is None:
        return
    with contextlib.suppress(Exception):
        process.terminate()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=1)


atexit.register(stop_signer_daemon)
