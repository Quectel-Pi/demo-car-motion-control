"""配置加载：config.json + 环境变量覆盖。"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any, Dict

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = PROJECT_ROOT / "config.json"
WEB_DIR = PROJECT_ROOT / "web"

DEFAULTS: Dict[str, Any] = {
    "serial": {
        "port": "auto",
        "baud": 1000000,
        "candidates": [
            "/dev/ttyS1",
            "/dev/ttyS4",
            "/dev/ttyS0",
            "/dev/ttyS2",
            "/dev/ttyAMA0",
            "/dev/serial0",
            "/dev/ttyUSB0",
            "/dev/ttyUSB1",
            "/dev/ttyACM0",
            "/dev/ttyACM1",
            "COM3",
            "COM4",
            "COM5",
            "COM6",
        ],
        "read_timeout_s": 0.02,
    },
    "board": {
        "max_speed_mm_s": 200.0,
        "wheel_diameter_mm": 60.0,
        #: 车体序 [FL, FR, RR, RL] → 电机编号 M1..M4（实车接线不同就改这里）
        "motor_order": [1, 2, 3, 4],
        #: 逐电机方向（按 M1..M4），True = 该电机取反（右侧镜像安装常见）
        "motor_invert": [False, False, False, False],
        "deadzone_pct": 15.0,
        "apply_deadzone": True,
        "battery_low_v": 6.9,
        "battery_full_v": 8.4,
        "battery_none_v": 1.0,
        "telemetry_timeout_s": 2.0,
    },
    "control": {
        "stream_hz": 20,
        "idle_stream_hz": 5,
        "command_hz": 20,
        "refresh_s": 1.0,
        "max_velocity": 80,
        "max_rot": 60,
        "invert_rot": False,
        "invert_motors": False,
        "invert_angle": False,
        "heartbeat_timeout_s": 8.0,
        "handshake_timeout_s": 2.5,
    },
    "demo": {"velocity": 55, "rot": 40},
    "web": {"host": "0.0.0.0", "port": 8080, "fallback_ports": [8090, 8091, 8092]},
    "sim": {"force": False, "battery_v": 7.9},
}


def _deep_merge(base: Dict[str, Any], extra: Dict[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in (extra or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _env_bool(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def load_config(path: str | os.PathLike[str] | None = None) -> Dict[str, Any]:
    """读取配置文件，允许环境变量覆盖关键项（便于板端 systemd 直接调参）。"""

    cfg_path = Path(path) if path else CONFIG_PATH
    raw: Dict[str, Any] = {}
    if cfg_path.exists():
        with cfg_path.open("r", encoding="utf-8") as fh:
            raw = json.load(fh)
    cfg = _deep_merge(DEFAULTS, raw)

    # 环境变量覆盖（命令行参数优先级更高，由 main.py 再覆盖一次）
    if os.environ.get("WHEELCTL_SERIAL"):
        cfg["serial"]["port"] = os.environ["WHEELCTL_SERIAL"]
    if os.environ.get("WHEELCTL_BAUD"):
        cfg["serial"]["baud"] = int(os.environ["WHEELCTL_BAUD"])
    if os.environ.get("WHEELCTL_DEADZONE"):
        cfg["board"]["deadzone_pct"] = float(os.environ["WHEELCTL_DEADZONE"])
    if os.environ.get("WHEELCTL_MAX_SPEED"):
        cfg["board"]["max_speed_mm_s"] = float(os.environ["WHEELCTL_MAX_SPEED"])
    if os.environ.get("WHEELCTL_HTTP_HOST"):
        cfg["web"]["host"] = os.environ["WHEELCTL_HTTP_HOST"]
    if os.environ.get("WHEELCTL_HTTP_PORT"):
        cfg["web"]["port"] = int(os.environ["WHEELCTL_HTTP_PORT"])
    if _env_bool("WHEELCTL_SIM"):
        cfg["sim"]["force"] = True

    return cfg
