#!/usr/bin/env python3
"""扩展板串口诊断 + 免 Web 的试动作工具（真机用）。

这块板子（幻尔树莓派5扩展板 A 款）**不需要先发指令**：它每 ~200ms 主动推一帧 SYS（电池电压），
所以"只读探测"就是最安全的接线自检。

常用：
    python3 test_tools/serial_probe.py --scan
    python3 test_tools/serial_probe.py                    # 自动探测 + 只读看电池（车不会动）
    python3 test_tools/serial_probe.py -p /dev/ttyS1      # 指定端口
    python3 test_tools/serial_probe.py --wheel 1 --duty 30 --hold 1.0   # 单轮点检（会转）
    python3 test_tools/serial_probe.py --move 前进 --speed 30 --hold 1.5
    python3 test_tools/serial_probe.py --raw 30,30,-30,-30 --hold 1.0
    python3 test_tools/serial_probe.py --stop
    python3 test_tools/serial_probe.py --rgb 0,40,0 --beep

安全提示：`--wheel/--move/--raw` 会让轮子真的转。**先把车架起来（轮子离地）**。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from wheelctl import kinematics, protocol  # noqa: E402
from wheelctl.config import load_config  # noqa: E402
from wheelctl.transport import (  # noqa: E402
    HAS_PYSERIAL,
    HAS_TERMIOS,
    SerialTransport,
    TransportError,
    candidate_ports,
    detect_port,
)

MOVES = {
    "前进": 0,
    "左前": 45,
    "左平移": 90,
    "左后": 135,
    "后退": 180,
    "右后": 225,
    "右平移": 270,
    "右前": 315,
}


def _board_cfg():
    try:
        return load_config().get("board", {})
    except Exception:
        return {}


def _order():
    return _board_cfg().get("motor_order") or [1, 2, 3, 4]


def _invert():
    return _board_cfg().get("motor_invert") or [False, False, False, False]


def open_port(port: str, baud: int) -> SerialTransport:
    transport = SerialTransport(port, baud)
    try:
        transport.open()
    except TransportError as exc:
        raise SystemExit(f"打不开 {port} @ {baud}: {exc}")
    return transport


def listen(transport: SerialTransport, seconds: float, echo: bool = False) -> int:
    """只读监听：打印电池上报与收到的包数。返回收到的字节数。"""

    parser = protocol.PacketParser()
    total = 0
    frames = 0
    deadline = time.monotonic() + seconds
    last_battery = None
    while time.monotonic() < deadline:
        chunk = transport.read_bytes()
        if not chunk:
            time.sleep(0.01)
            continue
        total += len(chunk)
        if echo:
            print(f"  <- {chunk.hex()}")
        for frame in parser.feed(chunk):
            frames += 1
            decoded = protocol.decode_report(frame)
            if decoded and decoded["kind"] == "battery":
                if decoded["voltage_v"] != last_battery:
                    last_battery = decoded["voltage_v"]
                    print(f"    电池 {decoded['voltage_v']:.2f} V（{decoded['millivolt']} mV）")
    print(f"  共收到 {total} 字节 / {frames} 帧 / 坏帧 {parser.bad_frames}")
    return total


def hold_move(transport: SerialTransport, frame: bytes, seconds: float, echo: bool = False) -> None:
    """下发动作帧并保持一段时间，到点自动停车。"""

    transport.write(frame)
    print(f"  下发: {frame.hex()}")
    listen(transport, seconds, echo)
    transport.write(protocol.cmd_stop())
    print(f"  停车: {protocol.cmd_stop().hex()}")
    time.sleep(0.2)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="幻尔树莓派5扩展板串口诊断 / 试动作")
    parser.add_argument("-p", "--port", default="auto", help="串口设备，默认自动探测")
    parser.add_argument("-b", "--baud", type=int, default=1000000, help="波特率，默认 1000000")
    parser.add_argument("--scan", action="store_true", help="只列出候选串口")
    parser.add_argument("--seconds", type=float, default=2.0, help="只读探测时长，默认 2s")
    parser.add_argument("--echo", action="store_true", help="打印原始字节")
    parser.add_argument("--wheel", type=int, default=None, choices=[1, 2, 3, 4], help="单轮点检：电机编号")
    parser.add_argument("--duty", type=float, default=30.0, help="单轮点检占空 -100~100")
    parser.add_argument("--move", default=None, choices=sorted(MOVES.keys()), help="整机方向试动作")
    parser.add_argument("--speed", type=float, default=30.0, help="整机试动作速度 0~100")
    parser.add_argument("--rot", type=float, default=0.0, help="叠加自转 -100~100（可边走边转）")
    parser.add_argument("--raw", default=None, help='四轮占空，例如 "30,30,-30,-30"')
    parser.add_argument("--hold", type=float, default=1.0, help="动作保持秒数，默认 1.0s")
    parser.add_argument("--stop", action="store_true", help="只发停车帧")
    parser.add_argument("--rgb", default=None, help='RGB 例如 "0,40,0"（第 1 颗）')
    parser.add_argument("--beep", action="store_true", help="蜂鸣器响一声")
    args = parser.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    ports = candidate_ports()
    if args.scan:
        print("候选串口：")
        for port in ports:
            print("  -", port)
        if not HAS_PYSERIAL and not HAS_TERMIOS:
            print("警告：当前 Python 既没有 pyserial 也没有 termios，无法打开串口")
        return 0

    port_name = args.port
    if port_name in ("", "auto"):
        found = detect_port(ports, args.baud)
        if not found:
            print("未探测到可用串口。已尝试：" + ", ".join(ports), file=sys.stderr)
            print("提示：40pin 上有多路 UART，扩展板一般在 /dev/ttyS1（波特率 1 Mbps）", file=sys.stderr)
            return 2
        port_name, args.baud = found
        print(f"自动探测到：{port_name} @ {args.baud}")
    print(f"串口：{port_name} @ {args.baud} 8N1  ({'pyserial' if HAS_PYSERIAL else 'termios'})")

    transport = open_port(port_name, args.baud)
    try:
        if args.stop:
            transport.write(protocol.cmd_stop())
            print("已发送停车帧：", protocol.cmd_stop().hex())
            return 0

        if args.rgb is not None:
            rgba = [int(x) for x in args.rgb.replace("，", ",").split(",")]
            if len(rgba) >= 3:
                frame = protocol.cmd_rgb([(1, rgba[0], rgba[1], rgba[2])])
                transport.write(frame)
                print("已发送 RGB：", frame.hex())

        if args.beep:
            frame = protocol.cmd_buzzer(1200, 0.08, 0.05, 2)
            transport.write(frame)
            print("已发送蜂鸣器：", frame.hex())

        if args.wheel is not None:
            duty = float(max(-100.0, min(100.0, args.duty)))
            frame = protocol.cmd_motor_duty([(args.wheel, duty)])  # --wheel 是电机编号，不受映射影响
            print(f"单轮点检：M{args.wheel} 占空 {duty:.0f}%")
            hold_move(transport, frame, max(0.2, args.hold), args.echo)
            return 0

        if args.raw is not None:
            values = [float(x) for x in args.raw.replace("，", ",").split(",")]
            if len(values) != 4:
                raise SystemExit('--raw 需要 4 个值，例如 "30,30,-30,-30"')
            frame = protocol.cmd_motor_duty_all(
                protocol.map_car_to_motor(values, _order(), _invert()))
            print(f"四轮直控：{values}")
            hold_move(transport, frame, max(0.2, args.hold), args.echo)
            return 0

        if args.move is not None:
            angle = MOVES[args.move]
            speed = float(max(0.0, min(100.0, args.speed)))
            pct = kinematics.wheel_speeds(angle, speed, args.rot, False)
            duties = [protocol.apply_deadzone(v, 15.0) for v in pct]
            frame = protocol.cmd_motor_duty_all(
                protocol.map_car_to_motor(duties, _order(), _invert()))
            print(f"试动作：{args.move}（{angle}°）速度 {speed:.0f}% 自转 {args.rot:.0f}% → 占空 {duties}")
            hold_move(transport, frame, max(0.2, args.hold), args.echo)
            return 0

        # 默认：只读（车不会动）
        listen(transport, args.seconds, args.echo)
        print("只读探测完成。若上面没有『电池 x.xx V』：检查 40pin 是否插到底、电池是否打开、端口/波特率是否选对。")
        return 0
    finally:
        try:
            transport.write(protocol.cmd_stop())
        except Exception:
            pass
        transport.close()


if __name__ == "__main__":
    raise SystemExit(main())
