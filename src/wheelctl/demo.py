"""演示（demo）序列：把车轮动作编排成带解说的时间脚本。

设计说明：
    序列在 **服务端** 执行（由驱动线程按状态变化下发 0xAA55 电机帧），因此演示期间浏览器卡顿、
    手机息屏或页面刷新都不会打断动作；页面只负责展示进度。

    每个 step 是「保持时长 + 控制量」的组合，控制量与
    :mod:`wheelctl.kinematics` 的入参保持一致。

    本工程走扩展板的 ``set_motor_duty``（**逐轮独立占空**），所以八方向、斜行、
    边走边转、漂移、单轮点检都能编排；``raw`` 步骤就是"四个轮子分别给多少"。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import ClassVar, Any, Dict, List, Optional, Sequence as TypingSequence, Tuple

from . import kinematics, protocol


@dataclass(frozen=True)
class Step:
    caption: str
    duration: float
    angle: int = 0
    velocity: int = 0
    rot: int = 0
    drift: bool = False
    raw: Optional[Tuple[int, int, int, int]] = None
    # 闭环转向：给出目标偏航角（度，相对演示开始时的朝向），由 IMU 反馈转到位
    heading: Optional[float] = None
    rot_limit: int = 40

    @property
    def mode(self) -> str:
        if self.heading is not None:
            return "turn"
        return "raw" if self.raw is not None else "vector"

    def target(self) -> Dict:
        if self.raw is not None:
            return {"mode": "raw", "wheels": list(self.raw)}
        return {
            "mode": "vector",
            "angle": int(self.angle),
            "velocity": int(self.velocity),
            "rot": int(self.rot),
            "drift": bool(self.drift),
        }

    def to_dict(self) -> Dict:
        return {
            "caption": self.caption,
            "duration": round(self.duration, 2),
            "angle": self.angle,
            "velocity": self.velocity,
            "rot": self.rot,
            "drift": self.drift,
            "raw": list(self.raw) if self.raw else None,
            "heading": self.heading,
            "rot_limit": self.rot_limit,
            "mode": self.mode,
        }


@dataclass(frozen=True)
class Sequence:
    name: str
    title: str
    description: str
    steps: Tuple[Step, ...]

    # 点检/标定类演示（页面里排在最后，并带 check 标记）
    CHECK_DEMOS: ClassVar[Tuple[str, ...]] = ("dir_scan", "wheel_scan", "dist_check")

    @property
    def duration(self) -> float:
        return round(sum(s.duration for s in self.steps), 2)

    @property
    def group(self) -> str:
        """showcase（展示）/ check（点检标定）。"""
        return "check" if self.name in self.CHECK_DEMOS else "showcase"

    def to_dict(self, include_steps: bool = False) -> Dict:
        data = {
            "name": self.name,
            "group": self.group,
            "title": self.title,
            "description": self.description,
            "duration": self.duration,
            "step_count": len(self.steps),
        }
        if include_steps:
            data["steps"] = [s.to_dict() for s in self.steps]
        return data


def _stop(seconds: float = 0.4, caption: str = "Stop (settle)") -> Step:
    return Step(caption, seconds)


def _straight_mm_s(velocity: int, deadzone_pct: float, apply_deadzone: bool, max_speed_mm_s: float) -> float:
    """某个"平移速度(%)"对应的估算车速（mm/s）——直行时四轮同强度，取其一即可。"""

    pct = max(abs(float(v)) for v in kinematics.wheel_speeds(angle=0, velocity=velocity))
    duty = protocol.apply_deadzone(pct, deadzone_pct) if apply_deadzone else pct
    return float(protocol.estimate_mm_s(duty, max_speed_mm_s)) or 1.0


def _build_sequences(velocity: int = 55, rot: int = 40, home: Optional[Dict[str, Any]] = None) -> Dict[str, Sequence]:
    v = max(10, min(100, int(velocity)))
    r = max(10, min(60, int(rot)))

    tour = Sequence(
        name="tour",
        title="Full motion tour",
        description="Forward / reverse / strafe both ways / diagonals / spin both ways — the whole mecanum repertoire in one run.",
        steps=(
            Step("Forward (000 deg)", 1.4, angle=0, velocity=v),
            _stop(0.35),
            Step("Reverse (180 deg)", 1.4, angle=180, velocity=v),
            _stop(0.35),
            Step("Strafe left (090 deg)", 1.2, angle=90, velocity=v),
            Step("Strafe right (270 deg)", 1.2, angle=270, velocity=v),
            Step("Diagonal FL (045 deg)", 1.2, angle=45, velocity=v),
            Step("Diagonal BR (225 deg)", 1.2, angle=225, velocity=v),
            _stop(0.35),
            Step("Spin left", 1.3, rot=r),
            Step("Spin right", 1.3, rot=-r),
            _stop(0.8, "Demo finished"),
        ),
    )

    # 米字往返：每个方向「走出去 → 原路返回中心」，所以每次动作结束都在出发点附近。
    # 时长按估算车速换算（无编码器，开环），去/回用同样时长，滑动误差会累积。
    star_leg = 0.90   # 单程时长（秒）；去/回相同，保证原路返回
    star_steps: List[Step] = []
    for angle in range(0, 360, 45):
        back = (angle + 180) % 360
        star_steps.append(Step(f"Out {angle:03d} deg", star_leg, angle=angle, velocity=v))
        star_steps.append(Step(f"Back to centre {back:03d} deg", star_leg, angle=back, velocity=v))
        star_steps.append(_stop(0.30))
    star = Sequence(
        name="star",
        title="Star: out and back",
        description=(
            "For each of the eight headings: move out, then come straight back along the same path to the "
            "centre before the next one (open loop - no encoders, so slip accumulates)."
        ),
        steps=tuple(star_steps) + (_stop(0.6, "Demo finished"),),
    )

    spin = Sequence(
        name="spin",
        title="Spin ramp",
        description="Spin speed ramps up, left then right, to show the four-wheel differential.",
        steps=(
            Step("Spin left 20%", 1.0, rot=20),
            Step("Spin left 40%", 1.0, rot=40),
            Step("Spin left max", 1.2, rot=min(60, r + 20)),
            _stop(0.4),
            Step("Spin right 20%", 1.0, rot=-20),
            Step("Spin right 40%", 1.0, rot=-40),
            Step("Spin right max", 1.2, rot=-min(60, r + 20)),
            _stop(0.8, "Demo finished"),
        ),
    )

    diag = Sequence(
        name="diag",
        title="Four diagonals",
        description="45/135/225/315 deg diagonals to show mecanum diagonal motion.",
        steps=(
            Step("Diagonal FL 045 deg", 1.4, angle=45, velocity=v),
            _stop(0.35),
            Step("Diagonal BL 135 deg", 1.4, angle=135, velocity=v),
            _stop(0.35),
            Step("Diagonal BR 225 deg", 1.4, angle=225, velocity=v),
            _stop(0.35),
            Step("Diagonal FR 315 deg", 1.4, angle=315, velocity=v),
            _stop(0.8, "Demo finished"),
        ),
    )

    dir_scan = Sequence(
        name="dir_scan",
        title="8-direction check",
        description="Nudge each direction once: confirm forward = nose, strafe left, spin = counter-clockwise.",
        steps=tuple(
            step
            for angle, caption in (
                (0, "Forward: wheel tops toward nose"),
                (90, "Strafe left"),
                (180, "Reverse"),
                (270, "Strafe right"),
                (45, "Diagonal FL"),
                (135, "Diagonal BL"),
                (225, "Diagonal BR"),
                (315, "Diagonal FR"),
            )
            for step in (
                Step(f"{caption} ({angle:03d} deg)", 0.9, angle=angle, velocity=max(20, min(v, 45))),
                _stop(0.25),
            )
        )
        + (_stop(0.6, "Demo finished"),),
    )

    drift = Sequence(
        name="drift",
        title="Drift showcase",
        description="The spin component is pushed onto the rear wheels only, the front wheels just translate — four combos.",
        steps=(
            Step("Drift: forward + turn right", 1.6, angle=0, velocity=v, rot=-r, drift=True),
            _stop(0.4),
            Step("Drift: forward + turn left", 1.6, angle=0, velocity=v, rot=r, drift=True),
            _stop(0.4),
            Step("Drift: reverse + turn right", 1.5, angle=180, velocity=v, rot=-r, drift=True),
            _stop(0.4),
            Step("Drift: strafe right + turn left", 1.4, angle=270, velocity=v, rot=r, drift=True),
            _stop(0.8, "Demo finished"),
        ),
    )

    wheel_scan = Sequence(
        name="wheel_scan",
        title="Single-wheel check",
        description="Nudge each wheel then all four; used for wiring and direction calibration. "
                    "Car order FL/FR/RR/RL maps to M1/M2/M4/M3 on this chassis.",
        steps=(
            Step("FL (M1) forward", 1.0, raw=(40, 0, 0, 0)),
            _stop(0.3),
            Step("FR (M2) forward", 1.0, raw=(0, 40, 0, 0)),
            _stop(0.3),
            Step("RR (M4) forward", 1.0, raw=(0, 0, 40, 0)),
            _stop(0.3),
            Step("RL (M3) forward", 1.0, raw=(0, 0, 0, 40)),
            _stop(0.3),
            Step("All four forward 30%", 1.2, raw=(30, 30, 30, 30)),
            Step("Alternating +30 / -30%", 1.2, raw=(30, -30, 30, -30)),
            _stop(0.6, "Demo finished"),
        ),
    )

    # ---- 正方形归位（开环：按估算速度算时间，4 边 + 4 次 90° 回到原点）----
    hc = dict(home or {})
    side_mm = float(hc.get("side_mm", 400.0))
    turn_deg = float(hc.get("turn_deg", 90.0))
    hv = max(10, min(80, int(hc.get("velocity", v))))
    hr = max(10, min(60, int(hc.get("rot", 30))))
    scale = float(hc.get("scale", 1.0)) or 1.0
    max_speed = float(hc.get("max_speed_mm_s", 200.0)) or 200.0
    deadzone = float(hc.get("deadzone_pct", 15.0))
    use_dz = bool(hc.get("apply_deadzone", True))
    deg_per_s = float(hc.get("deg_per_s_per_rot", 3.0)) * float(hr)   # 与 3D 的 omega=rot*3 一致
    mm_s = _straight_mm_s(hv, deadzone, use_dz, max_speed)
    side_s = max(0.4, round(side_mm / max(1.0, mm_s) * scale, 2))
    turn_s = max(0.3, round(turn_deg / max(1.0, deg_per_s) * scale, 2))
    settle = max(0.05, float(hc.get("settle_s", 0.25)))          # 每步后停稳时间，减小惯性拖尾
    trim_deg = float(hc.get("turn_trim_deg", 0.0))               # 每次转角的过冲补偿（正值=左转过冲，回一点）
    trim_s = max(0.05, round(abs(trim_deg) / max(1.0, deg_per_s), 2)) if trim_deg else 0.0
    square_steps: List[Step] = []
    for k in range(4):
        square_steps.append(Step(f"Side {k + 1} ({side_mm:.0f}mm ~ {side_s:.2f}s)", side_s, angle=0, velocity=hv))
        square_steps.append(_stop(settle))
        square_steps.append(Step(f"Turn left {turn_deg:.0f} deg ({turn_s:.2f}s)", turn_s, rot=hr))
        if trim_deg:
            # 过冲补偿：左转多转了 trim_deg，就反向转回同样的角度
            square_steps.append(Step(f"Trim {trim_deg:+.1f} deg", trim_s, rot=-hr))
        square_steps.append(_stop(settle))
    # 正方形归位只提供 IMU 闭环版（home_imu）：转角由陀螺仪控制，开环版不再保留

    # ---- 距离标定：按标定车速直行指定距离（默认 300 mm），用户用尺量，反推 max_speed_mm_s ----
    _dz = float((home or {}).get("deadzone_pct", 15.0))
    _ms = float((home or {}).get("max_speed_mm_s", 632.0))
    calib_mm = float((home or {}).get("calib_mm", 300.0))
    mm_s = _straight_mm_s(v, _dz, True, _ms)
    dist_s = max(0.15, round(calib_mm / max(1.0, mm_s), 3))
    dist_check = Sequence(
        name="dist_check",
        title=f"Distance check ({calib_mm:.0f} mm)",
        description=(
            f"Drives exactly {calib_mm:.0f} mm forward ({dist_s:.2f} s @ {v}%) using the calibrated speed, "
            f"then stops. Measure the real distance; if it is off, scale board.max_speed_mm_s by (measured / "
            f"{calib_mm:.0f})."
        ),
        steps=(
            Step(f"Forward {calib_mm:.0f} mm ({dist_s:.2f} s @ {v}%)", dist_s, angle=0, velocity=v),
            _stop(0.8, "Stopped - measure now"),
        ),
    )

    # ---- 正方形归位（IMU 闭环转角）：4 条边仍按时间，4 次转弯由陀螺仪闭环 ----
    row_limit = max(15, hr)
    imu_steps: List[Step] = []
    for k in range(4):
        imu_steps.append(Step(f"Side {k + 1} ({side_mm:.0f}mm ~ {side_s:.2f}s)", side_s, angle=0, velocity=hv))
        imu_steps.append(_stop(settle))
        target_deg = turn_deg * (k + 1)
        imu_steps.append(
            Step(
                f"Turn left to {target_deg:.0f} deg (IMU closed loop)",
                max(1.0, round(turn_s * 2.0, 2)),      # 上限时长：转到位会提前结束
                rot=hr,                                 # 没有 IMU 时按这个速度、按时长兜底
                heading=target_deg,
                rot_limit=row_limit,
            )
        )
        imu_steps.append(_stop(settle))
    home_imu = Sequence(
        name="home_imu",
        title="Square return (IMU closed loop)",
        description=(
            "Same square as 'home', but every 90 deg turn is closed by the MPU6050 gyro: the car keeps "
            "rotating until the measured heading reaches the target, so turn errors no longer accumulate. "
            "Needs the IMU; without it the turns fall back to timed rotation."
        ),
        steps=tuple(imu_steps) + (_stop(0.6, "Back to start (demo finished)"),),
    )

    # 顺序即页面下拉列表的顺序：先展示类，再点检/标定类（check）
    order = ("tour", "star", "drift", "home_imu", "dir_scan", "wheel_scan", "dist_check")
    built = {seq.name: seq for seq in (tour, star, spin, diag, dir_scan, drift, wheel_scan, home_imu, dist_check)}
    return {name: built[name] for name in order if name in built}


class DemoRunner:
    """线程安全的演示执行器：``tick()`` 由驱动线程驱动，按运行时间推进步骤。"""

    def __init__(
        self,
        velocity: int = 55,
        rot: int = 40,
        home: Optional[Dict[str, Any]] = None,
        heading_fn: Optional[Callable[[], Optional[float]]] = None,
        rate_fn: Optional[Callable[[], Optional[float]]] = None,
        turn_tol: float = 2.5,
        turn_lead: float = 2.0,
        rate_tol: float = 10.0,
        turn_kp: float = 1.1,
        turn_kd: float = 0.30,
        turn_deadband: float = 3.0,
        turn_fine_deg: float = 6.0,
        turn_fine_rot: int = 5,
        turn_done_deg: float = 1.2,
        turn_min_pulse: int = 8,
        turn_kp_fine: float = 1.6,
        hidden: Optional[List[str]] = None,
        heading_hold: bool = True,
        hold_gain: float = 0.6,
        hold_max_rot: int = 3,
    ) -> None:
        self._heading_fn = heading_fn        # 返回当前偏航角（度）；None 表示没有 IMU
        self._rate_fn = rate_fn              # 返回当前角速度（°/s）；用来判断"转完是否停稳"
        self._turn_tol = float(turn_tol)
        self._turn_lead = float(turn_lead)   # 小提前量：让惯性滑到位
        self._rate_tol = float(rate_tol)     # 判定停稳的角速度阈值（°/s）
        self._turn_kp = float(turn_kp)       # 比例增益
        self._turn_kd = float(turn_kd)       # 微分增益（用角速度做阻尼，抑制来回摆）
        self._turn_deadband = float(turn_deadband)   # 死区：小于这个误差就不再动
        self._turn_fine_deg = float(turn_fine_deg)   # 精调窗口（°）
        self._turn_fine_rot = int(turn_fine_rot)     # 精调时的最大转速（%）
        self._turn_done_deg = float(turn_done_deg)   # 认定到位的误差（°）
        self._turn_min_pulse = int(turn_min_pulse)   # 精调最小脉冲（%，克服启动摩擦）
        self._turn_kp_fine = float(turn_kp_fine)     # 精调比例增益
        self._turn_flips = 0
        # 航向保持（直行段用 IMU 做微调；IMU 不可用时自动退化为开环，绝不中止）
        self._heading_hold = bool(heading_hold)
        self._hold_gain = float(hold_gain)
        self._hold_max_rot = int(hold_max_rot)
        self._hold_target = None      # 本段直行开始时的朝向（度）
        self._hold_rot = 0            # 当前叠加的自转修正（%）
        self._hold_state = "off"      # off / active / no-imu                 # 误差反号次数（来回摆的计数）
        self._last_err_sign = 0
        self._last_heading = None            # 上一拍读到的朝向（检测读数是否卡住）
        self._last_heading_at = 0.0
        self._imu_lost = False
        self._abort_reason = None
        self._turn_log: List[Dict[str, Any]] = []
        self._heading_error: Optional[float] = None
        self._lock = threading.RLock()
        self._home = dict(home or {})
        built = _build_sequences(velocity, rot, self._home)
        skip = {str(x) for x in (hidden or [])}
        self._sequences = {k: v for k, v in built.items() if k not in skip}
        self._seq: Optional[Sequence] = None
        self._index = 0
        self._elapsed = 0.0
        self._running = False
        self._paused = False
        self._finished = False
        self._last_tick = 0.0
        self._started_wall = 0.0
        self._last_run_s = None

    # ---- 元信息 ----
    def list_sequences(self) -> List[Dict]:
        return [seq.to_dict() for seq in self._sequences.values()]

    def get_sequence(self, name: str) -> Optional[Sequence]:
        return self._sequences.get(name)

    # ---- 控制 ----
    def start(self, name: str) -> Dict:
        seq = self._sequences.get(name)
        if seq is None:
            raise KeyError(f"unknown demo sequence: {name}")
        with self._lock:
            self._seq = seq
            self._index = 0
            self._turn_log = []
            self._abort_reason = None
            self._elapsed = 0.0
            self._running = True
            self._paused = False
            self._finished = False
            self._last_tick = time.monotonic()
            self._started_wall = time.monotonic()
            self._last_run_s = None
            return self._snapshot_locked()

    def pause(self) -> Dict:
        with self._lock:
            if self._running and not self._paused:
                self._paused = True
                self._last_tick = time.monotonic()
            return self._snapshot_locked()

    def resume(self) -> Dict:
        with self._lock:
            if self._running and self._paused:
                self._paused = False
                self._last_tick = time.monotonic()
            return self._snapshot_locked()

    def stop(self, finished: bool = False) -> None:
        with self._lock:
            self._running = False
            self._paused = False
            self._finished = finished
            if not finished:
                # 手动停止后进度归零，页面不会停留在上一次的进度条
                self._elapsed = 0.0
                self._index = 0
            self._last_tick = time.monotonic()

    # ---- 驱动线程调用 ----
    def _finalize_crossed_turn(self) -> None:
        """tick 跳步时补记转角结果。

        问题背景：步骤是按"累计时间片"推进的。如果某一拍的 _elapsed 直接越过了
        转角步的结束时间（例如 6.90 > 6.89），for 循环会跳过这一步，导致
        "没到位的转角"既不判定、也不记录，turn 日志就丢了。
        这里在越过末尾时补一条 timeout/give_up 记录。
        """

        seq = self._seq
        if seq is None or not (0 <= self._index < len(seq.steps)):
            return
        step = seq.steps[self._index]
        if step.heading is None or self._heading_fn is None:
            return
        end = sum(s.duration for s in seq.steps[: self._index + 1])
        if self._elapsed < end:
            return
        cur = self._heading_fn()
        err = None if cur is None else (step.heading - cur + 180.0) % 360.0 - 180.0
        reason = "timeout"
        if err is not None and abs(err) <= float(getattr(self, "_turn_fine_deg", 6.0)):
            reason = "give_up"
        self._turn_log.append({
            "target": round(float(step.heading), 1),
            "achieved": None if cur is None else round(float(cur), 1),
            "error": None if err is None else round(float(err), 1),
            "rate": None,
            "seconds": round(float(step.duration), 2),
            "reason": reason,
        })
        del self._turn_log[:-12]

    # ---- 驱动线程调用 ----
    def tick(self) -> None:
        with self._lock:
            if not self._running or self._paused or self._seq is None:
                self._last_tick = time.monotonic()
                return
            now = time.monotonic()
            step_dt = min(0.5, max(0.0, now - self._last_tick))
            self._last_tick = now
            self._elapsed += step_dt
            self._finalize_crossed_turn()

            # 逐个步骤判断是否该进入下一步；转角步可以"转到位就提前结束"
            while True:
                total = 0.0
                advanced = False
                for idx, step in enumerate(self._seq.steps):
                    start = total
                    total += step.duration
                    if self._elapsed < total:
                        if idx != self._index:
                            self._index = idx
                            self._heading_error = None
                            self._turn_flips = 0
                            self._last_err_sign = 0
                            self._last_heading = None
                            self._imu_lost = False
                            self._last_heading_at = time.monotonic()
                            self._hold_target = None
                            self._hold_rot = 0
                            self._hold_state = "off"
                        if step.heading is not None and self._heading_fn is not None:
                            current = self._heading_fn()
                            now_mono = time.monotonic()
                            if current is None:
                                # 本拍没有 IMU 读数：不转，等超时
                                self._imu_lost = True
                                self._heading_error = None
                            else:
                                err_now = (step.heading - current + 180.0) % 360.0 - 180.0
                                # 冻结检测只在**粗转阶段**（误差 > 精调窗口）生效：
                                # 精调阶段占空很小、转速本来就慢，朝向短时间不变是正常的。
                                fine_win = float(getattr(self, "_turn_fine_deg", 6.0))
                                intend_turn = abs(err_now) > fine_win and self._turn_flips < 3
                                if intend_turn:
                                    if (self._last_heading is not None
                                            and abs(current - self._last_heading) < 0.05):
                                        if now_mono - self._last_heading_at > 0.5:
                                            self._imu_lost = True
                                    else:
                                        self._last_heading = current
                                        self._last_heading_at = now_mono
                                        self._imu_lost = False
                                else:
                                    # 精调阶段/已收敛：朝向不变不算失效；
                                    # 但若长时间纹丝不动（7.6V 低压下 5% 占空可能转不动），
                                    # 就放弃这一步的精度，继续跑后面的动作，而不是中止整个演示。
                                    stuck = (self._last_heading is not None
                                             and abs(current - self._last_heading) < 0.05)
                                    if stuck:
                                        # 注意：卡住期间**不能**刷新 _last_heading_at，
                                        # 否则"已卡住多久"永远算不出来（之前就是这个 bug）。
                                        # 1.5s：给"卡住加力"留出时间（0.4s 起加力，1.0s 到 18%）
                                        if (now_mono - self._last_heading_at) > 1.5:
                                            if abs(err_now) > float(getattr(self, "_turn_done_deg", 1.2)):
                                                self._turn_flips = max(self._turn_flips, 3)
                                    else:
                                        self._last_heading = current
                                        self._last_heading_at = now_mono
                                    self._imu_lost = False
                            if current is not None:
                                err = (step.heading - current + 180.0) % 360.0 - 180.0
                                sign = 1 if err > 0 else (-1 if err < 0 else 0)
                                if sign and self._last_err_sign and sign != self._last_err_sign:
                                    self._turn_flips += 1
                                if sign:
                                    self._last_err_sign = sign
                                self._heading_error = err
                                step_elapsed = self._elapsed - start
                                rate = None
                                if self._rate_fn is not None:
                                    rate = self._rate_fn()
                                settled = rate is None or abs(rate) <= self._rate_tol
                                near = abs(err) <= max(2.0, float(getattr(self, "_turn_done_deg", 1.2)))
                                gave_up = self._turn_flips >= 3 and abs(err) <= float(
                                    getattr(self, "_turn_fine_deg", 6.0))
                                if (near and settled) or gave_up or step_elapsed >= step.duration or self._imu_lost:
                                    reason = "arrived"
                                    if step_elapsed >= step.duration:
                                        reason = "timeout"
                                    elif gave_up and abs(err) > 1.5:
                                        reason = "give_up"
                                    if self._imu_lost:
                                        reason = "imu_stalled"
                                    self._turn_log.append({
                                        "target": round(float(step.heading), 1),
                                        "achieved": round(float(current), 1),
                                        "error": round(float(err), 1),
                                        "rate": None if rate is None else round(float(rate), 1),
                                        "seconds": round(float(step_elapsed), 2),
                                        "reason": reason,
                                    })
                                    del self._turn_log[:-12]
                                    if self._imu_lost:
                                        # IMU 在转角中失效：后面的动作已经没有意义，直接中止
                                        self._running = False
                                        self._paused = False
                                        self._finished = True
                                        self._abort_reason = "IMU lost during turn - demo aborted"
                                        self._heading_error = None
                                        if self._started_wall:
                                            self._last_run_s = round(time.monotonic() - self._started_wall, 2)
                                        return
                                    self._elapsed = total + 1e-6   # 直接跳到下一步
                                    advanced = True
                                    break
                        # 直行/平移段：有 IMU 就做航向保持（只加很小的自转修正）
                        # 只在**直行**（0°/180°）时做航向保持：
                        #   · 斜行(45/135/225/315) 只有 2 轮出力，叠加 rot 会让四轮互相顶 → 车跑不动
                        #   · 平移(90°/270°) 的横向分量也会被 rot 干扰，保持"纯平移"更稳
                        straight_only = (int(step.angle) % 180 == 0)
                        if (step.heading is None and step.velocity and not step.drift
                                and straight_only
                                and self._heading_hold and self._heading_fn is not None):
                            cur = self._heading_fn()
                            if cur is None:
                                self._hold_rot = 0
                                self._hold_state = "no-imu"
                            else:
                                if self._hold_target is None:
                                    self._hold_target = float(cur)
                                err_h = (self._hold_target - float(cur) + 180.0) % 360.0 - 180.0
                                # rot 为正 = 左转 = 朝向增大，所以修正量与误差同号
                                corr = self._hold_gain * err_h
                                self._hold_rot = int(max(-self._hold_max_rot,
                                                         min(self._hold_max_rot, round(corr))))
                                self._hold_state = "active"
                        return
                if not advanced:
                    break
            # 全部步骤播放完毕
            self._running = False
            self._paused = False
            self._finished = True
            self._index = len(self._seq.steps) - 1
            if self._started_wall:
                self._last_run_s = round(time.monotonic() - self._started_wall, 2)

    def current_target(self) -> Optional[Dict]:
        """返回当前步骤的控制量；未运行时返回 None。"""

        with self._lock:
            if not self._running or self._seq is None:
                return None
            step = self._seq.steps[min(self._index, len(self._seq.steps) - 1)]
            if step.heading is not None and self._imu_lost:
                # IMU 读数失效：立刻停止转动（宁可少转，不能盲转）
                return {"mode": "vector", "angle": 0, "velocity": 0, "rot": 0, "drift": False}
            if step.heading is not None and self._heading_fn is not None and self._heading_error is not None:
                err = float(self._heading_error)
                rate = 0.0
                if self._rate_fn is not None:
                    cur_rate = self._rate_fn()
                    if cur_rate is not None:
                        rate = float(cur_rate)
                abs_err = abs(err)
                fine_window = float(getattr(self, "_turn_fine_deg", 6.0))
                fine_rot = int(getattr(self, "_turn_fine_rot", 5))
                if abs_err <= float(getattr(self, "_turn_done_deg", 1.2)) and abs(rate) <= self._rate_tol:
                    # 已到位且已经稳住：收手。
                    # 必须同时看转速——只看角度会在车还高速旋转时断油，必然冲过站。
                    rot = 0
                elif abs_err <= fine_window:
                    # 精调阶段：带阻尼的 PD（不再要求"转速<=10°/s"，否则一加力就退出精调）
                    step_start = sum(s.duration for s in self._seq.steps[: self._index]) if self._seq else 0.0
                    fine_elapsed = self._elapsed - step_start
                    gain = 2 if (fine_elapsed > 1.8 and abs_err > 1.5) else 1
                    boost = min(18, fine_rot * gain)
                    kp_fine = float(getattr(self, "_turn_kp_fine", 1.6))
                    cmd_fine = kp_fine * err - 0.18 * rate
                    rot = int(max(-boost, min(boost, round(cmd_fine))))
                    # 最小脉冲 + 卡住加力：
                    #   车停着不动时，克服静摩擦需要的占空会比"行进中"更高，
                    #   所以按"距上次朝向变化的时间"逐步加力（最多 +10%）。
                    min_pulse = int(getattr(self, "_turn_min_pulse", 8))
                    stall_s = time.monotonic() - float(getattr(self, "_last_heading_at", time.monotonic()))
                    if abs_err > float(getattr(self, "_turn_done_deg", 1.2)):
                        if stall_s > 0.45:
                            # 持续小推力推不动（静摩擦）→ 改成"短促大扭矩脉冲 + 停顿"的踢法：
                            #   0.12s @ 22% 出力，然后 0.3s 停，循环。让轮子"突跳"越过静摩擦。
                            cycle = stall_s % 0.42
                            if cycle < 0.12:
                                rot = 22 if err > 0 else -22
                            else:
                                rot = 0
                        else:
                            need = min(18, min_pulse + int(max(0.0, stall_s - 0.4) * 8))
                            if abs(rot) < need:
                                rot = need if err > 0 else -need
                else:
                    cmd = self._turn_kp * err - self._turn_kd * rate
                    # 接近目标时自动降速：误差 25° 以内按比例限幅（最低 8%），远处仍用满速
                    if abs_err > 25.0:
                        approach = int(step.rot_limit)
                    else:
                        approach = int(max(8, min(step.rot_limit, round(abs_err * 0.7))))
                    rot = int(max(-approach, min(approach, round(cmd))))
                return {"mode": "vector", "angle": 0, "velocity": 0, "rot": rot, "drift": False}
            base = step.target()
            if self._hold_rot and step.heading is None and base.get("velocity"):
                # 航向保持：把微小的自转修正叠加到当前动作上
                base = dict(base)
                base["rot"] = int(max(-60, min(60, int(base.get("rot") or 0) + self._hold_rot)))
            return base

    def is_running(self) -> bool:
        with self._lock:
            return self._running

    def snapshot(self) -> Dict:
        with self._lock:
            return self._snapshot_locked()

    # ---- 内部方法 ----
    def _snapshot_locked(self) -> Dict:
        if self._seq is None:
            return {
                "available": [seq.to_dict() for seq in self._sequences.values()],
                "running": False,
                "paused": False,
                "finished": False,
                "name": None,
                "title": None,
                "description": None,
                "index": 0,
                "step_count": 0,
                "caption": "",
                "progress": 0.0,
                "remaining": 0.0,
                "duration": 0.0,
                "last_run_s": self._last_run_s,
                "turns": list(self._turn_log),
                "abort_reason": self._abort_reason,
            }
        seq = self._seq
        step = seq.steps[min(self._index, len(seq.steps) - 1)]
        total = seq.duration or 1.0
        elapsed = min(self._elapsed, total)
        remaining = max(0.0, total - elapsed)
        return {
            "available": [s.to_dict() for s in self._sequences.values()],
            "running": self._running,
            "paused": self._paused,
            "finished": self._finished,
            "name": seq.name,
            "title": seq.title,
            "description": seq.description,
            "index": self._index,
            "step_count": len(seq.steps),
            "caption": step.caption if self._running or self._finished else "",
            "progress": round(elapsed / total, 4),
            "remaining": round(remaining, 2),
            "duration": seq.duration,
            "last_run_s": self._last_run_s,
            "turns": list(self._turn_log),
            "heading_hold": {
                "state": self._hold_state,
                "target": None if self._hold_target is None else round(self._hold_target, 1),
                "rot": int(self._hold_rot),
            },

            "abort_reason": self._abort_reason,
            "steps": [s.to_dict() for s in seq.steps],
        }


def sequence_timeline(seq: TypingSequence[Step]) -> List[Tuple[float, str]]:
    """工具函数：把序列展开成 (累计秒, 解说) 列表，便于文档/测试校对。"""

    total = 0.0
    out: List[Tuple[float, str]] = []
    for step in seq:
        total += step.duration
        out.append((round(total, 2), step.caption))
    return out
