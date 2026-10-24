"""物理链路：串口（pyserial → 标准库 termios 回退）与仿真扩展板。

这一层只做**字节收发**；``0xAA 0x55`` 数据包的组帧/解帧在 :mod:`wheelctl.protocol`。

幻尔树莓派5扩展板（A 款）的实测参数：
* 串口 **1 Mbps** 8N1（官方 SDK 默认 ``/dev/ttyAMA0`` @ 1000000）；
* 板子**主动**每 ~200ms 推一帧 SYS 上报（电池电压），这条上报就是链路心跳；
* 指令帧不需要应答。

:class:`SimRasAdapter` 复刻这些行为，无硬件时也能演示与自检。
"""

from __future__ import annotations

import glob
import os
import struct
import time
from typing import Dict, List, Optional, Sequence, Tuple

from . import protocol

try:  # pragma: no cover - 由板端是否安装 pyserial 决定
    import serial  # type: ignore

    HAS_PYSERIAL = True
except Exception:  # pragma: no cover
    serial = None  # type: ignore
    HAS_PYSERIAL = False

try:  # pragma: no cover
    import termios

    HAS_TERMIOS = True
except Exception:  # pragma: no cover
    termios = None  # type: ignore
    HAS_TERMIOS = False


class TransportError(RuntimeError):
    """链路打开/写入失败。"""


class BaseTransport:
    mode = "base"

    def open(self) -> None:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError

    def write(self, frame) -> None:
        raise NotImplementedError

    def read_bytes(self) -> bytes:
        return b""

    @property
    def is_open(self) -> bool:
        return False

    @property
    def label(self) -> str:
        return self.mode


# ---------------------------------------------------------------- 串口
class SerialTransport(BaseTransport):
    """真实串口：幻尔树莓派5扩展板 1 Mbps 8N1。"""

    mode = "serial"

    _BAUD_CONST = {
        9600: "B9600",
        19200: "B19200",
        38400: "B38400",
        57600: "B57600",
        115200: "B115200",
        230400: "B230400",
        460800: "B460800",
        921600: "B921600",
        1000000: "B1000000",
        1500000: "B1500000",
        2000000: "B2000000",
    }

    def __init__(self, port: str, baud: int = 1000000) -> None:
        self.port = port
        self.baud = int(baud)
        self._impl = None
        self._fd: Optional[int] = None
        self.backend = ""

    def _open_pyserial(self) -> None:
        self._impl = serial.Serial(  # type: ignore[union-attr]
            port=self.port,
            baudrate=self.baud,
            bytesize=8,
            parity="N",
            stopbits=1,
            timeout=0.02,
            write_timeout=0.3,
        )
        # 与官方 SDK 一致：不要拉 RTS/DTR（有些串口芯片上电会因 DTR 变位复位对端）
        try:
            self._impl.rts = False
            self._impl.dtr = False
        except Exception:
            pass
        self._impl.reset_input_buffer()
        self.backend = "pyserial"

    def _open_termios(self) -> None:
        fd = os.open(self.port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        try:
            iflag, oflag, cflag, lflag, ispeed, ospeed, cc = termios.tcgetattr(fd)  # type: ignore[union-attr]
            cflag = (cflag & ~termios.CSIZE) | termios.CS8 | termios.CREAD | termios.CLOCAL  # type: ignore[union-attr]
            speed_name = self._BAUD_CONST.get(self.baud)
            if speed_name and hasattr(termios, speed_name):  # type: ignore[union-attr]
                speed = getattr(termios, speed_name)  # type: ignore[union-attr]
                ispeed, ospeed = speed, speed
            cc = list(cc)
            cc[termios.VMIN] = 0  # type: ignore[union-attr]
            cc[termios.VTIME] = 0  # type: ignore[union-attr]
            termios.tcsetattr(  # type: ignore[union-attr]
                fd, termios.TCSANOW, [0, 0, cflag, 0, ispeed, ospeed, cc]  # type: ignore[union-attr]
            )
        except Exception as exc:  # pragma: no cover
            os.close(fd)
            raise TransportError(f"serial config failed: {exc}") from exc
        self._fd = fd
        self.backend = "termios"

    def open(self) -> None:
        if HAS_PYSERIAL:
            try:
                self._open_pyserial()
                return
            except Exception as exc:
                raise TransportError(f"cannot open {self.port} @ {self.baud}: {exc}") from exc
        if HAS_TERMIOS:
            self._open_termios()
            return
        raise TransportError("neither pyserial nor termios available")

    def close(self) -> None:
        try:
            if self._impl is not None:
                self._impl.close()
        except Exception:
            pass
        finally:
            self._impl = None
        if self._fd is not None:
            try:
                os.close(self._fd)
            except Exception:
                pass
            self._fd = None

    def write(self, frame) -> None:
        payload = frame if isinstance(frame, (bytes, bytearray)) else str(frame).encode("ascii", "ignore")
        if self._impl is not None:
            self._impl.write(bytes(payload))
            self._impl.flush()
            return
        if self._fd is not None:
            os.write(self._fd, bytes(payload))
            return
        raise TransportError("serial not open")

    def read_bytes(self) -> bytes:
        if self._impl is not None:
            waiting = int(getattr(self._impl, "in_waiting", 0) or 0)
            if waiting > 0:
                return self._impl.read(waiting)
            return self._impl.read(1) or b""
        if self._fd is not None:
            try:
                return os.read(self._fd, 256)
            except (BlockingIOError, OSError):
                return b""
        return b""

    @property
    def is_open(self) -> bool:
        return self._impl is not None or self._fd is not None

    @property
    def label(self) -> str:
        backend = f" / {self.backend}" if self.backend else ""
        return f"{self.port} @ {self.baud}{backend}"


# ---------------------------------------------------------------- 仿真扩展板
class SimRasAdapter(BaseTransport):
    """树莓派5扩展板（RasAdapter5A）的软件仿真：解析占空帧 + 5Hz 电池上报。"""

    mode = "sim"
    FIRMWARE = "RasAdapter5A firmware (simulated)"

    def __init__(
        self,
        battery_v: float = 7.9,
        battery_decay_v_per_min: float = 0.02,
        report_interval_s: float = 0.2,
    ) -> None:
        self.battery_v = float(battery_v)
        self.battery_decay = float(battery_decay_v_per_min)
        self.report_interval = max(0.05, float(report_interval_s))

        self._open = False
        self._duties: Dict[int, float] = {1: 0.0, 2: 0.0, 3: 0.0, 4: 0.0}
        self._parser = protocol.PacketParser()
        self._boot = time.monotonic()
        self._last_report = 0.0
        self._out = bytearray()

        #: 主机下发的原始字节（自检用）
        self.history: List[Tuple[float, bytes]] = []
        #: 仿真板上报的帧（自检用）
        self.emitted: List[bytes] = []

    # ---- 生命周期 ----
    def open(self) -> None:
        self._open = True
        self._boot = time.monotonic()
        self._last_report = self._boot

    def close(self) -> None:
        self._open = False
        self._duties = {1: 0.0, 2: 0.0, 3: 0.0, 4: 0.0}

    @property
    def is_open(self) -> bool:
        return self._open

    @property
    def label(self) -> str:
        return "Simulation (RasAdapter5A emulated, no hardware)"

    # ---- 主机 → 扩展板 ----
    def write(self, frame) -> None:
        if not self._open:
            raise TransportError("simulation link not open")
        payload = frame if isinstance(frame, (bytes, bytearray)) else str(frame).encode("latin-1", "ignore")
        payload = bytes(payload)
        self.history.append((time.monotonic(), payload))
        if len(self.history) > 400:
            del self.history[:200]
        for parsed in self._parser.feed(payload):
            self._handle(parsed)

    def _handle(self, frame: protocol.Frame) -> None:
        if frame.func != protocol.PacketFunc.MOTOR or len(frame.data) < 2:
            return
        sub = frame.data[0]
        count = frame.data[1]
        if sub not in (protocol.MOTOR_SUB_DUTY, protocol.MOTOR_SUB_SPEED):
            return
        offset = 2
        for _ in range(count):
            if offset + 5 > len(frame.data):
                break
            motor_index = frame.data[offset]
            (value,) = struct.unpack("<f", frame.data[offset + 1:offset + 5])
            motor_id = motor_index + 1
            if 1 <= motor_id <= 4:
                self._duties[motor_id] = float(max(-100.0, min(100.0, value)))
            offset += 5

    # ---- 扩展板 → 主机 ----
    def read_bytes(self) -> bytes:
        if not self._open:
            return b""
        now = time.monotonic()
        if now - self._last_report >= self.report_interval:
            self._last_report = now
            millivolt = int(round(self._voltage(now) * 1000.0))
            data = bytes([protocol.SYS_REPORT_BATTERY]) + int(millivolt).to_bytes(2, "little")
            packet = protocol.build_packet(protocol.PacketFunc.SYS, data)
            self._out.extend(packet)
        if not self._out:
            return b""
        chunk = bytes(self._out)
        self._out.clear()
        return chunk

    def _voltage(self, now: float) -> float:
        minutes = (now - self._boot) / 60.0
        return max(5.0, self.battery_v - minutes * self.battery_decay)

    # ---- 自检辅助 ----
    @property
    def duty(self) -> List[float]:
        """车体序四轮占空（仿真板当前值）。"""

        return [self._duties[i] for i in protocol.MOTOR_IDS]

    @property
    def duty_by_id(self) -> Dict[int, float]:
        return dict(self._duties)


# ---------------------------------------------------------------- 端口探测
def _windows_com_ports() -> List[str]:
    """枚举 Windows 的 COM 口。"""

    if HAS_PYSERIAL:
        try:
            from serial.tools import list_ports  # type: ignore

            return [info.device for info in list_ports.comports()]
        except Exception:
            pass
    if os.name == "nt":
        try:
            import winreg

            ports: List[str] = []
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DEVICEMAP\SERIALCOMM") as key:
                index = 0
                while True:
                    try:
                        _, value, _ = winreg.EnumValue(key, index)
                    except OSError:
                        break
                    if value:
                        ports.append(str(value))
                    index += 1
            return sorted(ports)
        except Exception:
            pass
    return sorted(glob.glob("COM[0-9]*"))


def candidate_ports(extra: Optional[List[str]] = None) -> List[str]:
    ports: List[str] = []
    for pattern in ("/dev/ttyAMA*", "/dev/ttyUSB*", "/dev/ttyACM*"):
        ports.extend(sorted(glob.glob(pattern)))
    ports.extend(["/dev/ttyS1", "/dev/ttyS4", "/dev/ttyS0", "/dev/ttyS2", "/dev/serial0"])
    ports.extend(_windows_com_ports())
    if extra:
        ports.extend(extra)
    seen = set()
    unique = []
    for port in ports:
        if port not in seen:
            seen.add(port)
            unique.append(port)
    return unique


def _no_port_reason(candidates: Optional[Sequence[str]] = None) -> str:
    if not HAS_PYSERIAL and not HAS_TERMIOS:
        return (
            "No serial port found and pyserial is missing (Windows: pip install pyserial; "
            "Linux: install it or use the built-in termios fallback)"
        )
    tried = ", ".join(candidates or candidate_ports()) or "(no candidate ports)"
    return f"No usable serial port found, tried: {tried}"


def _can_open(port: str, baud: int) -> bool:
    try:
        if HAS_PYSERIAL:
            with serial.Serial(port=port, baudrate=baud, timeout=0.05):  # type: ignore[union-attr]
                pass
            return True
        fd = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        os.close(fd)
        return True
    except Exception:
        return False


def probe_port(port: str, baud: int = 1000000, timeout: float = 0.8) -> bool:
    """判定这条串口是不是接着扩展板：**收到任一合法 0xAA55 数据包**就算通。

    扩展板每 ~200ms 主动推 SYS（电池）帧，所以不需要先发指令；收到一帧即证明
    "波特率对、CRC 对、对面是这块板子"。
    """

    transport = SerialTransport(port, baud)
    try:
        transport.open()
    except Exception:
        return False
    parser = protocol.PacketParser()
    try:
        deadline = time.monotonic() + max(0.1, float(timeout))
        while time.monotonic() < deadline:
            chunk = transport.read_bytes()
            if chunk:
                if parser.feed(chunk):
                    return True
            else:
                time.sleep(0.01)
        return False
    except Exception:
        return False
    finally:
        try:
            transport.close()
        except Exception:
            pass


def detect_port(
    candidates: Optional[Sequence[str]] = None,
    baud: int = 1000000,
    probe: bool = True,
    alt_bauds: Sequence[int] = (115200,),
) -> Optional[Tuple[str, int]]:
    """按顺序找一个"真的在发 0xAA55 帧"的串口；返回 ``(设备名, 波特率)``。"""

    ports = [p for p in (candidates or candidate_ports())
             if not (p.startswith("/dev/") and not os.path.exists(p))]
    bauds = [int(baud)] + [int(b) for b in alt_bauds if int(b) != int(baud)]

    if probe:
        for rate in bauds:
            for port in ports:
                if probe_port(port, rate):
                    return port, rate

    for port in ports:
        if _can_open(port, int(baud)):
            return port, int(baud)
    return None


def build_transport(
    port: str,
    baud: int = 1000000,
    force_sim: bool = False,
    sim_kwargs: Optional[Dict] = None,
    candidates: Optional[Sequence[str]] = None,
) -> BaseTransport:
    """按配置构造传输层；``port=auto`` 找不到设备时自动降级到仿真。"""

    if force_sim or port in ("", "sim", "none"):
        return SimRasAdapter(**(sim_kwargs or {}))
    if port == "auto":
        found = detect_port(candidates, baud)
        if found is not None:
            found_port, found_baud = found
            return SerialTransport(found_port, found_baud)
        sim = SimRasAdapter(**(sim_kwargs or {}))
        sim.fallback_reason = _no_port_reason(candidates)  # type: ignore[attr-defined]
        return sim
    return SerialTransport(port, baud)
