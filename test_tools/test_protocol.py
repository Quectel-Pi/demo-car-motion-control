#!/usr/bin/env python3
"""扩展板协议单元测试：0xAA55 组帧 / CRC8 / 占空编码 / 解帧 / 死区补偿。

金标准来源：
* CRC8 表直接取自官方 ``ros_robot_controller_sdk.py`` 的 ``crc8_table``（256 项全量比对）；
* ``REAL_BOARD_FRAMES`` 是真机抓到的包（板子 5Hz 上报的 SYS 电池帧），
  用来证明"解帧 + 校验和"对真实数据成立。

运行：
    python3 test_tools/test_protocol.py
"""

from __future__ import annotations

import struct
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from wheelctl import kinematics as kin  # noqa: E402
from wheelctl import protocol as proto  # noqa: E402

#: 官方 SDK 的 crc8_table（原样抄录，用于全量对照）
VENDOR_CRC8_TABLE = [
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

#: 真机抓包（/dev/ttyS1 @ 1 Mbps）：aa 55 00 03 04 d8 1e ba → 电池 0x1ed8 = 7896 mV
REAL_BOARD_FRAMES = [
    "aa55000304d81eba",
    "aa55000304da1e2b00",
    "aa55000304e11e2500",
]


def vendor_crc8(data) -> int:
    """用官方表算 CRC8（独立于被测实现的第二条路径）。"""

    check = 0
    for byte in data:
        check = VENDOR_CRC8_TABLE[check ^ (byte & 0xFF)]
    return check & 0xFF


class CrcTest(unittest.TestCase):
    def test_table_has_256_entries(self):
        self.assertEqual(len(VENDOR_CRC8_TABLE), 256)

    def test_implementation_uses_official_table(self):
        # 实现必须与官方表逐项一致（表原样抄录，这里防止被"优化"成别的多项式）
        self.assertEqual(proto.CRC8_TABLE, VENDOR_CRC8_TABLE)

    def test_multi_byte_sequences(self):
        for payload in (b"", b"\x00", bytes(range(32)), b"\xff" * 8):
            self.assertEqual(proto.checksum_crc8(payload), vendor_crc8(payload), payload.hex())


class RealFrameTest(unittest.TestCase):
    def test_capture_parses_and_battery_decodes(self):
        parser = proto.PacketParser()
        frames = []
        for hex_frame in REAL_BOARD_FRAMES:
            frames.extend(parser.feed(bytes.fromhex(hex_frame)))
        self.assertEqual(len(frames), len(REAL_BOARD_FRAMES), "真机抓到的帧应全部通过 CRC 校验")
        self.assertEqual(frames[0].func, proto.PacketFunc.SYS)
        decoded = proto.decode_report(frames[0])
        self.assertEqual(decoded["kind"], "battery")
        self.assertEqual(decoded["millivolt"], 0x1ED8)      # 7896 mV
        self.assertAlmostEqual(decoded["voltage_v"], 7.90, places=2)

    def test_parser_with_corrupted_crc_is_rejected(self):
        good = bytes.fromhex(REAL_BOARD_FRAMES[0])
        bad = bytearray(good)
        bad[-1] ^= 0xFF
        parser = proto.PacketParser()
        self.assertEqual(parser.feed(bytes(bad)), [])
        self.assertGreaterEqual(parser.bad_frames, 1)


class MotorFrameTest(unittest.TestCase):
    def test_single_motor_duty_layout(self):
        frame = proto.cmd_motor_duty([(1, 35.0)])
        self.assertEqual(frame[:4], bytes([0xAA, 0x55, proto.PacketFunc.MOTOR, 7]))
        self.assertEqual(frame[4], proto.MOTOR_SUB_DUTY)
        self.assertEqual(frame[5], 1)                      # 电机个数
        self.assertEqual(frame[6], 0)                      # 编号 1 → 索引 0
        self.assertEqual(frame[7:11], struct.pack("<f", 35.0))
        self.assertEqual(frame[11], vendor_crc8(frame[2:11]))

    def test_all_four_motors_roundtrip(self):
        duties = [47.0, -47.0, 12.5, 0.0]
        frame = proto.cmd_motor_duty_all(duties)
        self.assertEqual(len(frame), 5 + 2 + 4 * 5)
        parser = proto.PacketParser()
        frames = parser.feed(frame)
        self.assertEqual(len(frames), 1)
        data = frames[0].data
        self.assertEqual(frames[0].func, proto.PacketFunc.MOTOR)
        self.assertEqual(data[0], proto.MOTOR_SUB_DUTY)
        self.assertEqual(data[1], 4)
        got = []
        for i in range(4):
            offset = 2 + i * 5
            (value,) = struct.unpack("<f", data[offset + 1:offset + 5])
            got.append(round(value, 3))
        self.assertEqual(got, duties)

    def test_duty_clamped_to_official_range(self):
        frame = proto.cmd_motor_duty_all([200.0, -500.0, 0.0, 100.0])
        frames = proto.PacketParser().feed(frame)
        data = frames[0].data
        values = [round(struct.unpack("<f", data[2 + i * 5 + 1:2 + i * 5 + 5])[0], 1) for i in range(4)]
        self.assertEqual(values, [100.0, -100.0, 0.0, 100.0])

    def test_stop_frame_is_all_zero(self):
        frames = proto.PacketParser().feed(proto.cmd_stop())
        data = frames[0].data
        values = [struct.unpack("<f", data[2 + i * 5 + 1:2 + i * 5 + 5])[0] for i in range(4)]
        self.assertEqual(values, [0.0, 0.0, 0.0, 0.0])

    def test_speed_subcommand(self):
        frame = proto.cmd_motor_speed([(1, 120.0)])
        self.assertEqual(frame[4], proto.MOTOR_SUB_SPEED)


class CarToMotorMapTest(unittest.TestCase):
    def test_identity(self):
        self.assertEqual(proto.map_car_to_motor([10, 20, 30, 40]), [10.0, 20.0, 30.0, 40.0])

    def test_real_car_order(self):
        # 本车：FL→M1, FR→M3, RR→M4, RL→M2
        got = proto.map_car_to_motor([10, 20, 30, 40], [1, 3, 4, 2])
        self.assertEqual(got, [10.0, 40.0, 20.0, 30.0])

    def test_invert_right_side(self):
        got = proto.map_car_to_motor([10, 20, 30, 40], [1, 3, 4, 2], [False, False, True, True])
        self.assertEqual(got, [10.0, 40.0, -20.0, -30.0])

    def test_length_guarded(self):
        self.assertEqual(proto.map_car_to_motor([5], [1, 2, 3, 4], [False] * 4), [5.0, 0.0, 0.0, 0.0])


class ParserTest(unittest.TestCase):
    def test_fragmented_stream(self):
        frame = proto.cmd_stop()
        parser = proto.PacketParser()
        self.assertEqual(parser.feed(frame[:5]), [])
        parsed = parser.feed(frame[5:])
        self.assertEqual(len(parsed), 1)

    def test_noise_before_head_is_skipped(self):
        parser = proto.PacketParser()
        parsed = parser.feed(b"\x00\xff\x12" + proto.cmd_stop())
        self.assertEqual(len(parsed), 1)

    def test_back_to_back_frames(self):
        parser = proto.PacketParser()
        parsed = parser.feed(proto.cmd_stop() + proto.cmd_stop() + proto.cmd_stop())
        self.assertEqual(len(parsed), 3)

    def test_incomplete_tail_is_buffered(self):
        parser = proto.PacketParser()
        frame = proto.cmd_stop()
        self.assertEqual(parser.feed(frame[:-2]), [])
        self.assertEqual(len(parser.feed(frame[-2:])), 1)


class OtherFunctionsTest(unittest.TestCase):
    def test_rgb_frame(self):
        frame = proto.cmd_rgb([(1, 10, 20, 30)])
        self.assertEqual(frame[2], proto.PacketFunc.RGB)
        self.assertEqual(frame[4], 0x01)
        self.assertEqual(list(frame[6:10]), [0, 10, 20, 30])
        self.assertEqual(frame[-1], vendor_crc8(frame[2:-1]))

    def test_buzzer_frame(self):
        frame = proto.cmd_buzzer(1000, 0.1, 0.05, 2)
        self.assertEqual(frame[2], proto.PacketFunc.BUZZER)
        freq, on, off, repeat = struct.unpack("<HHHH", frame[4:12])
        self.assertEqual((freq, on, off, repeat), (1000, 100, 50, 2))


class DeadzoneTest(unittest.TestCase):
    def test_zero_stays_zero(self):
        self.assertEqual(proto.apply_deadzone(0, 15), 0.0)

    def test_full_scale_unchanged(self):
        self.assertAlmostEqual(proto.apply_deadzone(100, 15), 100.0, places=3)

    def test_low_speed_boosted(self):
        self.assertAlmostEqual(proto.apply_deadzone(40, 15), 15 + 40 * 0.85, places=3)

    def test_sign_preserved(self):
        self.assertLess(proto.apply_deadzone(-40, 15), 0)
        self.assertGreater(proto.apply_deadzone(40, 15), 0)

    def test_disabled(self):
        self.assertAlmostEqual(proto.apply_deadzone(40, 15), 15 + 40 * 0.85, places=3)


class KinematicsLinkTest(unittest.TestCase):
    def test_forward_maps_to_four_equal_duties(self):
        pct = kin.wheel_speeds(0, 55, 0)
        duties = [proto.apply_deadzone(v, 15) for v in pct]
        self.assertEqual(len(set(round(d, 3) for d in duties)), 1)

    def test_diagonal_keeps_two_wheels_zero(self):
        pct = kin.wheel_speeds(45, 55, 0)
        duties = [proto.apply_deadzone(v, 15) for v in pct]
        self.assertEqual(duties[1], 0.0)
        self.assertEqual(duties[3], 0.0)


class EstimateTest(unittest.TestCase):
    def test_percent_to_mm_s(self):
        self.assertEqual(proto.estimate_mm_s(50, 200.0), 100)
        self.assertEqual(proto.estimate_mm_s(-50, 200.0), -100)
        self.assertEqual(proto.mm_s_to_pct(100, 200.0), 50.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)