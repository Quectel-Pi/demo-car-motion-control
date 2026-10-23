"""Web 服务：静态页面 + JSON API（标准库实现，板端零依赖）。"""

from __future__ import annotations

import gzip
import json
import mimetypes
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse

from . import netinfo
from .config import WEB_DIR
from .driver import WheelDriver

#: 静态资源缓存：rel -> (原始字节, gzip 字节, etag, content-type)
#: 手机走 2.4G 热点时，three.min.js 623KB 经常传不完整；压缩后约 150KB 就好多了。
_ASSET_CACHE: Dict[str, Tuple[bytes, Optional[bytes], str, str]] = {}

#: 这些类型值得压缩（图片/字体本身已压缩，跳过）
_COMPRESSIBLE_MIME = (
    "text/",
    "application/javascript",
    "application/json",
    "image/svg+xml",
)

_MOBILE_UA_KEYS = ("android", "iphone", "ipad", "ipod", "mobile", "harmonyos", "miui", "huawei")


def _load_asset(path, rel: str):
    """读取静态资源并缓存（含 gzip 压缩结果 + ETag）。"""

    try:
        st = path.stat()
    except OSError:
        return b"", None, '"0"', "application/octet-stream"
    etag = '"{:x}-{:x}"'.format(int(st.st_mtime), st.st_size)
    cached = _ASSET_CACHE.get(rel)
    if cached is not None and cached[2] == etag:
        return cached

    raw = path.read_bytes()
    ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    if ctype.startswith("text/") or ctype in ("application/javascript", "image/svg+xml"):
        ctype += "; charset=utf-8"
    gz = None
    if ctype.startswith(_COMPRESSIBLE_MIME):
        try:
            gz = gzip.compress(raw, 6)
            if len(gz) >= len(raw):        # 压不动就别压
                gz = None
        except Exception:
            gz = None
    entry = (raw, gz, etag, ctype)
    _ASSET_CACHE[rel] = entry
    return entry


def _is_mobile(user_agent: str) -> bool:
    ua = (user_agent or "").lower()
    return any(key in ua for key in _MOBILE_UA_KEYS)


def _hostname() -> str:
    try:
        import socket

        return socket.gethostname()
    except Exception:
        return "-"


mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("text/css", ".css")
mimetypes.add_type("image/svg+xml", ".svg")

_STATIC_WHITELIST = {
    "/": "index.html",
    "/index.html": "index.html",
    #: 手机精简版：手机访问 "/" 时自动走这一页；桌面仍进完整版
    "/m.html": "mobile.html",
    "/mobile.js": "mobile.js",
    #: 深色完整版：与 "/" 共用同一份 index.html，靠页面头部的路径判断换成 style-v2.css
    "/dark": "index.html",
    "/dark.html": "index.html",
    #: 旧路径，保留兼容（老文档/板端日志里出现过，不要删）
    "/index-v2.html": "index.html",
    "/app.js": "app.js",
    "/car3d.js": "car3d.js",
    "/style.css": "style.css",
    "/style-v2.css": "style-v2.css",
    "/vendor/three.min.js": "vendor/three.min.js",
}


class WheelHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: Tuple[str, int], driver: WheelDriver, web_dir: Path) -> None:
        super().__init__(address, WheelRequestHandler)
        self.driver = driver
        self.web_dir = Path(web_dir)


class WheelRequestHandler(BaseHTTPRequestHandler):
    # 关闭 Nagle：JSON 响应分两次 write，开着会让小包等延迟 ACK
    disable_nagle_algorithm = True
    server_version = "WheelControlDemo/1.0"
    protocol_version = "HTTP/1.1"

    # ------------------------------------------------------------------ 基础工具
    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003 - 保持基类签名
        # 20Hz 轮询会把终端刷满，这里只保留错误日志
        return

    # 客户端超时/刷新页面会主动断连，默认实现会往 stderr 打完整 traceback →
    # systemd 再写 journald（每次几毫秒），既拖慢请求又把日志刷爆。这里静默处理。
    def handle_one_request(self) -> None:
        try:
            super().handle_one_request()
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError, TimeoutError):
            self.close_connection = True

    def finish(self) -> None:
        try:
            super().finish()
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError, OSError):
            pass

    @property
    def app(self) -> WheelHTTPServer:
        return self.server  # type: ignore[return-value]

    def _send_json(self, payload: Dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def _send_error_json(self, message: str, status: int = 400) -> None:
        self._send_json({"ok": False, "error": message}, status)

    def _read_json(self) -> Optional[Dict[str, Any]]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            return None
        return data if isinstance(data, dict) else None

    def _send_file(self, filename: str) -> None:
        rel = _STATIC_WHITELIST.get(filename)
        if rel is None:
            self._send_error_json("unknown static asset", 404)
            return
        # 同一个地址 "/"：手机自动给精简版，桌面给完整版
        if filename in ("/", "/index.html") and _is_mobile(self.headers.get("User-Agent", "")):
            rel = "mobile.html"
        path = self.app.web_dir / rel
        if not path.is_file():
            self._send_error_json(f"missing frontend file: {rel}", 404)
            return

        raw, gz, etag, ctype = _load_asset(path, rel)

        # 缓存校验：手机弱网重进页面时省掉整包传输
        if self.headers.get("If-None-Match") == etag:
            self.send_response(HTTPStatus.NOT_MODIFIED)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            return

        accepts_gzip = "gzip" in (self.headers.get("Accept-Encoding") or "").lower()
        body = gz if (accepts_gzip and gz is not None) else raw

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("ETag", etag)
        # HTML 不缓存（改名/换构建后立刻生效），其余静态资源缓存 10 分钟
        if ctype.startswith("text/html"):
            self.send_header("Cache-Control", "no-cache")
        else:
            self.send_header("Cache-Control", "no-cache")
        if accepts_gzip and gz is not None:
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Vary", "Accept-Encoding")
        self.end_headers()
        self.wfile.write(body)

    # ------------------------------------------------------------------ GET
    def do_GET(self) -> None:  # noqa: N802 - 基类命名约定
        path = urlparse(self.path).path
        self.app.driver.note_client()

        if path in _STATIC_WHITELIST:
            self._send_file(path)
            return
        if path == "/api/status":
            self._send_json({
                "ok": True,
                "status": self.app.driver.status(),
                "server_time": time.time(),
                "net": {
                    "hostname": _hostname(),
                    "addresses": [
                        {"iface": iface, "url": url}
                        for iface, url in netinfo.access_urls(self.app.server_address[1])
                    ],
                },
            })
            return
        if path == "/api/demo/list":
            self._send_json({"ok": True, "sequences": self.app.driver.demo.list_sequences()})
            return
        if path == "/api/health":
            self._send_json({"ok": True, "pid": True, "uptime_s": self.app.driver.status()["uptime_s"]})
            return
        if path == "/favicon.ico":
            self.send_response(HTTPStatus.NO_CONTENT)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self._send_error_json(f"unknown path: {path}", 404)

    def _client_log(self, payload: Any) -> None:
        """前端把 JS 异常发到这里落盘，便于排查（20 万字节自动轮转）。"""
        import datetime
        import json as _json
        import os
        path = os.path.expanduser("~/wheel-client-errors.log")
        try:
            text = _json.dumps(payload, ensure_ascii=False)[:4000]
        except Exception:                       # noqa: BLE001 - 兜底
            text = "<unserializable>"
        try:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(datetime.datetime.now().strftime("%m-%d %H:%M:%S") + " " + text + "\n")
            if os.path.getsize(path) > 200_000:
                os.replace(path, path + ".old")
        except Exception:                       # noqa: BLE001 - 日志失败不影响主流程
            pass
        self._send_json({"ok": True})

    # ------------------------------------------------------------------ POST
    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        self.app.driver.note_client()
        data = self._read_json()
        if data is None:
            self._send_error_json("request body is not valid JSON")
            return
        driver = self.app.driver

        try:
            if path == "/api/client_log":
                self._client_log(data)
                return
            if path == "/api/target":
                status = driver.set_target(
                    angle=data.get("angle", 0),
                    velocity=data.get("velocity", 0),
                    rot=data.get("rot", 0),
                    drift=bool(data.get("drift", False)),
                )
                self._send_json({"ok": True, "status": status})
                return
            if path == "/api/raw":
                wheels = data.get("wheels")
                if not isinstance(wheels, list) or not wheels:
                    self._send_error_json("wheels must be an array of 4 numbers")
                    return
                try:
                    status = driver.set_raw(wheels)
                except ValueError as exc:
                    # 出厂固件不支持单轮直控：如实返回 400，不假装下发成功
                    self._send_error_json(str(exc), 400)
                    return
                self._send_json({"ok": True, "status": status})
                return
            if path == "/api/imu/measure_turn":
                self._send_json({
                    "ok": True,
                    "result": driver.measure_turn(
                        rot=int(data.get("rot", 30)),
                        seconds=float(data.get("seconds", 1.0)),
                    ),
                    "status": driver.status(),
                })
                return
            if path == "/api/imu/calib":
                self._send_json({"ok": True, "status": driver.imu_calibrate()})
                return
            if path == "/api/imu/zero":
                self._send_json({"ok": True, "status": driver.imu_zero()})
                return
            if path == "/api/stop":
                self._send_json({"ok": True, "status": driver.stop()})
                return
            if path == "/api/estop":
                self._send_json({"ok": True, "status": driver.estop(), "message": "emergency stop triggered"})
                return
            if path == "/api/demo/start":
                name = str(data.get("name", "")).strip()
                if not name:
                    self._send_error_json("missing demo name")
                    return
                self._send_json({"ok": True, "status": driver.start_demo(name, dry_run=bool(data.get("dry_run", False)))})
                return
            if path == "/api/demo/stop":
                self._send_json({"ok": True, "status": driver.stop_demo()})
                return
            if path == "/api/demo/pause":
                driver.demo.pause()
                self._send_json({"ok": True, "status": driver.status()})
                return
            if path == "/api/demo/resume":
                driver.demo.resume()
                self._send_json({"ok": True, "status": driver.status()})
                return
        except KeyError as exc:
            self._send_error_json(f"bad parameter: {exc}")
            return
        except Exception as exc:  # pragma: no cover - 防御性
            self._send_error_json(f"request failed: {type(exc).__name__}: {exc}", 500)
            return

        self._send_error_json(f"unknown API: {path}", 404)


def create_server(driver: WheelDriver, host: str, port: int, web_dir: Optional[Path] = None) -> WheelHTTPServer:
    return WheelHTTPServer((host, port), driver, web_dir or WEB_DIR)


def serve(driver: WheelDriver, host: str, port: int, web_dir: Optional[Path] = None) -> WheelHTTPServer:
    server = create_server(driver, host, port, web_dir)
    thread = threading.Thread(target=server.serve_forever, name="wheel-http", daemon=True)
    thread.start()
    return server
