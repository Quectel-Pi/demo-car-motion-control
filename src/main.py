#!/usr/bin/env python3
"""车轮控制 demo 入口（幻尔树莓派5扩展板 A 款方案）。

用法：
    python3 src/main.py                          # 自动探测串口，找不到则仿真模式
    python3 src/main.py --sim                    # 强制仿真（无硬件演示）
    python3 src/main.py --serial /dev/ttyS1      # 指定扩展板串口（M2 板载 UART）
    python3 src/main.py --serial COM5            # Windows 本地调试
    python3 src/main.py --deadzone 20            # 标定开环死区（%）
"""

from __future__ import annotations

import argparse
import signal
import socket
import sys
import threading
import time
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parent
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from wheelctl import netinfo  # noqa: E402
from wheelctl.config import PROJECT_ROOT, WEB_DIR, load_config  # noqa: E402
from wheelctl.driver import WheelDriver  # noqa: E402
from wheelctl.server import create_server  # noqa: E402

BANNER = """
========================================================
  车轮控制 Demo · Mecanum Wheel Control
  主控: Quectel Pi M2   执行: 幻尔树莓派5扩展板 A 款
========================================================
"""


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="车轮控制 demo（Web 控制 + 演示序列）")
    parser.add_argument("--config", default=None, help="配置文件路径，默认项目根目录 config.json")
    parser.add_argument("--serial", default=None, help="串口设备，如 /dev/ttyS1；auto=自动探测")
    parser.add_argument("--baud", type=int, default=None, help="波特率，默认 1000000（板载固件固定 1 Mbps）")
    parser.add_argument("--sim", action="store_true", help="强制仿真模式（不打开串口）")
    parser.add_argument("--deadzone", type=float, default=None, help="开环死区补偿百分比，默认 15")
    parser.add_argument("--max-speed", type=float, default=None, help="100%% 占空对应的轮速 mm/s（仅用于估算显示）")
    parser.add_argument("--port", type=int, default=None, help="HTTP 端口，默认 8080")
    parser.add_argument("--host", default=None, help="HTTP 监听地址，默认 0.0.0.0")
    return parser


def local_ip() -> str:
    """取本机在局域网中的地址，用于打印访问链接。"""

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except Exception:
        return "127.0.0.1"


def main(argv=None) -> int:
    args = build_arg_parser().parse_args(argv)
    cfg = load_config(args.config)

    if args.serial:
        cfg["serial"]["port"] = args.serial
    if args.baud:
        cfg["serial"]["baud"] = args.baud
    if args.sim:
        cfg["sim"]["force"] = True
    if args.deadzone is not None:
        cfg["board"]["deadzone_pct"] = args.deadzone
    if args.max_speed:
        cfg["board"]["max_speed_mm_s"] = args.max_speed
    if args.port:
        cfg["web"]["port"] = args.port
    if args.host:
        cfg["web"]["host"] = args.host

    host = cfg["web"]["host"]
    port = int(cfg["web"]["port"])

    print(BANNER.strip())
    driver = WheelDriver(cfg)
    driver.start()
    time.sleep(0.4)

    status = driver.status()
    link = status["link"]
    board = status["board"]
    telemetry = status["telemetry"]

    mode_text = "仿真模式（未连接扩展板，动作为模拟）" if link["mode"] == "sim" else f"串口 {link['port']}"
    handshake_text = {
        "battery": "已收到 $bat 电池应答",
        "reply": "已收到扩展板应答（$pong/$hb/$ready）",
        "sim": "仿真",
        "no-response": "无应答（检查接线、波特率与固件）",
        "failed": "失败",
    }.get(link["handshake"], link["handshake"])

    print(f"  链路     : {mode_text}")
    print(f"  握手     : {handshake_text}")
    print(f"  扩展板   : {board['motor_label']}")
    print(f"  协议     : {board['protocol']}")
    print(f"  能力     : 任意方向 · 可边走边转 · 支持漂移 · 支持单轮点检 · 无编码器（轮速为估算）")
    print(f"  死区补偿 : {board['deadzone_pct']:.0f}%（可 --deadzone 调整，0 = 关闭）")
    print(f"  安全     : 板子未声明指令超时，进程被强杀时车会保持最后占空 → 现场保留电源开关")
    sensors = status.get("sensors") or {}
    volt = (status.get("battery") or {}).get("voltage")
    mv = sensors.get("battery_mv")
    print(f"  遥测     : SYS 电池上报 {volt if volt is not None else '-'} V"
          + (f"（{mv} mV）" if mv is not None else "") + " · 板子每 ~200ms 推一帧")
    if link.get("error"):
        print(f"  链路告警 : {link['error']}")
    print(f"  演示序列 : {len(driver.demo.list_sequences())} 个")

    # 端口：优先用配置/参数给的端口，被占用时按 fallback_ports 依次尝试
    candidates = [port]
    for extra in cfg["web"].get("fallback_ports", []) or []:
        extra = int(extra)
        if extra not in candidates:
            candidates.append(extra)
    if args.port:
        # 显式 --port 时只重试它自己，避免"悄悄换到别的端口"让人以为没生效
        candidates = [port]

    server = None
    bound_port = port
    last_exc = None
    for idx, candidate in enumerate(candidates):
        try:
            server = create_server(driver, host, candidate, WEB_DIR)
            bound_port = candidate
            break
        except OSError as exc:
            last_exc = exc
            if idx + 1 < len(candidates):
                print(f"  [注意] 端口 {candidate} 不可用（{exc.strerror or exc}），尝试 {candidates[idx + 1]} ...")
                continue
            break

    if server is None:
        driver.shutdown()
        print(f"\n[ERROR] 无法监听 {host}:{port} —— {last_exc}")
        errno_ = getattr(last_exc, "errno", None)
        if errno_ in (98, 48, 10048, 10013) or isinstance(last_exc, PermissionError):
            print(f"        端口 {port} 已被占用或被系统拒绝（板端可能有其它 demo 常驻）。")
        print(f"        换端口重试：./run.sh --port 8090")
        return 2

    port = bound_port
    try:
        host_name = socket.gethostname()
    except Exception:
        host_name = "-"
    print(f"  主机名   : {host_name}")
    print(netinfo.format_access_block(port), end="")
    print(f"  本机自测 : http://127.0.0.1:{port}/")
    url_file = Path.home() / ".wheel_urls"
    try:
        netinfo.save_urls(port, url_file)
        print(f"  地址文件 : {url_file}   （cat 一下就能看到上面这些地址）")
    except Exception:
        pass
    print("  停止服务 : Ctrl+C")
    print("========================================================")

    stop_event = threading.Event()

    def _handle_signal(signum, frame):  # noqa: ARG001
        print("\n收到退出信号，正在停车并关闭服务 ...")
        stop_event.set()

    signal.signal(signal.SIGINT, _handle_signal)
    try:
        signal.signal(signal.SIGTERM, _handle_signal)
    except (ValueError, AttributeError):  # pragma: no cover
        pass

    thread = threading.Thread(target=server.serve_forever, name="wheel-http", daemon=True)
    thread.start()

    def _watch_addresses() -> None:
        """IP 变了（Wi-Fi 重连）就把新地址再打一遍，省得去翻 ip a。"""

        last = [url for _, url in netinfo.access_urls(port)]
        while not stop_event.wait(20):
            current = [url for _, url in netinfo.access_urls(port)]
            if current != last:
                last = current
                print("\n[网络变化] 访问地址已更新：", flush=True)
                print(netinfo.format_access_block(port), end="", flush=True)
                try:
                    netinfo.save_urls(port, Path.home() / ".wheel_urls")
                except Exception:
                    pass

    threading.Thread(target=_watch_addresses, name="addr-watch", daemon=True).start()

    try:
        while not stop_event.is_set():
            stop_event.wait(0.5)
    finally:
        driver.stop()
        server.shutdown()
        server.server_close()
        driver.shutdown()
        print("已停止：电机停转（A|8|$）、服务关闭。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
