#!/usr/bin/env python3
"""UART 自检工具：先用回环线验证 M2 的板载 UART，再接扩展板。

两种用法：

1) 回环自检（不接驱动板）：把该 UART 的 TX 与 RX 用一根杜邦线短接，然后
       python3 test_tools/uart_loopback.py -p /dev/ttyS1
   收发一致 → UART 引脚/驱动/波特率都正常。

2) 只读监听（已接扩展板）：看串口上有没有帧（本板 1 Mbps 8N1，十六进制打印）
       python3 test_tools/uart_loopback.py -p /dev/ttyS1 --watch
   （回环自检本身与波特率无关，收发用的是同一个速率）

背景：M2 的 40pin 第 8 脚 = UART1_TXD(GPIO2_B0)，第 10 脚 = UART1_RXD(GPIO2_B1)，
对应 /dev/ttyS1；GND 用 6/9/14/20/30/34/39 任一。
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from wheelctl.transport import SerialTransport, TransportError  # noqa: E402


def open_port(port: str, baud: int, timeout: float = 0.05) -> SerialTransport:
    transport = SerialTransport(port, baud)
    try:
        transport.open()
    except TransportError as exc:
        raise SystemExit(f"打不开 {port}: {exc}")
    return transport


def loopback_test(port: str, baud: int) -> int:
    transport = open_port(port, baud)
    print(f"已打开 {port} @ {baud}（{transport.backend}）")
    transport.read_bytes()
    time.sleep(0.1)
    transport.read_bytes()

    patterns = [b"PING\r\n", bytes(range(32, 96)), bytes(range(0, 64))]
    ok = 0
    try:
        for payload in patterns:
            _write_raw(transport, payload)
            got = _read_for(transport, timeout=0.8)
            mark = "OK" if got == payload else "MISMATCH"
            print(f"  发送 {len(payload)}B -> 收到 {len(got)}B  {mark}")
            if got != payload:
                print(f"    发: {payload[:32]!r}")
                print(f"    收: {got[:32]!r}")
            else:
                ok += 1
    finally:
        transport.close()

    print()
    if ok == len(patterns):
        print("回环自检通过 ✅  UART 引脚、驱动、波特率都正常，可以接扩展板了")
        return 0
    print("回环自检失败 ❌")
    print("  可能原因：")
    print("    1) TX/RX 没有短接（回环线没插好）")
    print("    2) 该 ttyS 不是 40pin 上的 UART（试试 /dev/ttyS4）")
    print("    3) 串口被 console/getty 占用（本机 ttyS1/ttyS4 的 getty 已禁用）")
    print("    4) 权限不足（需要 dialout 组）")
    return 1


def _write_raw(transport: SerialTransport, payload: bytes) -> None:
    if transport._impl is not None:  # pyserial 分支
        transport._impl.write(payload)
        transport._impl.flush()
    elif transport._fd is not None:
        os.write(transport._fd, payload)
    else:
        raise SystemExit("串口未打开")


def _read_for(transport: SerialTransport, timeout: float = 0.8) -> bytes:
    buf = b""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        chunk = transport.read_bytes()
        if chunk:
            buf += chunk
            if len(buf) >= 1:
                break
        else:
            time.sleep(0.01)
    # 再多收一会儿，避免只收到第一个字节
    tail_deadline = time.monotonic() + 0.15
    while time.monotonic() < tail_deadline:
        chunk = transport.read_bytes()
        if chunk:
            buf += chunk
        else:
            time.sleep(0.01)
    return buf


def watch(port: str, baud: int, seconds: float) -> int:
    transport = open_port(port, baud)
    print(f"监听 {port} @ {baud}，{seconds:.0f} 秒（Ctrl+C 结束）")
    deadline = time.monotonic() + seconds
    total = 0
    try:
        while time.monotonic() < deadline:
            chunk = transport.read_bytes()
            if chunk:
                total += len(chunk)
                sys.stdout.write(chunk.hex(" ") + "\n")
                sys.stdout.flush()
            else:
                time.sleep(0.01)
    except KeyboardInterrupt:
        pass
    finally:
        transport.close()
    print(f"\n共收到 {total} 字节")
    return 0 if total else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="M2 UART 自检（回环 / 监听）")
    parser.add_argument("-p", "--port", default="/dev/ttyS1", help="串口设备，默认 /dev/ttyS1")
    parser.add_argument("-b", "--baud", type=int, default=1000000,
                        help="波特率，默认 1000000（扩展板 1 Mbps）")
    parser.add_argument("--watch", action="store_true", help="只监听不发送")
    parser.add_argument("--seconds", type=float, default=5.0, help="监听时长")
    args = parser.parse_args(argv)

    if not os.path.exists(args.port):
        print(f"设备不存在: {args.port}")
        print("本机板载 UART: /dev/ttyS1（40pin 8/10 脚）、/dev/ttyS4")
        return 2

    return watch(args.port, args.baud, args.seconds) if args.watch else loopback_test(args.port, args.baud)


if __name__ == "__main__":
    raise SystemExit(main())
