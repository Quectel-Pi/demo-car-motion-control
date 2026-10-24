"""车轮驱动层：麦轮运动学 → 幻尔树莓派5扩展板的 4 路占空指令。

板子跑的是**自带的 STM32 固件**（不烧录），上位机通过 ``0xAA 0x55`` 数据包下发电机占空
（``set_motor_duty``，范围 -100~100），板子每 ~200ms 主动上报一帧 SYS（电池电压）。

职责边界
--------
* 对外只暴露「设置目标 / 单轮点检 / 停车 / 急停 / 状态快照」，Web 层不直接碰串口；
* 每轮独立占空 ⇒ **任意方向角、边走边转、漂移、单轮点检全部支持**（这一版不再有 8 档限制）；
* 指令只在状态变化时下发（外加 1 秒一次一致性刷新），刷新与变化都受 ``command_hz`` 限速；
* SYS 上报同时当心跳：超过 ``telemetry_timeout_s`` 没收到任何帧就标链路异常；
* 无硬件时自动降级为仿真板，状态里明确标注，绝不假装硬件已动作。

安全
----
官方 SDK 没有"指令超时保护"的说明，所以本工程按"板子会保持最后的占空"来处理：
浏览器心跳超时 / 进程退出 / 急停都会显式下发四轮 0 占空，现场仍需保留电源开关。
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any, Dict, List, Optional

from . import kinematics, protocol
from .demo import DemoRunner
from .transport import BaseTransport, TransportError, build_transport


class WheelDriver:
    def __init__(self, cfg: Dict[str, Any], transport: Optional[BaseTransport] = None) -> None:
        self.cfg = cfg
        ctrl = cfg.get("control", {})
        serial_cfg = cfg.get("serial", {})
        self._board_cfg = cfg.get("board", {})

        self._max_velocity = int(ctrl.get("max_velocity", 80))
        self._max_rot = int(ctrl.get("max_rot", 60))
        self._invert_rot = bool(ctrl.get("invert_rot", False))
        self._invert_angle = bool(ctrl.get("invert_angle", False))
        self._invert_motors = bool(ctrl.get("invert_motors", False))
        self._heartbeat_timeout = float(ctrl.get("heartbeat_timeout_s", 8.0))
        self._handshake_timeout = float(ctrl.get("handshake_timeout_s", 2.5))
        self._stream_hz = float(ctrl.get("stream_hz", 20) or 20)
        self._idle_hz = float(ctrl.get("idle_stream_hz", 5) or 5)
        self._command_hz = float(ctrl.get("command_hz", 20) or 20)
        self._refresh_s = float(ctrl.get("refresh_s", 1.0))

        # ---- 扩展板参数 ----
        #: 车体序 [FL, FR, RR, RL] 每个轮子实际接在哪个电机编号（M1..M4）上 —— 实车标定
        order = list(self._board_cfg.get("motor_order") or [1, 2, 3, 4])
        self._motor_order = [int(m) if 1 <= int(m) <= 4 else i + 1 for i, m in enumerate(order[:4])]
        while len(self._motor_order) < 4:
            self._motor_order.append(len(self._motor_order) + 1)
        #: 逐电机方向标志（按电机编号 M1..M4）：右侧电机机械上是镜像安装，实车接线常需要取反
        invert = list(self._board_cfg.get("motor_invert") or [False, False, False, False])
        self._motor_invert = [bool(i < len(invert) and invert[i]) for i in range(4)]
        self._motor_sign = [-1.0 if inv else 1.0 for inv in self._motor_invert]
        self._deadzone_pct = float(self._board_cfg.get("deadzone_pct", 15.0))
        self._apply_deadzone = bool(self._board_cfg.get("apply_deadzone", True))
        self._max_speed_mm_s = float(self._board_cfg.get("max_speed_mm_s", 200.0))
        self._wheel_diameter_mm = float(self._board_cfg.get("wheel_diameter_mm", 60.0))
        self._battery_low_v = float(self._board_cfg.get("battery_low_v", 6.9))
        self._battery_full_v = float(self._board_cfg.get("battery_full_v", 8.4))
        self._battery_none_v = float(self._board_cfg.get("battery_none_v", 1.0))
        self._telemetry_timeout = float(self._board_cfg.get("telemetry_timeout_s", 2.0))

        self._serial_cfg = serial_cfg
        self._force_sim = bool(cfg.get("sim", {}).get("force", False))
        self._sim_kwargs = {"battery_v": float(cfg.get("sim", {}).get("battery_v", 7.9))}

        demo_cfg = cfg.get("demo", {}) or {}
        home_cfg = dict(demo_cfg.get("home", {}) or {})
        # 归位演示要用到"速度→mm/s"的估算参数，这里补进去（缺省值在 demo.py 里兜底）
        home_cfg.setdefault("deadzone_pct", float(cfg.get("board", {}).get("deadzone_pct", 15.0)))
        home_cfg.setdefault("apply_deadzone", bool(cfg.get("board", {}).get("apply_deadzone", True)))
        home_cfg.setdefault("max_speed_mm_s", float(cfg.get("board", {}).get("max_speed_mm_s", 200.0)))
        self._imu_cfg = dict(cfg.get("imu", {}) or {})
        self._imu = None                    # wheelctl.imu.Heading 实例（可选硬件）
        self._imu_ok = False                # IMU 当前是否可用（掉线时自动重连）
        self._imu_drops = 0                 # 掉线次数（现场判断接触/干扰严重程度）
        # 堵转保护：运动时电池电压明显跌落（电机堵转电流大）就停车
        self._stall_cfg = dict(cfg.get("stall_guard", {}) or {})
        self._stall_baseline_mv = None      # 静止时的电压基线
        self._stall_sag_since = None        # 跌落开始时间
        self._stall_trips = 0               # 触发次数
        self._stall_last = None             # 最近一次触发信息
        self._sag_max = 0.0                 # 本次运动中观察到的最大电压跌落
        self._demo_was_running = False      # 用于检测"演示刚结束"
        self._event_log_path = os.path.expanduser("~/wheel-demo-events.log")
        self._last_turn_count = 0
        self._last_demo_name = None
        self._last_step_index = -1
        self._last_hold_state = None
        self._demo_start_count = 0
        self._quiet_when_idle = bool(cfg.get("board", {}).get("quiet_when_idle", True))
        self._imu_heading = None            # 当前偏航角（度）；None = 没有 IMU
        self._imu_rate = 0.0                # 当前角速度（°/s）
        self._imu_bias = 0.0
        self._imu_error = None
        self._imu_thread = None
        hidden = [str(x) for x in (demo_cfg.get("hidden") or [])]
        self.demo = DemoRunner(
            velocity=int(demo_cfg.get("velocity", 55)),
            rot=int(demo_cfg.get("rot", 40)),
            home=home_cfg,
            heading_fn=self.heading,
            rate_fn=self.imu_rate,
            hidden=hidden,
            heading_hold=bool(self._imu_cfg.get("heading_hold", True)),
            hold_gain=float(self._imu_cfg.get("hold_gain", 0.6)),
            hold_max_rot=int(self._imu_cfg.get("hold_max_rot", 3)),
            turn_tol=float(self._imu_cfg.get("turn_tol_deg", 2.5)),
            turn_lead=float(self._imu_cfg.get("turn_lead_deg", 2.0)),
            rate_tol=float(self._imu_cfg.get("settle_rate_dps", 10.0)),
            turn_kp=float(self._imu_cfg.get("turn_kp", 1.1)),
            turn_kd=float(self._imu_cfg.get("turn_kd", 0.30)),
            turn_deadband=float(self._imu_cfg.get("turn_deadband_deg", 3.0)),
            turn_fine_deg=float(self._imu_cfg.get("turn_fine_deg", 6.0)),
            turn_fine_rot=int(self._imu_cfg.get("turn_fine_rot", 5)),
            turn_done_deg=float(self._imu_cfg.get("turn_done_deg", 1.2)),
            turn_min_pulse=int(self._imu_cfg.get("turn_min_pulse", 8)),
            turn_kp_fine=float(self._imu_cfg.get("turn_kp_fine", 1.6)),
        )
        self._lock = threading.RLock()
        self._transport: Optional[BaseTransport] = transport
        self._parser = protocol.PacketParser()

        self._mode = "vector"
        self._target = {"angle": 0, "velocity": 0, "rot": 0, "drift": False}
        self._raw: List[int] = [0, 0, 0, 0]
        self._estop = False
        self._heartbeat_stale = False
        self._last_client = time.monotonic()
        self._started_at = time.monotonic()
        self._last_frame: Optional[bytes] = None
        self._last_frame_at = 0.0

        self._stats = {"sent": 0, "errors": 0, "last_line": "", "last_error": None, "hz": self._stream_hz}
        self._link = {
            "mode": "unknown",
            "port": "-",
            "baud": int(serial_cfg.get("baud", 1000000)),
            "connected": False,
            "handshake": "pending",
            "error": None,
            "backend": "",
            "reconnects": 0,
            "firmware": "RasAdapter5A (on-board STM32 firmware, unmodified)",
        }
        self._frame_stamps: List[float] = []      # 最近收到的帧时间戳（估算上报频率）
        self._telemetry: Dict[str, Any] = {
            "frames": 0,
            "reports": 0,
            "acks": 0,
            "battery_v": None,
            "battery_mv": None,
            "last_frame_at": None,
            "last_report_at": None,
            "bad_frames": 0,
            "unknown": [],
        }
        self._thread: Optional[threading.Thread] = None
        self._stopping = threading.Event()

    # ------------------------------------------------------------------ 生命周期
    def start(self) -> None:
        self._connect()
        self._start_imu()
        self._thread = threading.Thread(target=self._stream_loop, name="wheel-stream", daemon=True)
        self._thread.start()

    def shutdown(self) -> None:
        self._stopping.set()
        if self._imu is not None:
            try:
                self._imu.close()
            except Exception:               # noqa: BLE001 - 关闭失败不影响退出
                pass
            self._imu = None
        if self._thread is not None:
            self._thread.join(timeout=1.5)
        self._send_stop_frames()
        try:
            if self._transport is not None:
                self._transport.close()
        except Exception:
            pass
        with self._lock:
            self._link["connected"] = False

    # ------------------------------------------------------------------ IMU（可选）
    def _start_imu(self) -> None:
        """按需打开 MPU6050；没插/打不开就静默降级（演示回落为按时长转角）。"""
        if not bool(self._imu_cfg.get("enabled", True)):
            return
        try:
            from . import imu as imu_mod
        except Exception as exc:            # noqa: BLE001 - 平台不支持（如 Windows）
            self._imu_error = f"imu module unavailable: {exc}"
            return
        bus = int(self._imu_cfg.get("bus", 1))
        addr = int(self._imu_cfg.get("addr", 0x68))
        sign = float(self._imu_cfg.get("sign", 1.0))
        try:
            dev = imu_mod.MPU6050(bus=bus, addr=addr).open().begin()
            head = imu_mod.Heading(dev, gyro_sign=sign)
            if dev.who_am_i() not in (0x68, 0x69, 0x70, 0x71, 0x73, 0x98):
                # 地址能读但 ID 不对：仍然尝试使用
                self._imu_error = f"unexpected WHO_AM_I=0x{dev.who_am_i():02x}"
        except Exception as exc:            # noqa: BLE001 - 硬件缺失属正常情况
            self._imu_error = f"{type(exc).__name__}: {exc}"
            return

        self._imu = head
        rate = float(self._imu_cfg.get("rate_hz", 100))
        period = 1.0 / max(20.0, min(200.0, rate))

        def loop() -> None:
            # 关键：下面会重新赋值 head（重连时重建对象），必须声明 nonlocal，
            # 否则 head 会变成 loop 的局部变量 → UnboundLocalError，采样线程直接崩。
            nonlocal head
            # 外层：掉线后每 2 秒尝试重连（I2C 接触不良/EMI 都能自愈）
            while not self._stopping.is_set():
                try:
                    if self._imu is None:
                        self._imu = head
                    self._imu_bias = head.calibrate(float(self._imu_cfg.get("calib_s", 2.0)))
                    self._imu_error = None if getattr(head, "last_calib_ok", False) else "calibration suspect (sensor moved)"
                    self._imu_ok = True
                    fails = 0
                    while not self._stopping.is_set():
                        try:
                            self._imu_heading = head.update()
                            self._imu_rate = float(getattr(head, "rate", 0.0))
                            if fails:
                                fails = 0
                                self._imu_error = None
                        except Exception as exc:    # noqa: BLE001 - 单次抖动不算掉线
                            fails += 1
                            self._imu_error = f"read failed x{fails}: {exc}"
                            if fails >= 10:
                                raise
                        time.sleep(period)
                except Exception as exc:        # noqa: BLE001 - 掉线：标记不可用并重连
                    self._imu_drops += 1
                    self._imu_ok = False
                    self._imu_heading = None    # 上层（演示）据此立刻停止盲转
                    self._imu_rate = 0.0
                    self._imu_error = f"imu offline: {exc} (retrying)"
                    self._log_event(f"IMU offline: {exc}")
                    try:
                        head.imu.close()
                    except Exception:           # noqa: BLE001
                        pass
                    time.sleep(2.0)
                    try:
                        from . import imu as imu_mod
                        dev = imu_mod.MPU6050(bus=bus, addr=addr).open().begin()
                        head = imu_mod.Heading(dev, gyro_sign=sign)
                        self._imu = head
                    except Exception as exc2:   # noqa: BLE001
                        self._imu_error = f"reconnect failed: {exc2}"
                        time.sleep(2.0)

        self._imu_thread = threading.Thread(target=loop, name="wheel-imu", daemon=True)
        self._imu_thread.start()

    def heading(self):
        """当前偏航角（度）；没有 IMU / 掉线时返回 None。"""
        return self._imu_heading if self._imu_ok else None

    def imu_rate(self):
        """当前角速度（°/s）；没有 IMU 时返回 None。"""
        return None if self._imu is None else self._imu_rate

    def measure_turn(self, rot: int = 30, seconds: float = 1.0, settle_s: float = 1.0) -> Dict[str, Any]:
        """原地转一小段，用 IMU 量实际转过的角度（标定 deg_per_s_per_rot 用）。会短暂驱动电机。"""
        rot_i = int(max(10, min(self._max_rot, rot)))
        secs = float(max(0.2, min(3.0, seconds)))
        before = None if self._imu_heading is None else float(self._imu_heading)
        if before is None:
            return {"ok": False, "error": "IMU unavailable"}
        self.set_target(angle=0, velocity=0, rot=rot_i)
        time.sleep(secs)
        self.stop()
        time.sleep(max(0.3, settle_s))
        after = None if self._imu_heading is None else float(self._imu_heading)
        if after is None:
            return {"ok": False, "error": "IMU lost"}
        delta = ((after - before + 180.0) % 360.0) - 180.0
        k = delta / (rot_i * secs) if rot_i * secs else 0.0
        return {
            "ok": True,
            "rot": rot_i,
            "seconds": round(secs, 3),
            "heading_before": round(before, 2),
            "heading_after": round(after, 2),
            "angle": round(delta, 2),
            "deg_per_s_per_rot": round(k, 3),
            "suggested_turn_s_for_90": round(90.0 / (rot_i * k), 3) if k > 0.05 else None,
        }

    def imu_calibrate(self) -> Dict[str, Any]:
        """车静止时重新校准零偏并把当前朝向记为 0°（跑演示前调用）。"""
        if self._imu is None:
            return self.status()
        try:
            self._imu_bias = self._imu.calibrate(float(self._imu_cfg.get("calib_s", 2.0)))
            self._imu_error = None if getattr(self._imu, "last_calib_ok", False) else "calibration suspect (sensor moved)"
        except Exception as exc:            # noqa: BLE001
            self._imu_error = f"calib failed: {exc}"
        self._imu_heading = 0.0
        return self.status()

    def imu_zero(self) -> Dict[str, Any]:
        """把当前朝向记为 0°（演示开始前用）。"""
        if self._imu is not None:
            self._imu.reset()
            self._imu_heading = 0.0
        return self.status()

    # ------------------------------------------------------------------ 链路
    def _connect(self) -> None:
        if self._transport is None:
            port = self._serial_cfg.get("port", "auto")
            baud = int(self._serial_cfg.get("baud", 1000000))
            cands = self._serial_cfg.get("candidates") or None
            try:
                self._transport = build_transport(port, baud, self._force_sim, self._sim_kwargs, cands)
            except Exception as exc:
                with self._lock:
                    self._link.update({"mode": "none", "connected": False, "error": str(exc)})
                return

        transport = self._transport
        try:
            if not transport.is_open:
                transport.open()
        except Exception as exc:
            with self._lock:
                self._link.update({
                    "mode": transport.mode,
                    "connected": False,
                    "error": str(exc),
                    "handshake": "failed",
                })
            return

        handshake = "sim" if transport.mode == "sim" else "pending"
        error: Optional[str] = None

        try:
            # 先停车（防止上一次异常退出残留占空），再等板子的第一条上报
            transport.write(protocol.cmd_stop())
            deadline = time.monotonic() + self._handshake_timeout
            saw_any = False
            while time.monotonic() < deadline:
                if self._drain_once():
                    saw_any = True
                with self._lock:
                    if self._telemetry.get("battery_v") is not None:
                        handshake = "battery"
                        break
                time.sleep(0.02)
            if handshake == "pending":
                handshake = "reply" if saw_any else "no-response"
        except TransportError as exc:
            handshake = "failed"
            error = str(exc)
        except Exception as exc:  # pragma: no cover - 防御性
            handshake = "failed"
            error = str(exc)

        with self._lock:
            self._link.update({
                "mode": transport.mode,
                "port": transport.label,
                "connected": transport.is_open,
                "handshake": handshake,
                "backend": getattr(transport, "backend", "") or transport.mode,
                "baud": int(getattr(transport, "baud", self._link.get("baud", 1000000))),
            })
            if error:
                self._link["error"] = error
            elif transport.is_open:
                self._link["error"] = None
            reason = getattr(transport, "fallback_reason", None)
            if reason and transport.mode == "sim":
                self._link["error"] = str(reason)
        self._last_frame = None
        self._last_frame_at = 0.0

    def _ensure_connected(self) -> None:
        transport = self._transport
        if transport is not None and transport.is_open:
            return
        if transport is not None:
            try:
                transport.close()
            except Exception:
                pass
        with self._lock:
            self._link["reconnects"] += 1
        self._transport = None
        self._parser = protocol.PacketParser()
        self._connect()

    # ------------------------------------------------------------------ 收发
    def _drain_once(self) -> int:
        transport = self._transport
        if transport is None or not transport.is_open:
            return 0
        try:
            chunk = transport.read_bytes()
        except Exception as exc:
            with self._lock:
                self._stats["errors"] += 1
                self._stats["last_error"] = f"read failed: {exc}"
            return 0
        if not chunk:
            return 0
        frames = self._parser.feed(chunk)
        for frame in frames:
            self._handle_frame(frame)
        with self._lock:
            self._telemetry["bad_frames"] = self._parser.bad_frames
        return len(frames)

    def _report_hz(self, window_s: float = 3.0) -> float:
        """按最近 window_s 秒内的帧数估算上报频率（比用单帧间隔稳定）。"""
        now = time.monotonic()
        stamps = self._frame_stamps
        while stamps and now - stamps[0] > window_s:
            stamps.pop(0)
        if len(stamps) < 2:
            return 0.0
        span = stamps[-1] - stamps[0]
        if span <= 0:
            return 0.0
        return round((len(stamps) - 1) / span, 1)

    def _handle_frame(self, frame: protocol.Frame) -> None:
        now = time.monotonic()
        decoded = protocol.decode_report(frame)
        with self._lock:
            self._telemetry["frames"] += 1
            self._telemetry["last_frame_at"] = now
            stamps = self._frame_stamps
            stamps.append(now)
            if len(stamps) > 64:
                del stamps[:-64]
            if decoded is None:
                unknown = self._telemetry["unknown"]
                text = frame.raw.hex()
                if text not in unknown:
                    unknown.append(text)
                    del unknown[:-6]
                return
            if decoded["kind"] == "battery":
                self._telemetry["reports"] += 1
                self._telemetry["battery_v"] = decoded["voltage_v"]
                self._telemetry["battery_mv"] = decoded["millivolt"]
                self._telemetry["last_report_at"] = now
                if self._link.get("handshake") != "battery":
                    self._link["handshake"] = "battery"
                    self._link["error"] = None
                self._link["connected"] = True
            else:
                self._telemetry["acks"] += 1

    def _send_stop_frames(self) -> None:
        transport = self._transport
        if transport is None or not transport.is_open:
            return
        frame = protocol.cmd_stop()
        try:
            transport.write(frame)
            with self._lock:
                self._last_frame = frame
                self._last_frame_at = time.monotonic()
                self._stats["sent"] += 1
                self._stats["last_line"] = frame.hex()
        except Exception:
            pass

    # ------------------------------------------------------------------ 指令组装
    def _frame_of(self, duties_car: List[float]) -> bytes:
        """车体序占空 → 电机帧。

        实车接线 **不一定是 1:1**（本地这台是 M1=左前 / M3=右前 / M4=右后 / M2=左后），
        所以按 ``board.motor_order``（车体序 → 电机编号）重排后再按 1..4 下发。
        """

        # 整车反向（invert_motors）与逐电机反向（motor_invert）都折在这一层：
        # 状态里的 value 仍按"车体语义"（前进为正），command 才是真正下发的带符号值。
        invert = [bool(a) ^ bool(b) for a, b in zip(self._motor_invert, [self._invert_motors] * 4)]
        by_motor = protocol.map_car_to_motor(duties_car, self._motor_order, invert)
        return protocol.cmd_motor_duty_all(by_motor)

    def _duties_for(self, pct_car: List[float]) -> List[float]:
        """车体序强度（%）→ 下发占空（%）：死区补偿 + 整车反向。"""

        values = [float(v) for v in pct_car]
        if self._apply_deadzone:
            return [protocol.apply_deadzone(v, self._deadzone_pct) for v in values]
        return [float(protocol.clamp(v, protocol.DUTY_MIN, protocol.DUTY_MAX)) for v in values]

    def _hardware_values(self, target: Dict[str, Any]) -> Dict[str, Any]:
        angle = int(target["angle"])
        rot = int(target["rot"])
        if self._invert_angle:
            angle = (360 - angle) % 360
        if self._invert_rot:
            rot = -rot
        return {
            "angle": angle,
            "velocity": int(target["velocity"]),
            "rot": rot,
            "drift": bool(target["drift"]),
        }

    def _effective(self) -> Dict[str, Any]:
        with self._lock:
            mode = self._mode
            target = dict(self._target)
            raw = list(self._raw)

        if mode == "raw":
            pct_car = [float(kinematics.clamp(v, -100, 100)) for v in raw]
            duties = self._duties_for(pct_car)
            kind = "raw" if any(pct_car) else "stop"
            applied = {"angle": 0, "velocity": 0, "rot": 0, "drift": False}
            disp = {"pct": pct_car, "duties": duties, **applied}
        else:
            hw = self._hardware_values(target)
            pct_car = [float(v) for v in kinematics.wheel_speeds(**hw)]
            duties = self._duties_for(pct_car)
            kind = "stop" if not any(pct_car) else ("rotate" if not hw["velocity"] else "move")
            applied = hw
            # 展示口径 = **硬件口径**：轮子卡片上的占空/方向必须和电机的实际动作一致。
            # （本底盘需要 invert_angle 镜像才能真正走对方向，所以"哪个轮子在动"要用镜像后的结果；
            #  而箭头/文字描述仍用用户意图的角度，见下面的 angle/velocity/rot。）
            disp = {
                "pct": pct_car,
                "duties": duties,
                "angle": int(target["angle"]),
                "velocity": int(target["velocity"]),
                "rot": int(target["rot"]),
                "drift": bool(target["drift"]),
            }

        frame = self._frame_of(duties)
        return {
            "mode": mode,
            "kind": kind,
            "frame": frame,
            "line": frame.hex(),
            "pct": pct_car,
            "duties": duties,
            "active": kind != "stop",
            "applied": applied,
            "display": disp,
            "target": target,
            "raw": raw,
        }

    def _compose(self) -> Dict[str, Any]:
        with self._lock:
            if self.demo.is_running():
                demo_target = self.demo.current_target()
                if demo_target is not None:
                    self._apply_demo_target(demo_target)
            self._check_heartbeat()
        return self._effective()

    def _apply_demo_target(self, demo_target: Dict[str, Any]) -> None:
        if demo_target.get("mode") == "raw":
            self._mode = "raw"
            self._raw = [int(x) for x in demo_target.get("wheels", [0, 0, 0, 0])][:4]
        else:
            self._mode = "vector"
            self._target = {
                "angle": int(demo_target.get("angle", 0)),
                "velocity": int(demo_target.get("velocity", 0)),
                "rot": int(demo_target.get("rot", 0)),
                "drift": bool(demo_target.get("drift", False)),
            }

    def _check_heartbeat(self) -> None:
        age = time.monotonic() - self._last_client
        if age <= self._heartbeat_timeout:
            self._heartbeat_stale = False
            return
        if self.demo.is_running():
            self._heartbeat_stale = False
            return
        if self._has_motion():
            self._heartbeat_stale = True
            self._mode = "vector"
            self._target = {"angle": 0, "velocity": 0, "rot": 0, "drift": False}
            self._raw = [0, 0, 0, 0]

    def _has_motion(self) -> bool:
        if self._mode == "raw":
            return any(self._raw)
        return bool(self._target["velocity"] or self._target["rot"])

    # ------------------------------------------------------------------ 指令流
    def _stream_loop(self) -> None:
        min_interval = 1.0 / max(1.0, self._command_hz)
        while not self._stopping.is_set():
            started = time.monotonic()
            try:
                self._ensure_connected()
                self._drain_once()
                if self.demo.is_running():
                    self._demo_was_running = True
                elif self._demo_was_running:
                    # 演示刚结束（正常结束或中止）：强制目标归零并发停帧，
                    # 否则最后一次"精调小转动"会一直生效 → 电机持续 humming
                    self._demo_was_running = False
                    self.stop()

                cmd = self._compose()
                self.demo.tick()
                self._check_stall()
                self._log_demo_events()

                transport = self._transport
                if transport is not None and transport.is_open:
                    frame = cmd["frame"]
                    changed = frame != self._last_frame
                    due = (changed and (started - self._last_frame_at) >= min_interval) or \
                          ((started - self._last_frame_at) >= self._refresh_s)
                    if self._quiet_when_idle and not changed and not self._has_motion():
                        # 已经停住且帧没变化：不再周期性重发，避免电机听到 PWM 啸叫
                        due = False
                    if due:
                        transport.write(frame)
                        with self._lock:
                            self._stats["sent"] += 1
                            self._stats["last_line"] = cmd["line"]
                        self._last_frame = frame
                        self._last_frame_at = started
                else:
                    with self._lock:
                        self._stats["last_error"] = "link not connected, command not sent"
            except Exception as exc:
                with self._lock:
                    self._stats["errors"] += 1
                    self._stats["last_error"] = f"{type(exc).__name__}: {exc}"
                try:
                    if self._transport is not None:
                        self._transport.close()
                except Exception:
                    pass

            hz = self._stream_hz if self._has_motion() else self._idle_hz
            with self._lock:
                self._stats["hz"] = hz
            elapsed = time.monotonic() - started
            if self._stopping.wait(max(0.0, 1.0 / hz - elapsed)):
                return

    # ------------------------------------------------------------------ 事件日志
    def _log_event(self, text: str) -> None:
        """把演示/保护相关事件写进 ~/wheel-demo-events.log（现场排查用，200KB 轮转）。"""
        try:
            import datetime
            if os.path.getsize(self._event_log_path) > 200_000 if os.path.exists(self._event_log_path) else False:
                os.replace(self._event_log_path, self._event_log_path + ".old")
        except Exception:                   # noqa: BLE001
            pass
        try:
            with open(self._event_log_path, "a", encoding="utf-8") as fh:
                fh.write(datetime.datetime.now().strftime("%m-%d %H:%M:%S ") + text + "\n")
        except Exception:                   # noqa: BLE001
            pass

    def _log_demo_events(self) -> None:
        """对比演示快照，把"开始/每次转角/结束/中止"写日志。"""
        snap = self.demo.snapshot()
        name = snap.get("name")
        turns = snap.get("turns") or []
        if snap.get("running") and name and self._last_demo_name != name:
            self._last_demo_name = name
            self._last_turn_count = 0
            self._last_step_index = -1
            self._demo_start_count += 1
            self._log_event(f"demo start: {name} (#{self._demo_start_count})")
        # 转角过程轨迹：转角步每 0.4s 记一次（朝向/误差/指令），便于分析"最后几度为什么转不动"
        try:
            idx = int(snap.get("index") or 0)
            steps = snap.get("steps") or []
            cur_step = steps[idx] if 0 <= idx < len(steps) else {}
            if (snap.get("running") and cur_step.get("heading") is not None
                    and self._imu_heading is not None):
                now_m = time.monotonic()
                if now_m - getattr(self, "_last_trace_at", 0.0) > 0.4:
                    self._last_trace_at = now_m
                    tgt = self.demo.current_target() or {}
                    err = float(cur_step["heading"]) - float(self._imu_heading)
                    err = (err + 180.0) % 360.0 - 180.0
                    self._log_event("turn trace: target %.0f heading %.1f err %+.1f rot %s rate %.1f" % (
                        float(cur_step["heading"]), float(self._imu_heading), err,
                        tgt.get("rot"), float(self._imu_rate)))
        except Exception:                   # noqa: BLE001
            pass

        # 转角记录变少 = 演示中途被重新启动（start() 会清空 _turn_log）
        n_turns = len(snap.get("turns") or [])
        if n_turns < getattr(self, "_last_turns_seen", 0):
            self._log_event("WARNING: turns reset %d -> %d (demo restarted mid-run?)"
                            % (getattr(self, "_last_turns_seen", 0), n_turns))
        self._last_turns_seen = n_turns

        # 航向保持状态变化（现场判断"直线有没有在做闭环修正"）
        hh = snap.get("heading_hold") or {}
        hstate = hh.get("state")
        if hstate and hstate != getattr(self, "_last_hold_state", None):
            self._last_hold_state = hstate
            if hstate == "no-imu":
                self._log_event("heading hold OFF (no IMU) - running open loop")
            elif hstate == "active":
                self._log_event("heading hold ON (target %.1f deg)" % float(hh.get("target") or 0.0))

        # 逐步轨迹：每一步开始都记一行（含该步的控制量与 IMU 朝向）
        if snap.get("running"):
            idx = int(snap.get("index") or 0)
            if idx != self._last_step_index:
                self._last_step_index = idx
                steps = snap.get("steps") or []
                st = steps[idx] if 0 <= idx < len(steps) else {}
                hd = self._imu_heading if self._imu_heading is not None else float("nan")
                self._log_event("step %d/%d: %s (v=%s rot=%s h=%s, heading=%.1f)" % (
                    idx + 1, len(steps), (st.get("caption") or "")[:34],
                    st.get("velocity"), st.get("rot"), st.get("heading"), hd))
        while self._last_turn_count < len(turns):
            item = turns[self._last_turn_count]
            self._last_turn_count += 1
            self._log_event(
                "turn %d: target %.1f -> %.1f (err %+.1f, %.2fs, %s)" % (
                    self._last_turn_count, item.get("target", 0), item.get("achieved", 0),
                    item.get("error", 0), item.get("seconds", 0), item.get("reason", "?"))
            )
        if snap.get("abort_reason"):
            if snap["abort_reason"] != getattr(self, "_last_abort_logged", None):
                self._log_event("demo abort: %s (max_sag=%.0f mV)" % (snap['abort_reason'], self._sag_max))
                self._last_abort_logged = snap["abort_reason"]
            self._sag_max = 0.0
            self._last_demo_name = None
        else:
            self._last_abort_logged = None
            if not snap.get("running") and self._last_demo_name and snap.get("finished"):
                self._log_event("demo finished: %s (%.2fs) max_sag=%.0f mV" % (
                    self._last_demo_name, snap.get("last_run_s") or 0, self._sag_max))
                self._sag_max = 0.0
                self._last_demo_name = None

    # ------------------------------------------------------------------ 堵转保护
    def _check_stall(self) -> None:
        """运动过程中电池电压持续异常跌落 => 判定堵转，立即停车并中止演示。

        原理：电机堵转时电流飙升，2S 电池电压会被拉低几百 mV（比正常加速时的瞬时跌落更持久）。
        """
        if not bool(self._stall_cfg.get("enabled", True)):
            return
        mv = self._telemetry.get("battery_mv")
        if mv is None:
            return
        now = time.monotonic()
        moving = self._has_motion() or self.demo.is_running()
        if not moving:
            # 静止时更新基线（用一阶低通，避免单帧噪声）
            if self._stall_baseline_mv is None:
                self._stall_baseline_mv = float(mv)
            else:
                self._stall_baseline_mv = self._stall_baseline_mv * 0.9 + float(mv) * 0.1
            self._stall_sag_since = None
            return
        if self._stall_baseline_mv is None:
            return
        drop_mv = self._stall_baseline_mv - float(mv)
        threshold = float(self._stall_cfg.get("drop_mv", 800.0))
        hold_s = float(self._stall_cfg.get("hold_s", 1.0))
        # 记录运动中的最大跌落，方便用真实数据调阈值
        if drop_mv > getattr(self, "_sag_max", 0.0):
            self._sag_max = drop_mv
        if drop_mv >= threshold:
            if self._stall_sag_since is None:
                self._stall_sag_since = now
            elif now - self._stall_sag_since >= hold_s:
                self._stall_trips += 1
                self._stall_last = {
                    "at": round(now - self._started_at, 1),
                    "drop_mv": round(drop_mv, 0),
                    "voltage": round(float(mv) / 1000.0, 2),
                }
                self._stall_sag_since = None
                self._log_event("STALL detected: drop %.0f mV at %.2f V -> stop" % (drop_mv, float(mv) / 1000.0))
                self.demo.stop()
                self.stop()
        else:
            self._stall_sag_since = None

    # ------------------------------------------------------------------ 对外控制
    def note_client(self) -> None:
        self._last_client = time.monotonic()
        if self._heartbeat_stale:
            self._heartbeat_stale = False

    def set_target(
        self,
        angle: float = 0,
        velocity: float = 0,
        rot: float = 0,
        drift: bool = False,
        clamp_speed: bool = True,
    ) -> Dict:
        angle_i, velocity_i, rot_i, drift_b = kinematics.sanitize(angle, velocity, rot, drift)
        if clamp_speed:
            velocity_i = int(kinematics.clamp(velocity_i, 0, self._max_velocity))
            rot_i = int(kinematics.clamp(rot_i, -self._max_rot, self._max_rot))
        with self._lock:
            self._mode = "vector"
            self._estop = False
            self._target = {"angle": angle_i, "velocity": velocity_i, "rot": rot_i, "drift": drift_b}
            self.demo.stop()
        return self.status()

    def set_raw(self, wheels: List[float]) -> Dict:
        values = list(wheels or [])[:4]
        while len(values) < 4:
            values.append(0)
        wheels_i = [int(kinematics.clamp(int(round(float(v))), -100, 100)) for v in values]
        with self._lock:
            self._mode = "raw"
            self._estop = False
            self._raw = wheels_i
            self.demo.stop()
        return self.status()

    def stop(self) -> Dict:
        with self._lock:
            self.demo.stop()
            self._mode = "vector"
            self._target = {"angle": 0, "velocity": 0, "rot": 0, "drift": False}
            self._raw = [0, 0, 0, 0]
            self._estop = False
        self._send_stop_frames()
        return self.status()

    def estop(self) -> Dict:
        with self._lock:
            self.demo.stop()
            self._mode = "vector"
            self._target = {"angle": 0, "velocity": 0, "rot": 0, "drift": False}
            self._raw = [0, 0, 0, 0]
            self._estop = True
        self._send_stop_frames()
        return self.status()

    def start_demo(self, name: str, dry_run: bool = False) -> Dict:
        # 幂等保护：同一演示在 5 秒内重复启动直接忽略。
        # （手机页面重试/连点会第二次 start，把正在跑的转角状态清空 → 朝向基准错乱 → 乱转）
        now_m = time.monotonic()
        try:
            snap_now = self.demo.snapshot()
            if (snap_now.get("running") and snap_now.get("name") == name
                    and (now_m - float(getattr(self, "_last_start_at", 0.0))) < 5.0):
                self._log_event("start ignored: %s already running (%.1fs ago)"
                                % (name, now_m - float(getattr(self, "_last_start_at", 0.0))))
                return {"ok": True, "ignored": True, "running": True,
                        "demo": snap_now, "message": "demo already running"}
        except Exception:                   # noqa: BLE001
            pass
        self._last_start_at = now_m
        self._log_event("start requested: %s (dry_run=%s)" % (name, dry_run))
        # 先确保车是静止的：自动校准零偏需要静止，否则会把运动当成零偏
        seq = self.demo.get_sequence(name)
        needs_heading = bool(seq and any(s.heading is not None for s in seq.steps))
        self.stop()
        if self._imu is not None and self._imu_ok and (needs_heading or bool(self._imu_cfg.get("calib_always", False))):
            try:
                # 先等到车基本静止再校准：上一次演示刚结束时车可能还在转，
                # 这时校准会把"运动"当成零偏，导致整场朝向漂移。
                deadline = time.monotonic() + 1.5
                while time.monotonic() < deadline:
                    if self._imu_rate is None or abs(float(self._imu_rate)) <= 15.0:
                        break
                    time.sleep(0.1)
                time.sleep(float(self._imu_cfg.get("calib_settle_s", 0.25)))
                bias = self._imu.calibrate(float(self._imu_cfg.get("calib_s", 1.5)))
                if getattr(self._imu, "last_calib_ok", False):
                    self._imu_bias = bias
                    self._imu_error = None
                else:
                    # 校准期间车在动：保留上一次的零偏，只提示
                    self._imu_error = "calibration suspect (sensor moved) - kept previous bias"
                self._imu_heading = 0.0
            except Exception as exc:        # noqa: BLE001 - IMU 异常不影响演示
                self._imu_error = f"auto calib failed: {exc}"
        elif self._imu is not None:
            try:
                self._imu.reset()
                self._imu_heading = 0.0
            except Exception:               # noqa: BLE001
                pass
        with self._lock:
            self._estop = False
        if dry_run:
            # 只做校准 + 朝向归零，不启动演示、不动电机（桌上/架起来时安全验证用）
            return self.status()
        self.demo.start(name)
        return self.status()

    def stop_demo(self) -> Dict:
        self.demo.stop()
        with self._lock:
            self._mode = "vector"
            self._target = {"angle": 0, "velocity": 0, "rot": 0, "drift": False}
            self._raw = [0, 0, 0, 0]
        return self.status()

    # ------------------------------------------------------------------ 状态
    def status(self) -> Dict[str, Any]:
        eff = self._effective()
        now = time.monotonic()
        with self._lock:
            link = dict(self._link)
            stats = dict(self._stats)
            target = dict(self._target)
            estop = self._estop
            stale = self._heartbeat_stale
            telemetry = dict(self._telemetry)
            telemetry["unknown"] = list(self._telemetry["unknown"])

        last_frame = telemetry.get("last_frame_at")
        age = None if last_frame is None else round(now - last_frame, 2)
        telemetry["age_s"] = age
        telemetry["link_ok"] = age is not None and age <= self._telemetry_timeout
        telemetry["speed_fresh"] = False
        telemetry["stale"] = True
        telemetry["available"] = False

        disp = eff.get("display") or {"duties": eff["duties"], "pct": eff["pct"]}
        wheels_detail = []
        for idx, duty in enumerate(disp["duties"]):
            pct = int(disp["pct"][idx]) if idx < len(disp["pct"]) else 0
            estimated = protocol.estimate_mm_s(duty, self._max_speed_mm_s)
            motor_id = self._motor_order[idx]
            sign = self._motor_sign[motor_id - 1] * (-1.0 if self._invert_motors else 1.0)
            sent = float(eff["duties"][idx]) * sign
            wheels_detail.append({
                "id": protocol.WHEEL_IDS[idx],
                "label": protocol.WHEEL_LABELS[idx],
                "channel": f"M{motor_id}",
                "motor_id": motor_id,
                "value": round(float(duty), 1),
                "intent": pct,
                "direction": "fwd" if duty >= 0 else "rev",
                "command": round(sent, 1),
                "measured_mm_s": estimated,
                "measured_pct": round(protocol.mm_s_to_pct(estimated, self._max_speed_mm_s), 1),
                "measured_source": "estimate",
                "encoder_mm_s": None,
                "estimated_mm_s": estimated,
                "encoder": 0,
            })

        # 动作描述按"用户意图"，这样点左平移时显示的就是左平移（不会被 invert_angle 拗成右）
        shown = disp
        summary = kinematics.motion_vector(shown["angle"], shown["velocity"], shown["rot"])
        summary["text"] = kinematics.describe(
            shown["angle"], shown["velocity"], shown["rot"], shown["drift"]
        )
        summary["kind"] = eff["kind"]
        summary["mode"] = eff["mode"]

        voltage = telemetry.get("battery_v")

        return {
            "link": link,
            "board": {
                "name": "Hiwonder RasAdapter5 A (RasAdapter5A)",
                "motor_type": 4,
                "motor_label": "Expansion board - 4 DC motors",
                "control_mode": "pwm",
                "open_loop": True,
                "has_encoder": False,
                "invert": {
                    "motors": self._invert_motors,
                    "rot": self._invert_rot,
                    "angle": self._invert_angle,
                },
                "motor_order": list(self._motor_order),
                "motor_invert": list(self._motor_invert),
                "flash": {},
                "flash_mismatch": None,
                "unit": "%",
                "max_pwm": 100,
                "max_speed_mm_s": self._max_speed_mm_s,
                "wheel_diameter_mm": self._wheel_diameter_mm,
                "deadzone_pct": self._deadzone_pct,
                "deadzone_applied": self._apply_deadzone,
                "upload": {"all": False, "realtime": False, "speed": False},
                "firmware": link.get("firmware"),
                "protocol": "0xAA55 packets - 1 Mbps 8N1 (set_motor_duty -100..100)",
            },
            "capabilities": {
                "angle_step_deg": 1,
                "combined_vector_rot": True,
                "drift": True,
                "per_wheel": True,
                "encoder": False,
                "battery_report": True,
                "max_velocity": self._max_velocity,
                "max_rot": self._max_rot,
            },
            "battery": {
                "voltage": voltage,
                "low": (voltage is not None and float(voltage) >= self._battery_none_v
                        and float(voltage) < self._battery_low_v),
                "absent": voltage is not None and float(voltage) < self._battery_none_v,
                "low_v": self._battery_low_v,
                "full_v": self._battery_full_v,
                "millivolt": telemetry.get("battery_mv"),
            },
            "sensors": {
                "battery_mv": telemetry.get("battery_mv"),
                "report_hz": self._report_hz(),
            },
            "cmd": {
                "mode": eff["mode"],
                "kind": eff["kind"],
                "line": eff["line"],
                "last_sent": stats.get("last_line", ""),
                "sent": stats.get("sent", 0),
                "errors": stats.get("errors", 0),
                "hz": stats.get("hz", self._stream_hz),
                "last_error": stats.get("last_error"),
            },
            "target": target,
            "applied": {
                "angle": int(shown["angle"]),
                "velocity": int(shown["velocity"]),
                "rot": int(shown["rot"]),
                "drift": bool(shown["drift"]),
                "kind": eff["kind"],
            },
            "raw": list(self._raw),
            "wheels": wheels_detail,
            "summary": summary,
            "telemetry": telemetry,
            "demo": self.demo.snapshot(),
            "imu": {
                "available": bool(self._imu_ok),
                "heading": None if self._imu_heading is None else round(float(self._imu_heading), 2),
                "bias_dps": round(float(self._imu_bias), 3),
                "rate_dps": round(float(self._imu_rate), 2),
                "bias_track_dps": round(float(getattr(self._imu, "bias_track", 0.0)), 4) if self._imu is not None else 0.0,
                "still_s": round(float(getattr(self._imu, "still_s", 0.0)), 2) if self._imu is not None else 0.0,
                "bus": int(self._imu_cfg.get("bus", 1)),
                "addr": int(self._imu_cfg.get("addr", 0x68)),
                "sign": float(self._imu_cfg.get("sign", 1.0)),
                "turn_tol_deg": float(self._imu_cfg.get("turn_tol_deg", 2.0)),
                "drops": self._imu_drops,
                "calib_ok": bool(getattr(self._imu, "last_calib_ok", False)) if self._imu is not None else False,
                "calib_spread_dps": round(float(getattr(self._imu, "calib_spread", 0.0)), 3) if self._imu is not None else 0.0,
                "error": self._imu_error,
            },
            "safety": {
                "estop": estop,
                "stall_guard": {
                    "enabled": bool(self._stall_cfg.get("enabled", True)),
                    "baseline_mv": None if self._stall_baseline_mv is None else round(self._stall_baseline_mv, 0),
                    "drop_mv": float(self._stall_cfg.get("drop_mv", 350.0)),
                    "trips": self._stall_trips,
                    "last": self._stall_last,
                },
                "heartbeat_stale": stale,
                "heartbeat_timeout_s": self._heartbeat_timeout,
                "client_idle_s": round(now - self._last_client, 2),
                "firmware_watchdog": False,
                "limits": {
                    "max_velocity": self._max_velocity,
                    "max_rot": self._max_rot,
                    "max_speed_mm_s": self._max_speed_mm_s,
                },
            },
            "uptime_s": round(now - self._started_at, 1),
        }
