#!/usr/bin/env python3
"""运动学单元测试：校验麦轮分解公式与显示文本。

运行：
    python3 test_tools/test_kinematics.py
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from wheelctl import kinematics as kin  # noqa: E402


class WheelSpeedsTest(unittest.TestCase):
    def test_forward_all_wheels_same(self):
        wheels = kin.wheel_speeds(0, 100)
        self.assertEqual(len(wheels), 4)
        self.assertEqual(len(set(wheels)), 1, f"前进时四轮应同速同向，实际 {wheels}")
        self.assertGreater(wheels[0], 0)

    def test_forward_magnitude_is_velocity_over_sqrt2(self):
        # 100 / sqrt(2) ≈ 70.7 → 截断为 70
        self.assertEqual(kin.wheel_speeds(0, 100), [70, 70, 70, 70])

    def test_backward_negates_forward(self):
        fwd = kin.wheel_speeds(0, 60)
        back = kin.wheel_speeds(180, 60)
        self.assertEqual(back, [-v for v in fwd])

    def test_strafe_left_is_diagonal_pair(self):
        # 左平移：对角线轮对同向，另一对反向（麦克纳姆轮典型走法）
        wheels = kin.wheel_speeds(90, 60)
        self.assertGreater(wheels[0], 0)
        self.assertLess(wheels[1], 0)
        self.assertGreater(wheels[2], 0)
        self.assertLess(wheels[3], 0)

    def test_strafe_right_mirrors_left(self):
        left = kin.wheel_speeds(90, 60)
        right = kin.wheel_speeds(270, 60)
        self.assertEqual(right, [-v for v in left])

    def test_diagonal_matches_axis_combination(self):
        # 45° 斜行时其中一对轮子应基本停转（麦克纳姆轮合成的典型现象）
        wheels = kin.wheel_speeds(45, 80)
        self.assertTrue(any(abs(v) <= 1 for v in wheels), f"45° 应出现近零速轮，实际 {wheels}")

    def test_rotation_left_pair_opposes_right_pair(self):
        wheels = kin.wheel_speeds(0, 0, rot=50)
        self.assertEqual(wheels[0], -wheels[1])
        self.assertEqual(wheels[0], -wheels[2])
        self.assertEqual(wheels[0], wheels[3])

    def test_rotation_uses_half_speed_factor(self):
        self.assertEqual(kin.wheel_speeds(0, 0, rot=100)[0], 50)  # rot(100) * speed(0.5)

    def test_drift_amplifies_rear_wheels(self):
        normal = kin.wheel_speeds(0, 60, rot=40, drift=False)
        drift = kin.wheel_speeds(0, 60, rot=40, drift=True)
        self.assertNotEqual(normal, drift)
        self.assertGreater(abs(drift[3]), abs(drift[0]))

    def test_stop_is_all_zero(self):
        self.assertEqual(kin.wheel_speeds(0, 0, 0, False), [0, 0, 0, 0])

    def test_values_clamped(self):
        for value in kin.wheel_speeds(0, 100, rot=100, drift=True):
            self.assertGreaterEqual(value, -100)
            self.assertLessEqual(value, 100)

    def test_invalid_inputs_are_sanitized(self):
        angle, velocity, rot, drift = kin.sanitize("bad", 500, -500, 1)
        self.assertEqual((angle, velocity, rot, drift), (0, 100, -100, True))

    def test_velocity_negative_is_clamped_to_zero(self):
        self.assertEqual(kin.wheel_speeds(0, -50), [0, 0, 0, 0])


class VectorAndTextTest(unittest.TestCase):
    def test_motion_vector_forward(self):
        vec = kin.motion_vector(0, 50)
        self.assertEqual(round(vec["vy"]), 50)
        self.assertEqual(round(vec["vx"]), 0)

    def test_motion_vector_left_is_positive_vx(self):
        vec = kin.motion_vector(90, 50)
        self.assertEqual(round(vec["vx"]), 50)
        self.assertEqual(round(vec["vy"]), 0)

    def test_describe_texts(self):
        # 界面已全英文，describe() 的输出按英文契约断言
        self.assertEqual(kin.describe(0, 0, 0), "Stopped")
        self.assertIn("Forward", kin.describe(0, 55))
        self.assertIn("Strafe left", kin.describe(90, 55))
        self.assertIn("Spin left", kin.describe(0, 0, 40))
        self.assertIn("turn right", kin.describe(0, 55, -40))
        self.assertIn("(drift)", kin.describe(0, 55, -40, True))


if __name__ == "__main__":
    unittest.main(verbosity=2)
