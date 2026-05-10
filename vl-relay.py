#!/usr/bin/env python3
"""
On-demand llama.cpp model relay with automatic VRAM offload.

Starts the backend llama-server only when a request arrives,
and automatically stops it after an idle timeout to free VRAM.

This eliminates GPU memory waste when the model is not in use,
while maintaining full OpenAI-compatible API semantics.
"""
import http.server
import json
import subprocess
import urllib.request
import urllib.error
import time
import threading
import signal
import os
import sys
import atexit
import ctypes

# ---------------------------------------------------------------------------
# Configuration — override any via environment variables
# ---------------------------------------------------------------------------
MODEL       = os.environ.get("VL_MODEL",
                os.path.expanduser("~/AI-Server/model/qwen3-vl/Huihui-Qwen3-VL-4B-Instruct-abliterated-Q4_K_M.gguf"))
MMPROJ      = os.environ.get("VL_MMPROJ",
                os.path.expanduser("~/AI-Server/model/qwen3-vl/mmproj-F16.gguf"))
LLAMA_BIN   = os.environ.get("LLAMA_SERVER",
                os.path.expanduser("~/AI-Server/llama.cpp/build/bin/llama-server"))
LISTEN_PORT = int(os.environ.get("VL_PORT", "8083"))
INT_PORT    = int(os.environ.get("VL_INTERNAL_PORT", "8084"))
IDLE_TIMEOUT = int(os.environ.get("VL_IDLE_TIMEOUT", "300"))   # seconds
POLL_INTERVAL = int(os.environ.get("VL_POLL_INTERVAL", "15"))
CTX_SIZE    = int(os.environ.get("VL_CTX_SIZE", "8192"))
N_GPU_LAYERS = int(os.environ.get("VL_N_GPU_LAYERS", "99"))
PARALLEL    = int(os.environ.get("VL_PARALLEL", "1"))
CACHE_K     = os.environ.get("VL_CACHE_K", "q8_0")
CACHE_V     = os.environ.get("VL_CACHE_V", "q8_0")

# ---------------------------------------------------------------------------
# PDEATHSIG — child dies when relay dies (even SIGKILL)
# ---------------------------------------------------------------------------
LIBC = ctypes.CDLL("libc.so.6")
PR_SET_PDEATHSIG = 1

def _set_pdeathsig():
    LIBC.prctl(PR_SET_PDEATHSIG, signal.SIGTERM, 0, 0, 0)

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
_proc = None         # subprocess handle
_last_req = 0.0      # timestamp of last request
_lock = threading.Lock()

def _log(msg: str):
    print(f"[vl-relay] {msg}", flush=True)

# ---------------------------------------------------------------------------
# Backend lifecycle
# ---------------------------------------------------------------------------
def _start_backend() -> bool:
    global _proc
    if _proc is not None and _proc.poll() is None:
        return True          # already running

    _log("Starting llama-server (this loads ~3.8GB VRAM)...")
    try:
        _proc = subprocess.Popen(
            [LLAMA_BIN,
             "--model", MODEL,
             "--mmproj", MMPROJ,
             "--port", str(INT_PORT),
             "--host", "127.0.0.1",
             "--n-gpu-layers", str(N_GPU_LAYERS),
             "--ctx-size", str(CTX_SIZE),
             "--no-warmup",
             "--cache-type-k", CACHE_K,
             "--cache-type-v", CACHE_V,
             "--cont-batching",
             "--parallel", str(PARALLEL)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            preexec_fn=_set_pdeathsig,
        )
        # Wait until healthy (up to 30s)
        for _ in range(60):
            if _proc.poll() is not None:
                _log(f"Backend exited early (code {_proc.returncode})")
                _proc = None
                return False
            try:
                urllib.request.urlopen(
                    f"http://127.0.0.1:{INT_PORT}/health", timeout=1)
                _log("Backend ready")
                return True
            except Exception:
                time.sleep(0.5)
        _log("Backend failed to become ready within 30s")
        _stop_backend()
        return False
    except Exception as e:
        _log(f"Failed to start backend: {e}")
        _proc = None
        return False

def _stop_backend():
    global _proc
    if _proc is None:
        return
    p = _proc
    _proc = None
    try:
        p.terminate()
        p.wait(timeout=5)
    except Exception:
        p.kill()
    _log("Backend stopped — VRAM released")

def _idle_monitor():
    """Periodically check if the backend has been idle too long."""
    while True:
        time.sleep(POLL_INTERVAL)
        with _lock:
            if _proc is not None and _proc.poll() is None:
                idle = time.time() - _last_req
                if idle > IDLE_TIMEOUT:
                    _log(f"Idle {idle:.0f}s — unloading")
                    _stop_backend()

# ---------------------------------------------------------------------------
# HTTP handler — transparent proxy to internal backend
# ---------------------------------------------------------------------------
class _Handler(http.server.BaseHTTPRequestHandler):
    # --- Forward all methods transparently ---
    def do_GET(self):     self._proxy("GET")
    def do_POST(self):    self._proxy("POST")
    def do_PUT(self):     self._proxy("PUT")
    def do_DELETE(self):  self._proxy("DELETE")
    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()

    def _proxy(self, method: str):
        global _last_req
        content_len = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_len)

        with _lock:
            _last_req = time.time()
            if not _start_backend():
                self.send_error(503, "Backend unavailable")
                return

        try:
            upstream = f"http://127.0.0.1:{INT_PORT}{self.path}"
            req = urllib.request.Request(
                upstream,
                data=body if method in ("POST", "PUT") else None,
                headers={k: v for k, v in self.headers.items()
                         if k.lower() not in ("host", "content-length",
                                              "transfer-encoding", "connection")},
                method=method,
            )
            with urllib.request.urlopen(req, timeout=300) as resp:
                data = resp.read()

            self.send_response(resp.status)
            for k, v in resp.headers.items():
                if k.lower() not in ("transfer-encoding", "connection",
                                     "content-encoding"):
                    self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)
        except urllib.error.HTTPError as e:
            self.send_response(e.code)
            self.end_headers()
            self.wfile.write(e.read())
        except Exception as e:
            _log(f"Proxy error {self.path}: {e}")
            self.send_error(502, f"Upstream error: {e}")

    def log_message(self, fmt, *args):
        pass  # suppress default HTTP log

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def _cleanup():
    _stop_backend()

if __name__ == "__main__":
    atexit.register(_cleanup)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    signal.signal(signal.SIGINT, lambda *_: sys.exit(0))

    # Quick port check
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{LISTEN_PORT}/health",
                               timeout=1)
        _log(f"Port {LISTEN_PORT} already in use — exiting")
        sys.exit(1)
    except Exception:
        pass

    # Start idle monitor
    threading.Thread(target=_idle_monitor, daemon=True).start()

    server = http.server.HTTPServer(("127.0.0.1", LISTEN_PORT), _Handler)
    _log(f"Listening on :{LISTEN_PORT}, idle timeout {IDLE_TIMEOUT}s")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        _stop_backend()
