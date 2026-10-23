"""幻尔树莓派5扩展板（RasAdapter5A）通信协议：``0xAA 0x55`` 数据包 + CRC8。

为什么是这个协议
----------------
扩展板（A 款 / ``SCH_RasAdapter5A V1.0``）板载 STM32 固件，**已经烧好、不要重新烧录**。
官方上位机接口是 ``ros_robot_controller_sdk.py`` 里的 ``Board`` 类（``/dev/ttyAMA0``、**1 Mbps**）：

    board.set_motor_duty([[1, 35]])          # 1 号电机占空 35%（负值反转）
    board.get_battery()                       # 板子自己周期性上报的电池电压（mV）

本模块把同一套协议**按字节实现**（不依赖 SDK 的线程/队列），
帧格式与 CRC 与会话使用的官方 SDK 完全一致（`test_tools/test_protocol.py` 会直接拿官方
``ros_robot_controller_sdk.py`` 做逐字节对照）。

帧格式
------
::

    0xAA 0x55 | func(1B) | len(1B) | data(len B) | crc8(1B)

* ``crc8`` 覆盖 ``func + len + data``（CRC-8/ATM，多项式 0x07，初值 0，查表实现与官方一致）；
* ``func`` 见 :class:`PacketFunc`；
* 电机帧（``func=3``）的 ``data[0]`` 是子命令：``0x05`` = 占空（开环），``0x01`` = 速度（闭环）；
  每个电机 5 字节：``<Bf``（索引 = 编号 - 1，float 数值）。

量纲
----
* 占空 ``-100 ~ 100``（官方文档明确："数值为正时前进，数值为负则后退"），
  超出范围可能损伤电机，所以本工程在主机侧硬钳位；
* 电池由板子**主动周期性上报**（SYS 帧、``data[0]=0x04``，uint16 小端，单位 mV），
  这条上报同时充当链路心跳；
* 本工程默认走**开环占空**（``$set_motor_duty`` / 子命令 0x05）：
  该底盘的 TT 类电机即使带编码器，页面也不假装有速度回读
  （官方 SDK 没有"读轮速"的接口，只有 ``set_motor_speed`` 闭环下发）。
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

FRAME_HEAD1 = 0xAA
FRAME_HEAD2 = 0x55

#: 板载 4 路电机接口，本工程按车体序 [左前, 右前, 右后, 左后] 对应编号 1..4
WHEEL_IDS: Tuple[str, ...] = ("FL", "FR", "RR", "RL")
WHEEL_LABELS: Tuple[str, ...] = ("FL", "FR", "RR", "RL")
MOTOR_IDS: Tuple[int, ...] = (1, 2, 3, 4)
#: 轮位下标 → 扩展板丝印编号（M1..M4）
CHANNELS: Tuple[str, ...] = ("M1", "M2", "M3", "M4")

DUTY_MIN, DUTY_MAX = -100.0, 100.0


class PacketFunc:
    """与官方 SDK 的 ``PacketFunction`` 一致。"""

    SYS = 0
    LED = 1
    BUZZER = 2
    MOTOR = 3
    PWM_SERVO = 4
    BUS_SERVO = 5
    KEY = 6
    IMU = 7
    GAMEPAD = 8
    SBUS = 9
    OLED = 10
    RGB = 11


#: 电机子命令
MOTOR_SUB_SPEED = 0x01   #: 闭环速度（需要编码器电机）
MOTOR_SUB_DUTY = 0x05    #: 开环占空（本工程默认）

#: SYS 上报里的子命令：电池电压（mV）
SYS_REPORT_BATTERY = 0x04


def clamp(value: float, low: float, high: float) -> float:
    return low if value < low else high if value > high else value


# ---------------------------------------------------------------- CRC8
#: CRC8 查找表：**原样抄自官方 ros_robot_controller_sdk.py 的 crc8_table**。
#: 这颗 STM32 用的校验算法与常见的 CRC-8/ATM(0x07) 不同，所以不要去倒推多项式，
#: 直接用官方表才能和板子对上（真机抓包回归见 test_tools/test_protocol.py）。
CRC8_TABLE: List[int] = [
    0, 94, 188, 226, 97, 63, 221, 131, 194, 156, 126, 32, 163, 253, 31, 65,
    157, 195, 33, 127, 252, 162, 64, 30, 95, 1, 227, 189, 62, 96, 130, 220,
    35, 125, 159, 193, 66, 28, 254, 160, 225, 191, 93, 3, 128, 222, 60, 98,
    190, 224, 2, 92, 223, 129, 99, 61, 124, 34, 192, 158, 29, 67, 161, 255,
    70, 24, 250, 164, 39, 121, 155, 197, 132, 218, 56, 102, 229, 187, 89, 7,
    219, 133, 103, 57, 186, 228, 6, 88, 25, 71, 165, 251, 120, 38, 196, 154,
    101, 59, 217, 135, 4, 90, 184, 230, 167, 249, 27, 69, 198, 152, 122, 36,
    248, 166, 68, 26, 153, 199, 37, 123, 58, 100, 134, 216, 91, 5, 231, 185,
    140, 210, 48, 110, 237, 179, 81, 15, 78, 16, 242, 172, 47, 113, 147, 205,
    17, 79, 173, 243, 112, 46, 204, 146, 211, 141, 111, 49, 178, 236, 14, 80,
    175, 241, 19, 77, 206, 144, 114, 44, 109, 51, 209, 143, 12, 82, 176, 238,
    50, 108, 142, 208, 83, 13, 239, 177, 240, 174, 76, 18, 145, 207, 45, 115,
    202, 148, 118, 40, 171, 245, 23, 73, 8, 86, 180, 234, 105, 55, 213, 139,
    87, 9, 235, 181, 54, 104, 138, 212, 149, 203, 41, 119, 244, 170, 72, 22,
    233, 183, 85, 11, 136, 214, 52, 106, 43, 117, 151, 201, 74, 20, 246, 168,
    116, 42, 200, 150, 21, 75, 169, 247, 182, 232, 10, 84, 215, 137, 107, 53,
]


def checksum_crc8(data: Sequence[int]) -> int:
    """CRC8：与官方 SDK 的 ``checksum_crc8`` 完全一致（查表法，表见 :data:`CRC8_TABLE`）。"""

    check = 0
    for byte in data:
        check = CRC8_TABLE[check ^ (byte & 0xFF)]
    return check & 0xFF


# ---------------------------------------------------------------- 组帧
def build_packet(func: int, data: Sequence[int]) -> bytes:
    """``build_packet(3, [0x05, 1, 0, 0, 0, 0, 0])`` → 完整一帧。"""

    payload = bytes(byte & 0xFF for byte in data)
    body = bytes([func & 0xFF, len(payload)]) + payload
    return bytes([FRAME_HEAD1, FRAME_HEAD2]) + body + bytes([checksum_crc8(body)])


def cmd_motor_duty(dutys: Sequence[Tuple[int, float]]) -> bytes:
    """``[(1, 35.0), (2, -35.0)]`` → 占空帧（只更新列出的电机）。"""

    data = [MOTOR_SUB_DUTY, len(dutys)]
    for motor_id, duty in dutys:
        data.extend(struct.pack("<Bf", int(motor_id) - 1, float(duty)))
    return build_packet(PacketFunc.MOTOR, data)


def map_car_to_motor(
    duties_car: Sequence[float],
    motor_order: Sequence[int] = (1, 2, 3, 4),
    motor_invert: Sequence[bool] = (False, False, False, False),
) -> List[float]:
    """车体序占空 [FL, FR, RR, RL] → 按电机编号 M1..M4 排好的占空。

    * ``motor_order[i]`` = 车体序第 i 个轮子接在哪个电机编号上（实车标定）；
    * ``motor_invert[m-1]`` = 该电机是否需要取反（右侧电机机械镜像安装时会反）。
    """

    order = list(motor_order or [1, 2, 3, 4])[:4]
    while len(order) < 4:
        order.append(len(order) + 1)
    invert = list(motor_invert or []) + [False] * 4
    values = [float(v) for v in list(duties_car)[:4]]
    values += [0.0] * (4 - len(values))

    by_motor = [0.0, 0.0, 0.0, 0.0]
    for car_idx, motor_id in enumerate(order):
        motor_id = int(motor_id)
        if 1 <= motor_id <= 4:
            sign = -1.0 if invert[motor_id - 1] else 1.0
            by_motor[motor_id - 1] = values[car_idx] * sign
    return by_motor


def cmd_motor_duty_all(duties: Sequence[float]) -> bytes:
    """车体序 4 个占空（-100~100）→ 一次更新 4 个电机（编号 1..4）。"""

    values = [float(clamp(float(v), DUTY_MIN, DUTY_MAX)) for v in list(duties)[:4]]
    values += [0.0] * (4 - len(values))
    return cmd_motor_duty(list(zip(MOTOR_IDS, values)))


def cmd_motor_speed(speeds: Sequence[Tuple[int, float]]) -> bytes:
    """闭环速度帧（需要编码器电机，本工程默认不用）。"""

    data = [MOTOR_SUB_SPEED, len(speeds)]
    for motor_id, speed in speeds:
        data.extend(struct.pack("<Bf", int(motor_id) - 1, float(speed)))
    return build_packet(PacketFunc.MOTOR, data)


def cmd_stop() -> bytes:
    """四轮占空归零。"""

    return cmd_motor_duty_all([0.0, 0.0, 0.0, 0.0])


def cmd_rgb(pixels: Sequence[Tuple[int, int, int, int]]) -> bytes:
    """``[(1, r, g, b), ...]``，编号从 1 开始。"""

    data = [0x01, len(pixels)]
    for index, r, g, b in pixels:
        data.extend(struct.pack("<BBBB", int(index) - 1, int(r) & 0xFF, int(g) & 0xFF, int(b) & 0xFF))
    return build_packet(PacketFunc.RGB, data)


def cmd_buzzer(freq: int, on_s: float, off_s: float = 0.0, repeat: int = 1) -> bytes:
    data = struct.pack("<HHHH", int(freq), int(on_s * 1000), int(off_s * 1000), int(repeat))
    return build_packet(PacketFunc.BUZZER, list(data))


# ---------------------------------------------------------------- 死区补偿
def apply_deadzone(pct: float, deadzone_pct: float = 15.0) -> float:
    """把"期望强度（%）"变成"能转起来的占空（%）"，逐轮独立。

    0 保持 0；非 0 从死区起步线性映射到 100。官方固件不做死区补偿，
    TT 类电机开环低速起不来，所以这一步放在主机侧（``board.deadzone_pct`` 可标定）。
    """

    pct = float(clamp(pct, DUTY_MIN, DUTY_MAX))
    if pct == 0:
        return 0.0
    dead = float(clamp(deadzone_pct, 0.0, 95.0))
    sign = 1.0 if pct > 0 else -1.0
    mag = abs(pct)
    return float(clamp(dead + mag * (100.0 - dead) / 100.0, 0.0, 100.0)) * sign


def estimate_mm_s(duty_pct: float, max_speed_mm_s: float = 200.0) -> int:
    """占空 → 估算轮速（mm/s），仅用于页面展示"估算"。"""

    return int(round(float(duty_pct) * float(max_speed_mm_s) / 100.0))


def mm_s_to_pct(mm_s: float, max_speed_mm_s: float = 200.0) -> float:
    if not max_speed_mm_s:
        return 0.0
    return round(float(mm_s) * 100.0 / float(max_speed_mm_s), 1)


# ---------------------------------------------------------------- 解帧
@dataclass
class Frame:
    """一条完整数据包。"""

    func: int
    data: bytes
    raw: bytes

    @property
    def name(self) -> str:
        return {0: "SYS", 1: "LED", 2: "BUZZER", 3: "MOTOR", 4: "PWM_SERVO",
                5: "BUS_SERVO", 6: "KEY", 7: "IMU", 8: "GAMEPAD", 9: "SBUS",
                10: "OLED", 11: "RGB"}.get(self.func, f"FUNC{self.func}")


def decode_report(frame: Frame) -> Optional[Dict]:
    """把扩展板上报解析成结构化数据；不认识返回 ``None``。"""

    if frame.func == PacketFunc.SYS and len(frame.data) >= 3 and frame.data[0] == SYS_REPORT_BATTERY:
        millivolt = struct.unpack("<H", frame.data[1:3])[0]
        return {"kind": "battery", "millivolt": millivolt, "voltage_v": round(millivolt / 1000.0, 2)}

    if frame.func == PacketFunc.KEY and len(frame.data) >= 2:
        return {"kind": "key", "key_id": frame.data[0], "event": frame.data[1]}

    if frame.func == PacketFunc.IMU and len(frame.data) >= 24:
        ax, ay, az, gx, gy, gz = struct.unpack("<6f", frame.data[:24])
        return {"kind": "imu", "ax": ax, "ay": ay, "az": az, "gx": gx, "gy": gy, "gz": gz}

    return None


class PacketParser:
    """``0xAA 0x55`` 流式解析：自动找帧头、校验 CRC8、丢弃坏帧。"""

    def __init__(self, max_buffer: int = 4096) -> None:
        self._buf = bytearray()
        self._max = int(max_buffer)
        self.bad_frames = 0

    def feed(self, chunk: bytes | bytearray | str) -> List[Frame]:
        if not chunk:
            return []
        if isinstance(chunk, str):
            chunk = chunk.encode("latin-1", "ignore")
        self._buf.extend(chunk)
        if len(self._buf) > self._max:
            del self._buf[:-self._max // 2]
            self.bad_frames += 1

        frames: List[Frame] = []
        while True:
            start = self._find_head()
            if start < 0:
                # 没有帧头：只留最后一个字节，避免缓冲无限增长
                del self._buf[:max(0, len(self._buf) - 1)]
                break
            if start > 0:
                del self._buf[:start]
            if len(self._buf) < 4:
                break
            func = self._buf[2]
            length = self._buf[3]
            total = 5 + length
            if len(self._buf) < total:
                break
            body = bytes(self._buf[2:4 + length])
            crc = self._buf[4 + length]
            if checksum_crc8(body) != crc:
                self.bad_frames += 1
                del self._buf[:1]
                continue
            payload = bytes(self._buf[4:4 + length])
            frames.append(Frame(func=func, data=payload, raw=bytes(self._buf[:total])))
            del self._buf[:total]
        return frames

    def _find_head(self) -> int:
        buf = self._buf
        for i in range(len(buf) - 1):
            if buf[i] == FRAME_HEAD1 and buf[i + 1] == FRAME_HEAD2:
                return i
        return -1
