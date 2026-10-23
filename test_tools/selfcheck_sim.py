#!/usr/bin/env python3
"""端到端仿真自检：不开硬件，用「仿真扩展板」跑完整链路（驱动 → HTTP → 页面静态资源）。

覆盖：
    1. 0xAA55 电机帧、逐轮独立占空、死区补偿
    2. 八方向 / 斜行 / 边走边转 / 漂移 / 单轮点检（本板全部支持）
    3. 指令只在状态变化时下发（不刷屏）
    4. 浏览器心跳超时自动停车、急停、停车
    5. 串口自动探测（按"有没有 0xAA55 帧"判定）、后上电握手自动升级
    6. 扩展板上报（SYS 电池帧）与 HTTP API / 前端静态资源

运行：
    python3 test_tools/selfcheck_sim.py
"""

from __future__ import annotations

import json
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from wheelctl import kinematics, protocol  # noqa: E402
from wheelctl.config import WEB_DIR, load_config  # noqa: E402
from wheelctl.driver import WheelDriver  # noqa: E402
from wheelctl.server import create_server  # noqa: E402
from wheelctl.transport import SimRasAdapter  # noqa: E402

PASS = 0
FAILED = []


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS
    if ok:
        PASS += 1
        print(f"  [PASS] {name}" + (f" —— {detail}" if detail else ""))
    else:
        FAILED.append(name)
        print(f"  [FAIL] {name}" + (f" —— {detail}" if detail else ""))


def wait_for(cond, timeout: float = 1.5, step: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(step)
    return cond()


def make_driver(**overrides) -> WheelDriver:
    cfg = load_config()
    cfg["sim"]["force"] = True
    for key, value in overrides.items():
        cfg[key].update(value)
    return WheelDriver(cfg)


def http(url: str, payload=None, method=None):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def duties(st) -> list:
    return [w["value"] for w in st["wheels"]]


def main() -> int:
    print("== 1. 链路与上报 ==")
    driver = make_driver()
    driver.start()
    wait_for(lambda: driver.status()["link"]["handshake"] != "pending")

    st = driver.status()
    check("握手拿到 SYS 电池帧", st["link"]["handshake"] == "battery", st["link"]["handshake"])
    check("波特率 1 Mbps", st["link"]["baud"] == 1000000, str(st["link"]["baud"]))
    check("电池电压已解析", abs(float(st["battery"]["voltage"]) - 7.9) < 0.3, str(st["battery"]["voltage"]))
    check("收到上报帧", st["telemetry"]["frames"] > 0, str(st["telemetry"]["frames"]))
    check("上电先停车", duties(st) == [0.0, 0.0, 0.0, 0.0], str(duties(st)))
    check("能力声明：逐轮/漂移/组合可用",
          all([st["capabilities"]["per_wheel"], st["capabilities"]["drift"],
               st["capabilities"]["combined_vector_rot"]]), str(st["capabilities"]))

    def apply(**kw):
        driver.set_target(**kw)
        wait_for(lambda: driver.status()["applied"]["kind"] != "stop" or kw.get("velocity", 0) == 0)
        return driver.status()

    print("== 2. 八方向 / 斜行（逐轮独立占空） ==")
    st = apply(angle=0, velocity=55)
    d = duties(st)
    check("前进四轮同向同速", len(set(d)) == 1 and d[0] > 0, str(d))
    check("死区补偿后占空高于死区", d[0] >= st["board"]["deadzone_pct"], str(d[0]))
    check("下发帧是 0xAA55 电机帧", st["cmd"]["line"].startswith("aa5503"), st["cmd"]["line"][:24])

    st = apply(angle=90, velocity=55)
    d = duties(st)
    # 注意：本底盘需要 invert_angle 镜像，display 口径 = 硬件口径，
    # 所以左平移的占空模式是 FL/RL 与 FR/RR 反向（物理上就是往左平移）。
    check("左平移：对角两轮反向（硬件口径）", d[0] < 0 and d[1] > 0 and d[2] < 0 and d[3] > 0, str(d))

    st = apply(angle=180, velocity=55)
    check("后退四轮同向", len(set(duties(st))) == 1 and duties(st)[0] < 0, str(duties(st)))

    st = apply(angle=45, velocity=55)
    d = duties(st)
    # 45°（左前）镜像后是 315°，硬件上动的是 FR(M2) 与 RL(M3)
    check("45° 斜行：动的是 FR(M2)/RL(M3)，另两轮为 0",
          d[0] == 0 and d[2] == 0 and d[1] != 0 and d[3] != 0, str(d))

    # 新增：界面显示的通道必须等于实际在动的电机（用户反馈 45° 斜行显示 M1/M4 实际 M2/M3）
    st45 = apply(angle=45, velocity=55)
    moving = [w["channel"] for w in st45["wheels"] if abs(float(w["value"]) or 0) > 0.1]
    check("界面显示的通道 = 实际动作的通道（45° → M2/M3）",
          sorted(moving) == ["M2", "M3"], f"moving={moving}")

    st = apply(angle=20, velocity=55)
    check("任意角度不再吸附（20° 就是 20°）", st["applied"]["angle"] == 20, str(st["applied"]))

    print("== 3. 原地自转 / 边走边转 / 漂移 ==")
    st = apply(angle=0, velocity=0, rot=30)
    d = duties(st)
    check("原地左转：FL/RL 与 FR/RR 反向（硬件口径）",
          d[0] < 0 and d[1] > 0 and d[2] > 0 and d[3] < 0, str(d))
    check("自转时平移量为 0", st["applied"]["velocity"] == 0, str(st["applied"]))

    st = apply(angle=0, velocity=55, rot=30)
    d = duties(st)
    # 组合动作里平移分量会被折半（speed=0.5），所以用"与平移到一半的差值"来判断自转叠加方向
    plain = duties(apply(angle=0, velocity=55))
    rot_only = duties(apply(angle=0, velocity=0, rot=30))
    half = [v / 2.0 for v in plain]
    diff = [d[i] - half[i] for i in range(4)]
    same_sign = all((diff[i] >= 0) == (rot_only[i] >= 0) for i in range(4) if abs(rot_only[i]) > 0.1)
    check("边走边转：叠加量的方向与纯自转一致",
          st["applied"]["rot"] == 30 and same_sign,
          f"half={[round(x,1) for x in half]} combined={d} diff={[round(x,1) for x in diff]} rot_only={rot_only}")

    st_plain = apply(angle=0, velocity=55, rot=0)
    st_drift = apply(angle=0, velocity=55, rot=30, drift=True)
    d_plain, d_drift = duties(st_plain), duties(st_drift)
    check("漂移模式：后两轮一正一负（一条反向加力）",
          d_drift[3] < 0 < d_drift[2] and abs(d_drift[2]) > abs(d_plain[2]),
          f"普通={d_plain} 漂移={d_drift}")

    print("== 4. 单轮点检（本板支持逐轮直控） ==")
    st = driver.set_raw([40, 0, 0, 0])
    time.sleep(0.1)
    st = driver.status()
    d = duties(st)
    check("单轮点检只转 1 号电机", d[0] > 0 and d[1] == 0 and d[2] == 0 and d[3] == 0, str(d))
    check("模式标记为 raw", st["cmd"]["mode"] == "raw", st["cmd"]["mode"])

    print("== 4.1 轮序映射（motor_order：车体序 → 电机编号） ==")

    def motor_duties(frame_bytes):
        import struct

        frames = protocol.PacketParser().feed(frame_bytes)
        data = frames[0].data
        out = [0.0, 0.0, 0.0, 0.0]
        for i in range(data[1]):
            offset = 2 + i * 5
            idx = data[offset]
            (value,) = struct.unpack("<f", data[offset + 1:offset + 5])
            out[idx] = round(value, 1)
        return out

    # 只测映射：不做任何取反
    mapped = make_driver(board={"motor_order": [1, 3, 4, 2], "motor_invert": [False] * 4},
                         control={"invert_motors": False})
    mapped.start()
    wait_for(lambda: mapped.status()["link"]["handshake"] != "pending")
    for car_idx, expect_motor, name in ((0, 0, "FL→M1"), (1, 2, "FR→M3"), (2, 3, "RR→M4"), (3, 1, "RL→M2")):
        wheels = [0, 0, 0, 0]
        wheels[car_idx] = 40
        mapped.set_raw(wheels)
        got = motor_duties(mapped._effective()["frame"])  # noqa: SLF001
        check(f"{name} 只驱动对应电机",
              abs(got[expect_motor]) > 0 and sum(1 for v in got if abs(v) > 1e-9) == 1,
              f"车体序 {wheels} → 电机占空 {got}")
    chans = [w["channel"] for w in mapped.status()["wheels"]]
    check("状态里的通道号是实车编号", chans == ["M1", "M3", "M4", "M2"], str(chans))
    mapped.shutdown()

    print("== 4.2 逐电机方向（motor_invert + 整车反向） ==")
    signed = make_driver(board={"motor_order": [1, 3, 4, 2], "motor_invert": [False, False, True, True]},
                         control={"invert_motors": True, "invert_angle": True, "invert_rot": True})
    signed.start()
    wait_for(lambda: signed.status()["link"]["handshake"] != "pending")
    # 本车实测：前进时 M3/M4 为正、M1/M2 为负（因为右侧 M1/M2 电机极性相反）
    signed.set_target(angle=0, velocity=55)
    st = signed.status()
    got = [(w["channel"], w["command"], w["value"]) for w in st["wheels"]]
    check("前进：M1/M2 下发负、M3/M4 下发正（与实车接线一致）",
          got[0][1] < 0 and got[1][1] > 0 and got[2][1] > 0 and got[3][1] < 0, str(got))
    check("车体语义仍是前进为正（页面不被反相接反影响）",
          all(w["value"] > 0 for w in st["wheels"]), str([w["value"] for w in st["wheels"]]))
    signed.set_target(angle=0, velocity=0, rot=25)
    time.sleep(0.05)
    st = signed.status()
    dirs = [w["command"] for w in st["wheels"]]
    check("原地左转：四轮电值同号（左右镜像的必然结果）", len(set(d > 0 for d in dirs)) == 1, str(dirs))
    signed.shutdown()

    print("== 5. 指令不刷屏（只在变化时下发） ==")
    driver.set_target(angle=0, velocity=55)
    time.sleep(0.25)
    sim = driver._transport  # noqa: SLF001 - 自检需要看仿真板收到了什么
    before = len(sim.history)
    for _ in range(3):
        driver.set_target(angle=0, velocity=55)
        time.sleep(0.05)
    time.sleep(0.2)
    after = len(sim.history)
    check("相同目标不重复下发", after - before <= 1, f"{before} → {after}")

    print("== 6. 停车 / 急停 ==")
    st = driver.stop()
    check("停车 → 四轮 0 占空", duties(st) == [0.0, 0.0, 0.0, 0.0], str(duties(st)))
    st = driver.estop()
    check("急停 → 安全标记", st["safety"]["estop"] and duties(st) == [0.0, 0.0, 0.0, 0.0], str(st["safety"]))

    print("== 7. 演示序列 ==")
    names = [s["name"] for s in driver.demo.list_sequences()]
    check("演示序列 7 套（隐藏 spin/diag）", len(names) == 7, ",".join(names))
    check("含漂移、单轮点检与归位", "drift" in names and "wheel_scan" in names, ",".join(names))
    driver.start_demo("wheel_scan")
    wait_for(lambda: driver.status()["demo"]["running"])
    check("演示可以启动", driver.status()["demo"]["running"], driver.status()["demo"]["caption"])
    driver.stop_demo()
    check("演示可以停止", not driver.status()["demo"]["running"])

    print("== 8. 浏览器心跳超时自动停车 ==")
    passive = make_driver(control={"heartbeat_timeout_s": 0.25})
    passive.start()
    wait_for(lambda: passive.status()["link"]["handshake"] != "pending")
    passive.set_target(angle=0, velocity=55)
    wait_for(lambda: any(duties(passive.status())))
    moving = duties(passive.status())
    time.sleep(0.7)
    stopped = duties(passive.status())
    check("心跳超时后自动停车", any(moving) and not any(stopped), f"{moving} → {stopped}")
    passive.shutdown()

    print("== 9. 串口自动探测（无硬件时不应误选端口） ==")
    from wheelctl.transport import detect_port, probe_port

    check("probe_port 对不存在的设备返回 False",
          probe_port("/dev/definitely-not-a-real-port", 1000000, 0.1) is False)
    check("detect_port 找不到设备时返回 None",
          detect_port(["/dev/definitely-not-a-real-port"], 1000000) is None)

    print("== 10. 小车后上电时握手能自动升级（不用重启服务） ==")

    class LateBoard(SimRasAdapter):
        """前 0.6 秒装死不回，模拟"主控先起来、小车后上电"。"""

        def __init__(self) -> None:
            super().__init__()
            self._silent_until = 0.0

        def open(self) -> None:  # noqa: D103
            super().open()
            self._silent_until = time.monotonic() + 0.6

        def read_bytes(self) -> bytes:  # noqa: D103
            if time.monotonic() < self._silent_until:
                self._out.clear()
                return b""
            return super().read_bytes()

    late_cfg = load_config()
    late_cfg["sim"]["force"] = True
    late_cfg["control"].update({"handshake_timeout_s": 0.2, "refresh_s": 0.2})
    late = WheelDriver(late_cfg, transport=LateBoard())
    late.start()
    first = late.status()["link"]["handshake"]
    ok = wait_for(lambda: late.status()["link"]["handshake"] == "battery", timeout=3.0)
    after = late.status()
    check("初始握手还不是 battery", first != "battery", first)
    check("收到 SYS 帧后自动变 battery", ok, after["link"]["handshake"])
    check("自动补上电池电压", after["battery"]["voltage"] is not None, str(after["battery"]["voltage"]))
    late.shutdown()

    print("== 11. 仿真板与板端行为一致性 ==")
    board = SimRasAdapter()
    board.open()
    board.write(protocol.cmd_motor_duty_all([47.0, -12.5, 0.0, 30.0]))
    check("仿真板解析出四轮占空", board.duty == [47.0, -12.5, 0.0, 30.0], str(board.duty))
    board.write(protocol.cmd_stop())
    check("停止帧四轮归零", board.duty == [0.0, 0.0, 0.0, 0.0], str(board.duty))
    board.write(b"\xaa\x55\x03\x07\x05\x01\x00\x00\x00\x00\x00")  # 故意坏 CRC
    check("坏 CRC 的帧被丢弃", board.duty == [0.0, 0.0, 0.0, 0.0], str(board.duty))
    time.sleep(0.25)
    raw = board.read_bytes()
    frames = protocol.PacketParser().feed(raw)
    check("仿真板主动上报 SYS 电池帧", bool(frames) and frames[0].func == protocol.PacketFunc.SYS, raw.hex()[:24])
    board.close()

    print("== 11.2 演示序列（7 套，每步都要能算出真实电机向量） ==")
    demo_drv = make_driver(board={"motor_order": [1, 3, 4, 2], "motor_invert": [False, False, True, True]},
                           control={"invert_motors": True, "invert_angle": True, "invert_rot": True})
    demo_drv.start()
    wait_for(lambda: demo_drv.status()["link"]["handshake"] != "pending")
    seqs = demo_drv.demo.list_sequences()
    check("演示序列共 7 套", len(seqs) == 7, str([s["name"] for s in seqs]))
    for meta in seqs:
        seq = demo_drv.demo.get_sequence(meta["name"])
        vectors = []
        for step in seq.steps:
            if step.raw is not None:
                car = [float(x) for x in step.raw]
            else:
                car = [float(x) for x in kinematics.wheel_speeds(step.angle, step.velocity, step.rot, step.drift)]
            vectors.append(tuple(round(x) for x in car))
        moving = {v for v in vectors if any(abs(x) > 0.01 for x in v)}
        need = 1 if meta["name"] == "dist_check" else 2   # 量距离演示只有一种动作
        check(f"{meta['name']} 有动作{'且不止一种' if need > 1 else ''}（{meta['step_count']} 步 / {meta['duration']}s）",
              len(moving) >= need, f"不同运动向量 {len(moving)} 个")
    # 单轮点检的解说必须和实车映射一致（车体序 → 真实电机号）
    scan = demo_drv.demo.get_sequence("wheel_scan")
    joined = " ".join(s.caption for s in scan.steps)
    check("单轮点检解说写明真实电机号（M1/M2/M4/M3）",
          all(tag in joined for tag in ("M3", "M1", "M2", "M4")), joined[:60])
    demo_drv.shutdown()

    print("== 11.25 米字往返：每条方向都应回到中心 ==")
    import math

    star_seq = demo_drv.demo.get_sequence("star")
    legs = [s for s in star_seq.steps if s.velocity]
    check("star 是 8 组「出发+返回」", len(legs) == 16, f"{len(legs)} 段直行")
    hx = hy = 0.0
    heading = 0.0
    max_dev = 0.0
    unit = 0.0
    for i, s in enumerate(star_seq.steps):
        if s.rot:
            heading += s.rot * 3.0 * s.duration
        if s.velocity:
            ang = math.radians(heading + s.angle)
            dist = s.velocity * s.duration
            hx += dist * math.sin(ang)
            hy += dist * math.cos(ang)
            unit = max(unit, dist)
            # 每走完「返回」那段，应回到中心
            if (i % 3) == 1:
                max_dev = max(max_dev, math.hypot(hx, hy))
    dev_pct = max_dev / unit * 100 if unit else 999
    check("每次返回后都回到中心（偏差 <2% 单程）", dev_pct < 2.0, f"最大偏差 {dev_pct:.2f}%")
    check("整段结束后仍在中心", math.hypot(hx, hy) / unit * 100 < 2.0,
          f"终点偏移 {math.hypot(hx, hy) / unit * 100:.2f}%")

    print("== 11.28 IMU 闭环转角逻辑（无硬件，白盒） ==")
    from wheelctl.demo import DemoRunner, Step, Sequence

    fake = {"yaw": 0.0}

    def fake_heading():
        return fake["yaw"]

    r2 = DemoRunner(heading_fn=fake_heading, turn_tol=2.0)
    seq2 = r2.get_sequence("home_imu")
    check("home_imu 存在且转弯步带 heading 目标", seq2 is not None and
          any(s.heading is not None for s in seq2.steps), seq2.name if seq2 else "None")
    turns = [s for s in seq2.steps if s.heading is not None]
    check("home_imu 有 4 个闭环转角（0/90/180/270）",
          [int(s.heading) for s in turns] == [90, 180, 270, 360], str([s.heading for s in turns]))
    check("转角步 mode == turn", all(s.mode == "turn" for s in turns), str({s.mode for s in turns}))

    # P 控制：误差大 -> 输出限幅；误差小 -> 0
    r2._heading_error = 40.0
    r2._index = [i for i, s in enumerate(seq2.steps) if s.heading is not None][0]
    r2._seq = seq2
    r2._running = True
    tgt = r2.current_target()
    check("误差 40° 时输出限幅到 rot_limit", tgt["rot"] == turns[0].rot_limit, str(tgt))
    r2._heading_error = 0.3
    tgt = r2.current_target()
    check("误差 0.3° 时输出 rot=0（死区）", tgt["rot"] == 0, str(tgt))
    r2._heading_error = -10.0
    tgt = r2.current_target()
    check("负误差输出反向转动", tgt["rot"] < 0, str(tgt))
    r2._running = False

    # 转到位应"立刻进入下一步"（不用等满时长）
    r3 = DemoRunner(heading_fn=lambda: 90.0, turn_tol=2.0)   # 假装已经转到 90°
    seq3 = r3.get_sequence("home_imu")
    r3._seq = seq3
    r3._running = True
    side, settle1 = seq3.steps[0], seq3.steps[1]
    turn = seq3.steps[2]
    r3._elapsed = side.duration + settle1.duration + 0.01     # 刚进入转角步
    r3._last_tick = time.monotonic()
    r3.tick()
    check("已到位时转角步立即结束（不等时长）", r3._index > 2, f"index={r3._index}")
    r3._running = False

    print("== 11.29 演示开始前自动校准（假 IMU） ==")

    class FakeImu:
        def __init__(self):
            self.calib_calls = 0
            self.reset_calls = 0
            self.last_calib_ok = True
            self.calib_spread = 0.1
            self.bias = 0.0

        def calibrate(self, seconds=1.5):
            self.calib_calls += 1
            self.bias = 0.42
            return self.bias

        def reset(self):
            self.reset_calls += 1

    demo_drv._imu = FakeImu()
    demo_drv._imu_ok = True          # 模拟"IMU 在线"
    demo_drv._imu_cfg["calib_s"] = 0.01          # 测试里别真等 1.5 秒
    demo_drv._imu_cfg["calib_settle_s"] = 0.0
    demo_drv.start_demo("home_imu")
    check("需要 IMU 的演示开始前会校准", demo_drv._imu.calib_calls == 1, str(demo_drv._imu.calib_calls))
    check("校准结果被采纳为 bias", abs(demo_drv._imu_bias - 0.42) < 1e-6, f"{demo_drv._imu_bias}")
    check("演示开始时朝向归零", demo_drv._imu_heading == 0.0, str(demo_drv._imu_heading))
    demo_drv.stop_demo()

    before = demo_drv._imu.calib_calls
    demo_drv.start_demo("tour")                   # 不用 IMU 的演示不应该再校准
    check("普通演示不会触发校准", demo_drv._imu.calib_calls == before, str(demo_drv._imu.calib_calls))
    demo_drv.stop_demo()
    demo_drv._imu = None
    demo_drv._imu_ok = False
    demo_drv._imu_cfg["calib_s"] = 1.5

    print("== 11.31 IMU 失效保护（不得盲转） ==")
    r4 = DemoRunner(heading_fn=lambda: 0.0, rate_fn=lambda: 0.0, turn_tol=2.5,
                    turn_lead=2.0, rate_tol=10.0, turn_kp=1.1, turn_kd=0.3, turn_deadband=3.0)
    seq4 = r4.get_sequence("home_imu")
    r4._seq = seq4
    r4._running = True
    r4._index = [i for i, s in enumerate(seq4.steps) if s.heading is not None][0]
    r4._heading_error = 40.0
    r4._imu_lost = False
    tgt4 = r4.current_target()
    check("IMU 正常时会给出转动指令", tgt4["rot"] != 0, str(tgt4))
    r4._imu_lost = True
    tgt4 = r4.current_target()
    check("IMU 失效时立刻停转（rot=0）", tgt4["rot"] == 0, str(tgt4))
    r4._running = False

    print("== 11.32 堵转保护（电池电压跌落） ==")
    demo_drv._stall_cfg = {"enabled": True, "drop_mv": 350.0, "hold_s": 0.6}
    demo_drv._stall_baseline_mv = 7600.0
    demo_drv._stall_sag_since = None
    demo_drv._stall_trips = 0
    demo_drv._telemetry["battery_mv"] = 7600
    demo_drv._check_stall()                       # 静止：应只更新基线，不触发
    check("静止时不触发堵转保护", demo_drv._stall_trips == 0, str(demo_drv._stall_trips))

    demo_drv.set_target(angle=0, velocity=55, rot=0)   # 进入运动状态
    demo_drv._telemetry["battery_mv"] = 7100            # 电压跌落 500mV
    demo_drv._stall_sag_since = time.monotonic() - 1.0  # 已持续 1s
    demo_drv._check_stall()
    check("运动中电压持续跌落触发堵转停车", demo_drv._stall_trips == 1,
          f"trips={demo_drv._stall_trips} last={demo_drv._stall_last}")
    check("堵转后目标速度归零", demo_drv._target["velocity"] == 0, str(demo_drv._target))
    demo_drv._stall_cfg = {"enabled": False}

    print("== 11.32 转角收敛时不得误判 IMU 失效 ==")

    def _runner_at_turn(heading_value):
        r = DemoRunner(heading_fn=lambda: heading_value, rate_fn=lambda: 0.0,
                       turn_tol=2.5, turn_lead=2.0, rate_tol=10.0,
                       turn_kp=1.1, turn_kd=0.3, turn_deadband=3.0)
        seq = r.get_sequence("home_imu")
        r._seq = seq
        r._running = True
        r._index = 2
        side, settle = seq.steps[0], seq.steps[1]
        r._elapsed = side.duration + settle.duration + 0.01
        r._last_tick = time.monotonic()
        return r

    # A) 已经收敛（误差 1° < 死区 3°）→ 朝向不变是正常的，不应判定失效
    rA = _runner_at_turn(89.0)
    rA._last_heading = 89.0
    rA._last_heading_at = time.monotonic() - 2.0      # 假装已经 2 秒没变化
    rA.tick()
    check("转角收敛后朝向不变不算 IMU 失效", rA._imu_lost is False, f"imu_lost={rA._imu_lost}")
    check("收敛后不会中止演示", not rA.snapshot().get("abort_reason"), str(rA.snapshot().get("abort_reason")))
    rA._running = False

    # B) 还差 90°（确实要求转动）却一直不动 → 应判定失效并中止
    rB = _runner_at_turn(0.0)
    rB._last_heading = 0.0
    rB._last_heading_at = time.monotonic() - 2.0
    rB.tick()
    snapB = rB.snapshot()
    check("要求转动但朝向冻结 → 判定 IMU 失效", rB._imu_lost is True, f"imu_lost={rB._imu_lost}")
    check("失效后中止演示", bool(snapB.get("abort_reason")), str(snapB.get("abort_reason")))
    rB._running = False

    print("== 11.3 归位演示（home_imu，模型层闭环） ==")
    home_seq = demo_drv.demo.get_sequence("home_imu")
    check("home_imu 序列存在且有 4 条边 + 4 次转向",
          home_seq is not None
          and sum(1 for s in home_seq.steps if s.velocity) == 4
          and sum(1 for s in home_seq.steps if s.heading is not None) == 4,
          f"{home_seq.duration}s / {len(home_seq.steps)} 步")
    import math

    # 用配置里的真实转角系数（现场标定过），而不是理论值 3.0
    k_deg = float(load_config().get("demo", {}).get("home", {}).get("deg_per_s_per_rot", 3.0))
    hx = hy = 0.0
    heading = 0.0
    side_units = 0.0
    for s in home_seq.steps:
        if s.heading is not None:
            heading = float(s.heading)                   # 闭环把角度转到目标
        elif s.rot:
            heading += s.rot * k_deg * s.duration        # 开环按时长
        if s.velocity:
            ang = math.radians(heading + s.angle)
            dist = s.velocity * s.duration
            hx += dist * math.sin(ang)
            hy += dist * math.cos(ang)
            side_units += dist
    side_units = side_units / 4 or 1.0
    closing = math.hypot(hx, hy) / side_units * 100
    head_err = abs(((heading % 360) + 180) % 360 - 180)
    check("走完一圈回到起点（闭合误差 <2% 边长）", closing < 2.0, f"{closing:.2f}%")
    check("朝向复原（误差 <5°）", head_err < 5.0, f"{head_err:.1f}°")

    print("== 11.5 访问地址探测（启动日志要打印可用的 URL） ==")
    from wheelctl import netinfo

    addrs = netinfo.local_addresses()
    check("探测到本机 IPv4（非回环/非 169.254）",
          all(not ip.startswith(("127.", "169.254.")) for _, ip in addrs), str(addrs))
    block = netinfo.format_access_block(8080)
    check("访问地址块里带 http:// 与端口", "http://" in block and ":8080/" in block, block.splitlines()[0] if block else "")
    urls = [u for _, u in netinfo.access_urls(8080)]
    check("access_urls 返回可直接打开的 URL", all(u.startswith("http://") and u.endswith("/") for u in urls), str(urls[:2]))

    print("== 11.34 航向保持（尽力而为）==")

    def _hold_runner(heading_box, has_imu=True):
        fn = (lambda: heading_box["v"]) if has_imu else (lambda: None)
        r = DemoRunner(heading_fn=fn, rate_fn=lambda: 0.0, heading_hold=True, hold_gain=0.6, hold_max_rot=3)
        seq = r.get_sequence("home_imu")
        r._seq = seq
        r._running = True
        r._index = 0            # 第 1 步是直行（velocity=55，heading=None）
        r._elapsed = 0.1
        r._last_tick = time.monotonic()
        return r

    # A) 直行中车头左偏 6° → 应叠加"向右"的小修正（rot 为负），且不超过 3%
    box = {"v": 0.0}
    rH = _hold_runner(box)
    rH.tick()                        # 第一步先记录目标朝向 0.0
    box["v"] = 6.0                   # 车头偏了 +6°（左）
    rH._last_tick = time.monotonic() - 0.05
    rH.tick()
    tgtH = rH.current_target() or {}
    check("航向保持：车头左偏 6° → 叠加向右的修正",
          int(tgtH.get("rot") or 0) < 0 and abs(int(tgtH.get("rot") or 0)) <= 3,
          f"rot={tgtH.get('rot')} state={rH._hold_state} target={rH._hold_target}")
    check("航向保持：直行动作保持不变", int(tgtH.get("velocity") or 0) == 55, str(tgtH))
    rH._running = False

    # A2) 斜行（45°）不做航向保持：rot 必须保持步骤原值 0
    box2 = {"v": 0.0}
    rD = DemoRunner(heading_fn=lambda: box2["v"], rate_fn=lambda: 0.0, heading_hold=True, hold_gain=0.6, hold_max_rot=3)
    seqD = rD.get_sequence("diag") or rD.get_sequence("tour")
    diag_steps = [i for i, s in enumerate(seqD.steps) if s.heading is None and s.velocity and int(s.angle) % 90 != 0]
    if diag_steps:
        rD._seq = seqD
        rD._running = True
        rD._index = diag_steps[0]
        rD._elapsed = sum(s.duration for s in seqD.steps[:diag_steps[0]]) + 0.05
        rD._last_tick = time.monotonic()
        rD.tick()
        box2["v"] = 6.0
        rD._last_tick = time.monotonic() - 0.05
        rD.tick()
        tgtD = rD.current_target() or {}
        check("斜行不做航向保持（rot 不被叠加）",
              int(tgtD.get("rot") or 0) == 0 and rD._hold_state == "off",
              f"rot={tgtD.get('rot')} state={rD._hold_state}")
        rD._running = False

    # A3) 左平移（90°）不做航向保持
    box3 = {"v": 0.0}
    rS = DemoRunner(heading_fn=lambda: box3["v"], rate_fn=lambda: 0.0, heading_hold=True, hold_gain=0.6, hold_max_rot=3)
    seqS = rS.get_sequence("tour")
    strafe_idx = [i for i, s in enumerate(seqS.steps) if s.heading is None and s.velocity and int(s.angle) % 180 == 90]
    if strafe_idx:
        rS._seq = seqS
        rS._running = True
        rS._index = strafe_idx[0]
        rS._elapsed = sum(s.duration for s in seqS.steps[:strafe_idx[0]]) + 0.05
        rS._last_tick = time.monotonic()
        rS.tick()
        box3["v"] = 6.0
        rS._last_tick = time.monotonic() - 0.05
        rS.tick()
        tgtS = rS.current_target() or {}
        check("平移不做航向保持（rot 不被叠加）",
              int(tgtS.get("rot") or 0) == 0 and rS._hold_state == "off",
              f"rot={tgtS.get('rot')} state={rS._hold_state}")
        rS._running = False

    print("== 11.36 时间越过转角步末尾时补记结果 ==")

    rC = DemoRunner(heading_fn=lambda: 82.4, rate_fn=lambda: 0.0,
                    turn_tol=2.5, turn_lead=2.0, rate_tol=10.0,
                    turn_kp=1.1, turn_kd=0.3, turn_deadband=3.0,
                    turn_fine_deg=6.0, turn_fine_rot=9, turn_done_deg=1.2)
    seqC = rC.get_sequence("home_imu")
    rC._seq = seqC
    rC._running = True
    rC._index = 2                                     # Turn left to 90°
    turn_end = sum(s.duration for s in seqC.steps[:3])
    rC._elapsed = turn_end + 0.02                     # 已经越过这一步的末尾
    rC._last_tick = time.monotonic()
    rC.tick()
    turnsC = rC.snapshot().get("turns") or []
    check("越过末尾的转角也会被记录（不再静默丢失）",
          len(turnsC) == 1 and turnsC[0].get("target") == 90.0,
          str(turnsC))
    check("补记的原因合理（误差 7.6° → timeout）",
          bool(turnsC) and turnsC[0].get("reason") in ("timeout", "give_up"),
          str(turnsC[0].get("reason") if turnsC else None))
    rC._running = False

    print("== 11.35 转角末尾精调卡住：放弃精度但继续演示 ==")

    def _runner_turn_err(err_deg, rate=0.0):
        r = DemoRunner(heading_fn=lambda: 90.0 - err_deg, rate_fn=lambda: rate,
                       turn_tol=2.5, turn_lead=2.0, rate_tol=10.0,
                       turn_kp=1.1, turn_kd=0.3, turn_deadband=3.0,
                       turn_fine_deg=6.0, turn_fine_rot=9)
        seq = r.get_sequence("home_imu")
        r._seq = seq
        r._running = True
        r._index = 2                      # 第 3 步：Turn left to 90°
        r._elapsed = sum(s.duration for s in seq.steps[:2]) + 0.05
        r._last_tick = time.monotonic()
        return r

    # 误差 4.5°（在精调窗口内），朝向 1 秒没变 → 判定"转不动"，放弃精度，继续演示
    rG = _runner_turn_err(4.5)
    rG._last_heading = 85.5
    rG._last_heading_at = time.monotonic() - 2.0       # 超过 1.5s 的"卡住"阈值
    rG.tick()
    snapG = rG.snapshot()
    turnsG = snapG.get("turns") or []
    check("精调卡住 → 放弃精度（reason=give_up）",
          bool(turnsG) and turnsG[-1].get("reason") == "give_up", str(turnsG[-1] if turnsG else None))
    check("精调卡住 → 不中止演示（继续下一步）",
          rG._running is True and not snapG.get("abort_reason"),
          f"running={rG._running} abort={snapG.get('abort_reason')}")
    check("精调卡住 → 不误报 IMU 失效", rG._imu_lost is False, f"imu_lost={rG._imu_lost}")
    rG._running = False

    # B) 没有 IMU → 不加修正、不中止
    rN = _hold_runner({"v": 0.0}, has_imu=False)
    rN.tick()
    tgtN = rN.current_target() or {}
    check("无 IMU：航向保持自动退化（rot=0）", int(tgtN.get("rot") or 0) == 0, str(tgtN))
    check("无 IMU：直行演示继续（不中止）",
          rN._running is True and rN._abort_reason is None, f"running={rN._running} abort={rN._abort_reason}")
    rN._running = False

    print("== 12. HTTP API 与前端 ==")
    server = create_server(driver, "127.0.0.1", 0, WEB_DIR)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    try:
        code, body_light = http(base + "/")
        check("GET / 返回页面", code == 200 and "RasAdapter5" in body_light.decode("utf-8", "ignore"), str(code))
        code, body_dark = http(base + "/dark.html")
        check("GET /dark.html（深色页）",
              code == 200 and "RasAdapter5" in body_dark.decode("utf-8", "ignore"), str(code))
        check("深色与亮色共用同一份 markup", body_dark == body_light,
              f"len {len(body_dark)} vs {len(body_light)}")
        code, body = http(base + "/dark")
        check("GET /dark（短路径）", code == 200 and body == body_light, str(code))
        code, body = http(base + "/index-v2.html")
        check("GET /index-v2.html（旧路径兼容）", code == 200 and body == body_light, str(code))
        code, body = http(base + "/app.js")
        check("GET /app.js", code == 200, str(code))
        code, body = http(base + "/api/status")
        payload = json.loads(body)
        check("GET /api/status", code == 200 and payload["ok"], str(code))
        caps = payload["status"]["capabilities"]
        check("capabilities 声明逐轮可用",
              caps["per_wheel"] is True and caps["drift"] is True, str(caps))
        code, body = http(base + "/api/target", {"angle": 90, "velocity": 40}, "POST")
        payload = json.loads(body)
        check("POST /api/target 生效", code == 200 and payload["status"]["applied"]["angle"] == 90,
              payload["status"]["cmd"]["line"][:20])
        code, body = http(base + "/api/raw", {"wheels": [10, 0, 0, 0]}, "POST")
        check("POST /api/raw 可用", code == 200 and json.loads(body)["ok"], str(code))
        code, body = http(base + "/api/demo/list")
        check("GET /api/demo/list", code == 200 and len(json.loads(body)["sequences"]) == 7, str(code))
        code, body = http(base + "/api/estop", {}, "POST")
        check("POST /api/estop", code == 200 and json.loads(body)["ok"], str(code))
    finally:
        server.shutdown()
        server.server_close()

    driver.stop()
    driver.shutdown()

    print()
    total = PASS + len(FAILED)
    if FAILED:
        print(f"[FAILED] {len(FAILED)}/{total} 项未通过：{', '.join(FAILED)}")
        return 1
    print(f"[OK] {total}/{total} 项全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
