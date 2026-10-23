"""MPU6050 陀螺仪最小实现（只依赖标准库，不需要 smbus/pip）。

用途：给「原地转向」提供角度反馈，把 home/star 这类演示从开环变成闭环。

接线（Quectel Pi M2 / 树莓派兼容 40pin）：
    MPU6050 VCC -> 3.3V (pin 1)
    MPU6050 GND -> GND  (pin 6)
    MPU6050 SDA -> GPIO2 / SDA (pin 3)
    MPU6050 SCL -> GPIO3 / SCL (pin 5)
    INT 可以不接（这里用轮询）

命令：
    python3 -m wheelctl.imu --probe            # 探测总线与 WHO_AM_I
    python3 -m wheelctl.imu --watch            # 10Hz 打印当前偏航角（用手转动车验证）
    python3 -m wheelctl.imu --calib            # 静止校准零偏并打印结果
"""

from __future__ import annotations

import argparse
import time

try:                                    # Windows 上没有 fcntl（I2C 只在 Linux 板端需要）
    import fcntl
except ImportError:                     # pragma: no cover - 平台差异
    fcntl = None
from typing import Optional, Tuple

I2C_SLAVE = 0x0703

REG_SMPLRT_DIV = 0x19
REG_CONFIG = 0x1A
REG_GYRO_CONFIG = 0x1B
REG_ACCEL_CONFIG = 0x1C
REG_ACCEL_XOUT_H = 0x3B
REG_PWR_MGMT_1 = 0x6B
REG_WHO_AM_I = 0x75

GYRO_SCALE = 131.0          # ±250 °/s 档：131 LSB/(°/s)


class ImuError(RuntimeError):
    """IMU 打不开或读数异常。"""


class MPU6050:
    """极简 I2C 封装：open() / read_gyro() / read_accel() / who_am_i()。"""

    def __init__(self, bus: int = 1, addr: int = 0x68) -> None:
        self.bus = int(bus)
        self.addr = int(addr)
        self.path = f"/dev/i2c-{self.bus}"
        self._fh = None

    # ---------------------------------------------------------------- 底层
    def open(self) -> "MPU6050":
        if fcntl is None:
            raise ImuError("I2C is only available on the board (Linux); this platform has no fcntl")
        try:
            self._fh = open(self.path, "r+b", buffering=0)
            fcntl.ioctl(self._fh.fileno(), I2C_SLAVE, self.addr)
        except OSError as exc:
            raise ImuError(f"cannot open {self.path} (addr=0x{self.addr:02x}): {exc}") from exc
        return self

    def close(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            finally:
                self._fh = None

    def _write(self, reg: int, value: int) -> None:
        if self._fh is None:
            raise ImuError("IMU is not open")
        self._fh.write(bytes([reg & 0xFF, value & 0xFF]))

    def _read(self, reg: int, length: int, retries: int = 3) -> bytes:
        """读寄存器；I2C 偶发 NAK/干扰时重试几次（电机 EMI 常见）。"""
        if self._fh is None:
            raise ImuError("IMU is not open")
        last = None
        for attempt in range(max(1, retries)):
            try:
                self._fh.write(bytes([reg & 0xFF]))
                data = self._fh.read(length)
                if len(data) == length:
                    return data
                last = ImuError(f"register 0x{reg:02x}: got {len(data)}/{length} bytes")
            except OSError as exc:
                last = exc
            time.sleep(0.002 * (attempt + 1))
        raise ImuError(f"register 0x{reg:02x} read failed: {last}")

    @staticmethod
    def _s16(big: int, little: int) -> int:
        value = (big << 8) | little
        return value - 65536 if value >= 32768 else value

    # ---------------------------------------------------------------- 高层
    def who_am_i(self) -> int:
        return self._read(REG_WHO_AM_I, 1)[0]

    def begin(self) -> "MPU6050":
        """唤醒 + 配置量程/DLPF。GYRO ±250°/s，DLPF 44Hz。"""
        self._write(REG_PWR_MGMT_1, 0x01)      # 唤醒，时钟源=陀螺 X
        time.sleep(0.05)
        self._write(REG_SMPLRT_DIV, 0x04)      # 1kHz/(1+4) = 200Hz 采样
        self._write(REG_CONFIG, 0x03)          # DLPF 44Hz
        self._write(REG_GYRO_CONFIG, 0x00)     # ±250°/s
        self._write(REG_ACCEL_CONFIG, 0x00)    # ±2g
        time.sleep(0.05)
        return self

    def read_gyro(self) -> Tuple[float, float, float]:
        """返回 °/s (x, y, z)。绕 z 轴正方向 = 逆时针（从上方看）。"""
        d = self._read(REG_ACCEL_XOUT_H + 8, 6)      # 0x43..0x48
        gx = self._s16(d[0], d[1]) / GYRO_SCALE
        gy = self._s16(d[2], d[3]) / GYRO_SCALE
        gz = self._s16(d[4], d[5]) / GYRO_SCALE
        return gx, gy, gz

    def read_accel(self) -> Tuple[float, float, float]:
        """返回 g (x, y, z)。"""
        d = self._read(REG_ACCEL_XOUT_H, 6)
        ax = self._s16(d[0], d[1]) / 16384.0
        ay = self._s16(d[2], d[3]) / 16384.0
        az = self._s16(d[4], d[5]) / 16384.0
        return ax, ay, az


class Heading:
    """静止校准零偏 + 积分出偏航角（度）。线程安全，可在驱动线程里调用 update()。"""

    def __init__(self, imu: MPU6050, gyro_sign: float = 1.0) -> None:
        self.imu = imu
        self.gyro_sign = float(gyro_sign)
        self.zaru_rate_dps = 0.8        # 判定静止的角速度阈值（°/s）
        self.zaru_hold_s = 1.5          # 连续静止多久才开始修正
        self.zaru_gain = 0.02           # 每次修正的比例（越小越稳、越慢）
        self.bias_z = 0.0
        self.yaw = 0.0
        self._last = None
        self.rate_hz = 0.0
        self.rate = 0.0             # 当前角速度（°/, 平滑后），判断"是否停稳"用
        self._samples = 0
        self.still_s = 0.0              # 连续静止时长
        self.bias_track = 0.0           # 静止时对零偏的缓慢修正量（累计）
        self.last_calib_ok = False      # 校准期间是否真的静止
        self.calib_spread = 0.0         # 校准期间极差（°/s）
        self.calib_moving = False

    def calibrate(self, seconds: float = 2.0, max_rate: float = 6.0, retries: int = 3) -> float:
        """静止测零偏（车必须完全不动）。返回零偏 °/s。

        校准期间若检测到明显转动（|gz| > max_rate）或抖动过大，会重试；
        重试仍失败则取中位数并标记 unreliable（见 last_calib_ok）。
        """
        self.last_calib_ok = False
        best = None
        for _ in range(max(1, retries)):
            samples = []
            t_end = time.monotonic() + max(0.3, seconds)
            moving = False
            while time.monotonic() < t_end:
                _, _, gz = self.imu.read_gyro()
                samples.append(gz)
                if abs(gz) > max_rate:
                    moving = True
                time.sleep(0.005)
            if not samples:
                continue
            samples.sort()
            med = samples[len(samples) // 2]
            mean = sum(samples) / len(samples)
            spread = samples[-1] - samples[0]
            best = (med, mean, spread, moving)
            if not moving and spread < 2.0:
                self.last_calib_ok = True
                break
        if best is None:
            raise ImuError("calibration failed: no gyro data")
        med, _mean, spread, moving = best
        self.bias_z = med
        self.calib_spread = spread
        self.calib_moving = moving
        self.yaw = 0.0
        self._last = time.monotonic()
        return self.bias_z

    def reset(self) -> None:
        self.yaw = 0.0
        self._last = time.monotonic()

    def update(self) -> float:
        """读一次并积分，返回当前偏航角（度）。"""
        now = time.monotonic()
        _, _, gz = self.imu.read_gyro()
        if self._last is not None:
            dt = now - self._last
            if 0.0 < dt < 0.5:
                rate = (gz - self.bias_z) * self.gyro_sign
                if rate > 250.0:
                    rate = 250.0
                elif rate < -250.0:
                    rate = -250.0
                self.rate = self.rate * 0.7 + rate * 0.3      # 一阶低通，去掉抖动

                # ZARU：静止时用当前读数缓慢修正零偏（不需要重新校准，漂移会被吃掉）
                if abs(rate) < float(self.zaru_rate_dps):
                    self.still_s += dt
                else:
                    self.still_s = 0.0
                if self.still_s >= float(self.zaru_hold_s):
                    # 只在"确认静止"时修正，修正速度很慢（每秒最多 0.5%/s 的残差）
                    corr = rate * float(self.zaru_gain)
                    self.bias_z += corr
                    self.bias_track += corr
                    rate = (gz - self.bias_z) * self.gyro_sign
                self.yaw += rate * dt
                self.rate_hz = 1.0 / dt if dt > 0 else 0.0
                self._samples += 1
        self._last = now
        return self.yaw


def _all_buses():
    """列出系统上所有 /dev/i2c-N 的总线号（这台板子上扩展板 IIC 口是 i2c-3）。"""
    import glob
    out = []
    for path in glob.glob("/dev/i2c-*"):
        try:
            out.append(int(path.rsplit("-", 1)[1]))
        except (IndexError, ValueError):
            continue
    return sorted(out)


def find_imu(buses=None, addrs=(0x68, 0x69)) -> Optional[str]:
    """扫描总线/地址，返回可用的 "bus:addr" 字符串；buses=None 表示扫描全部总线。"""
    for bus in (buses if buses is not None else (_all_buses() or (1, 0, 2))):
        for addr in addrs:
            dev = MPU6050(bus=bus, addr=addr)
            try:
                dev.open()
                who = dev.who_am_i()
                dev.close()
                if who in (0x68, 0x69, 0x70, 0x71, 0x73, 0x98):
                    return f"{bus}:{addr:#04x}"
            except (ImuError, OSError):
                dev.close()
                continue
    return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="MPU6050 自检 / 实时偏航角")
    ap.add_argument("--probe", action="store_true", help="扫描总线并读 WHO_AM_I")
    ap.add_argument("--watch", action="store_true", help="10Hz 打印偏航角（Ctrl+C 结束）")
    ap.add_argument("--calib", action="store_true", help="静止校准零偏")
    ap.add_argument("--bus", type=int, default=1)
    ap.add_argument("--addr", type=lambda s: int(s, 0), default=0x68)
    ap.add_argument("--sign", type=float, default=1.0, help="陀螺 z 轴方向（左转为正就给 1，反之 -1）")
    args = ap.parse_args(argv)

    if args.probe:
        print("可用总线:", [f"/dev/i2c-{b}" for b in (_all_buses() or [1])])
        found = find_imu()
        if found:
            bus, addr = found.split(":")
            dev = MPU6050(bus=int(bus), addr=int(addr, 16)).open()
            print(f"  找到 IMU: bus={bus} addr={addr} WHO_AM_I=0x{dev.who_am_i():02x}  (0x68 就是 MPU6050)")
            dev.close()
            return 0
        print("  ✗ 没找到 0x68/0x69 —— 检查接线/供电，或用 i2cdetect -y 1 看总线")
        return 2

    dev = MPU6050(bus=args.bus, addr=args.addr).open()
    dev.begin()
    who = dev.who_am_i()
    print(f"WHO_AM_I = 0x{who:02x}（MPU6050 应为 0x68）")
    head = Heading(dev, gyro_sign=args.sign)

    if args.calib:
        print("静止校准零偏（1.5s，别碰车）...")
        bias = head.calibrate(1.5)
        print(f"  gyro_z 零偏 = {bias:+.3f} °/s")
        dev.close()
        return 0

    if args.watch:
        print("静止 1.5s 校准零偏...")
        head.calibrate(1.5)
        print("开始输出（用手把车转 90°，看数字是否接近 ±90）")
        try:
            while True:
                yaw = head.update()
                ax, ay, az = dev.read_accel()
                print(f"  yaw={yaw:+7.1f}°  gz={head.rate_hz:5.1f}Hz  acc=({ax:+.2f},{ay:+.2f},{az:+.2f})g", end="\r")
                time.sleep(0.02)
        except KeyboardInterrupt:
            print()
        finally:
            dev.close()
        return 0

    # 默认：读一次原始值
    t0 = time.monotonic()
    for _ in range(10):
        head.update()
        time.sleep(0.05)
    print(f"  偏航角 ≈ {head.yaw:+.2f}°（{time.monotonic() - t0:.1f}s）")
    print(f"  gyro = {tuple(round(v, 2) for v in dev.read_gyro())} °/s")
    dev.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
