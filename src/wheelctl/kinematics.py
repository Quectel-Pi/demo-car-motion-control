"""麦克纳姆轮运动学（只负责几何，不含任何驱动板细节）。

坐标约定（沿用原厂例程）：

* 车轮编号 ``[0]=左前 FL  [1]=右前 FR  [2]=右后 RR  [3]=左后 RL``；
* ``angle`` 以车头为 0°、逆时针为正，``90`` 为左平移、``180`` 为后退、``270`` 为右平移；
* ``velocity`` 平移强度 0~100，``rot`` 自转强度 -100~100（正=左转/逆时针）；
* ``drift`` 漂移模式（后两轮反向加力）。

输出为 4 个轮子的**相对强度**（-100~100，截断取整）。要变成驱动板指令，需用
:mod:`wheelctl.protocol` 的 ``pct_to_spd`` / ``pct_to_pwm`` 并按板序映射
（官方板 M1=左前, M2=左后, M3=右前, M4=右后）。
"""

from __future__ import annotations

import math
from typing import Dict, List, Tuple

WHEEL_COUNT = 4
WHEEL_IDS: Tuple[str, ...] = ("FL", "FR", "RR", "RL")
WHEEL_LABELS: Tuple[str, ...] = ("左前", "右前", "右后", "左后")


def clamp(value: float, low: float, high: float) -> float:
    return low if value < low else high if value > high else value


def sanitize(
    angle: float = 0,
    velocity: float = 0,
    rot: float = 0,
    drift: bool = False,
) -> Tuple[int, int, int, bool]:
    """把外部输入收敛到安全范围。"""

    try:
        angle_i = int(round(float(angle))) % 360
    except (TypeError, ValueError):
        angle_i = 0
    try:
        velocity_i = int(clamp(int(round(float(velocity))), 0, 100))
    except (TypeError, ValueError):
        velocity_i = 0
    try:
        rot_i = int(clamp(int(round(float(rot))), -100, 100))
    except (TypeError, ValueError):
        rot_i = 0
    return angle_i, velocity_i, rot_i, bool(drift)


def wheel_speeds(angle: float, velocity: float, rot: float = 0, drift: bool = False) -> List[int]:
    """返回 4 个轮子的相对强度（-100~100），顺序 FL/FR/RR/RL。"""

    angle_i, velocity_i, rot_i, drift_b = sanitize(angle, velocity, rot, drift)

    speed = 1.0 if rot_i == 0 else 0.5
    rad = math.radians((angle_i + 90) % 360)
    v = velocity_i / math.sqrt(2)
    s = v * math.sin(rad)
    c = v * math.cos(rad)

    if drift_b:
        raw = [
            (s - c) * speed,
            (s + c) * speed,
            (s - c) * speed - rot_i * speed * 2,
            (s + c) * speed + rot_i * speed * 2,
        ]
    else:
        raw = [
            (s - c) * speed + rot_i * speed,
            (s + c) * speed - rot_i * speed,
            (s - c) * speed - rot_i * speed,
            (s + c) * speed + rot_i * speed,
        ]

    return [int(clamp(int(x), -100, 100)) for x in raw]


def motion_vector(angle: float, velocity: float, rot: float = 0) -> Dict[str, float]:
    """车体坐标系下的运动矢量（用于前端画箭头）。

    ``vx`` 正=车体左侧（``angle=90`` 时为正），``vy`` 正=车头方向，
    ``omega`` 为近似角速度(°/s)，正值表示逆时针（左转）。
    """

    angle_i, velocity_i, rot_i, _ = sanitize(angle, velocity, rot, False)
    rad = math.radians(angle_i)
    return {
        "vx": round(velocity_i * math.sin(rad), 3),
        "vy": round(velocity_i * math.cos(rad), 3),
        "omega": round(rot_i * 3.0, 3),
    }


def describe(angle: float, velocity: float, rot: float = 0, drift: bool = False) -> str:
    """把一组控制量翻译成英文动作描述。"""

    angle_i, velocity_i, rot_i, drift_b = sanitize(angle, velocity, rot, drift)
    if velocity_i == 0 and rot_i == 0:
        return "Stopped"
    names = {
        0: "Forward",
        45: "Forward-left",
        90: "Strafe left",
        135: "Back-left",
        180: "Reverse",
        225: "Back-right",
        270: "Strafe right",
        315: "Forward-right",
    }
    tag = " (drift)" if drift_b else ""
    if velocity_i and rot_i:
        base = names.get(angle_i, f"{angle_i} deg")
        return f"{base} + {'turn left' if rot_i > 0 else 'turn right'} {abs(rot_i)}%{tag}"
    if rot_i and not velocity_i:
        return f"Spin {'left' if rot_i > 0 else 'right'} {abs(rot_i)}%{tag}"
    base = names.get(angle_i, f"{angle_i} deg")
    return f"{base} {velocity_i}%{tag}"


