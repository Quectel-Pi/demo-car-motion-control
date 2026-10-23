# 🚗 Mecanum Wheel Car Motion Control Demo

English | [中文](README_zh.md)

This project is developed on the [Quectel Pi M2 smart main board](https://www.quectel.com.cn/product/quectel-pi-m2).
The M2 stacks directly onto a Hiwonder Raspberry Pi 5 Expansion Board (Model A / RasAdapter5A) over the 40-pin
header and sends per-wheel duty values over a 1 Mbps UART; the board's own STM32 firmware drives the four motors,
so the Mecanum chassis can drive in any direction, strafe diagonally, spin while driving, and drift.
Open `http://<board IP>:8080/` in a phone or desktop browser and you are in control — no client software needed.
An MPU6050 is used for closed-loop turning and heading hold while driving straight.

![Control page and demo car](assets/demo_car_motion_control.png)

▶ **[Watch the demo video](assets/demo-car-motion-control.mp4)** (about 51 seconds)

## 🎯 Features

- **Any heading, not just eight directions**: duty is sent per wheel, so the joystick angle is used as-is with no 45° snapping
- **The full Mecanum repertoire**: diagonal driving, drive-while-spinning, drift (both rear wheels biased in reverse) and single-wheel checks
- **IMU closed-loop turning**: MPU6050 (`/dev/i2c-3`) with PD plus a low-speed fine trim, so square homing does not accumulate turn error; heading hold corrects the straight legs
- **Runs without hardware**: the `--sim` board mirrors the real one (parses motor frames, pushes a battery frame every 200 ms, drops packets with a bad CRC)
- **One markup, two skins**: `/` is the light theme and `/dark` the dark one; phones opening `/` are served the lite page `/m.html`
- **The browser is the control panel**: 3D car model, joystick and status all come from the on-board service; demos run server-side, so a page refresh or a sleeping phone does not interrupt them
- **Safety net**: emergency stop sends duty 0 to all four wheels; 8 seconds without requests zeroes them automatically; SIGINT/SIGTERM stop the car before exiting
- **No flashing, no board-side changes**: the expansion board's own firmware is used as-is and this project stays host-side; the protocol uses the official CRC8 table and has a real-capture regression test
- **A single dependency layer**: only `pyserial` at run time (it falls back to the built-in termios implementation when missing); the Web service is pure standard library

## 📋 Requirements

### Hardware

- Quectel Pi M2 smart main board (Debian 13 factory image, 40-pin expansion header)
- Hiwonder Raspberry Pi 5 Expansion Board, Model A (RasAdapter5A, on-board STM32 firmware, **no flashing required**)
- Mecanum chassis with 4 TT-class motors (wiring FL→M1 / FR→M2 / RR→M4 / RL→M3)
- Battery (7.6–7.9 V measured; the expansion board powers the M2 through the 40-pin header)
- MPU6050 gyro (on-board, `/dev/i2c-3`, optional — turning falls back to timed rotation without it)
- A phone or PC on the same network as the board, with a browser
- HDMI display + USB keyboard and mouse (optional, for local operation and troubleshooting on the board)
- An SSH terminal for remote work (optional): `ssh <user>@<board IP>`, project directory `~/projects/demo-car-motion-control`

The expansion board stacks straight onto the M2's 40-pin header, with **no extra wiring** (serial, ground and
power all go through it):

| M2 40-pin | Pin function | Purpose |
| --- | --- | --- |
| **8** | `GPIO2_B0_d` (UART1_TXD) | M2 → expansion board (`/dev/ttyS1`) |
| **10** | `GPIO2_B1_d` (UART1_RXD) | Expansion board → M2 (battery frames pushed at 5 Hz) |
| **9** | `GND` | Common ground |
| 2 / 4 | `5V` | Battery powers the M2 through the board (7.90 V measured) |

### Software

- OS: Debian 13 (Quectel Pi M2 factory image, no re-flashing needed)
- Python: Python 3 (an isolated environment is created on board with `python3 -m venv`)
- Dependency: `pyserial` (the only third-party package, see `requirements.txt`; a built-in termios implementation is used when it is missing)
- Serial device: `/dev/ttyS1`, **1 Mbps 8N1**, no flow control; reading it needs membership of the `dialout` group
- Peripheral device: `/dev/i2c-3` (MPU6050, optional)
- Network port: `8080/tcp` (Web page and JSON API)

## 🚀 Installation and running

1. Open a terminal on the smart main board and clone the project with git.

```bash
sudo apt update && sudo apt install -y git
git clone https://github.com/weltonjiang/demo-car-motion-control.git
```

When it finishes, a `demo-car-motion-control` folder appears in the current directory.

2. Enter the project directory and run the deployment script (idempotent — a re-run only fills in what is missing).

```bash
cd demo-car-motion-control
./install.sh            # without the exec bit use: bash install.sh
```

The script creates `.venv`, installs the dependencies, checks which system packages are missing and installs the
`~/carurl` shortcut command. The manual equivalent is:

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

3. Do the **read-only self-check** before anything else (the car will not move; raising it off the ground and
   keeping the power switch within reach is recommended).

```bash
python3 test_tools/serial_probe.py --scan   # which tty is answering
python3 test_tools/serial_probe.py          # a battery frame of 7.90 V means wiring / 1 Mbps / CRC are all correct
```

4. Run the program.

```bash
./run.sh --sim              # look at the UI first with no expansion board attached (simulation)
./run.sh                    # auto-detect the serial port (falls back to simulation if none is found)
./run.sh --port 8080        # explicit Web port
```

On start the program enumerates every non-loopback IPv4 address and prints one usable URL per address (the IP may
change on each boot or Wi-Fi reconnect, so trust what is printed):

```text
  URLs       : http://192.168.4.1:8080/    (wlan0)
               http://192.168.1.50:8080/   (eth0)
  Dark theme : http://192.168.4.1:8080/dark
```

Forgot the IP? Run `~/carurl`, or `cat ~/.wheel_urls`, or
`journalctl -u demo-car-motion-control -b | grep -A7 主机名`.

### Optional: start on boot

```bash
sed -i "s|^User=.*|User=$USER|; s|<user>|$USER|g" scripts/demo-car-motion-control.service
sudo cp scripts/demo-car-motion-control.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now demo-car-motion-control
```

Both `User=` and the `<user>` path placeholder in the unit must be replaced with your local user name; the
template uses **absolute paths** instead of `%h` so that it cannot resolve to `/root` during early boot.

Let the main board run its own Wi-Fi hotspot (for sites without a router):

```bash
sudo SSID=QuectelPi PASS=<at least 8 characters> ./scripts/setup_wifi_ap.sh
```

The page address then becomes fixed at `http://192.168.4.1:8080/`.

## 🎛️ Usage

The demo selector and progress bar sit across the top, the left column holds manual control, the single-wheel
check and board feedback, and the right column holds the 3D stage and the event log; phones are served the lite
layout (status pills, large joystick, 3D view).

- Drag the joystick to move, at **any angle**; the further you drag, the faster it goes (capped by `control.max_velocity`, default 80%)
- Keyboard `W/A/S/D` or the arrow keys (diagonals supported), `Q/E` add left/right spin, `Space` / `Esc` stop
- Two sliders set translation speed and spin speed; the **drift switch** maps to `drift=1` (both rear wheels biased in reverse)
- The four single-wheel check sliders write M1–M4 duty directly, used to verify wiring / wheel order / rotation direction
- Nine demo sequences: `tour` mixed tour / `star` star pattern / `spin` spin ladder / `diag` four-way diagonal /
  `dir_scan` eight-direction check / `drift` drift / `wheel_scan` single-wheel check / `home_imu` square homing
  (IMU closed loop) / `dist_check` distance calibration; `spin` and `diag` are hidden by default (`demo.hidden`)
- The board feedback panel is collapsed by default; expanded it shows battery voltage (SYS report), report latency, frames received and link state
- On a phone, `/` serves the lite page automatically; `/m.html` is lite, `/index.html` the full page, `/dark` the full dark page
- Demos run **server-side**, so refreshing the page or the phone screen going off does not interrupt them

### Protocol summary

```text
Frame  : 0xAA 0x55 | func | len | data | crc8 (covering func+len+data)
Motors : func=3, data=[0x05(duty), count] + 5B per motor (<Bf: index = number-1, -100..100)
Battery: func=0, data=[0x04, mv_lo, mv_hi]   ← pushed by the board roughly every 200 ms
```

- Baud rate is **1 Mbps** 8N1, not 9600/115200 (a low baud rate only yields garbage);
- CRC8 must use the official `crc8_table` lookup, **do not reverse-engineer the polynomial** (this project copies
  the table verbatim and has a real-capture regression test);
- The board reports proactively and command frames need no reply → the SYS frame doubles as the heartbeat
  (the link is marked abnormal after 2 seconds without one);
- Duty is hard-clamped to `-100..100` on the host side; open-loop duty has a start-up dead zone, so the host
  applies **per-wheel dead-zone compensation** (`board.deadzone_pct`, default 15%: 0 stays 0, non-zero values map
  linearly from the dead zone up to 100).

### Wheel order and direction calibration

| Position | Motor connector | Body order |
| --- | --- | --- |
| Front left FL | **M1** | 1 |
| Front right FR | **M2** | 2 |
| Rear right RR | **M4** | 3 |
| Rear left RL | **M3** | 4 |

This is the official Hiwonder wiring (M1/M2 = front axle, M3/M4 = rear axle); verify wheel by wheel with the
single-wheel check after wiring it this way. Each of the four layers in `config.json` does exactly one job — do
not mix them up:

```jsonc
"board":   { "motor_order":  [1, 2, 4, 3],              // body order FL/FR/RR/RL → M1/M2/M4/M3
             "motor_invert": [false, true, false, true] }, // by body order: left side (-), right side (+)
"control": { "invert_motors": true,                      // whole-car reversal
             "invert_angle":  true,                      // left/right mirror: angle must be mirrored
             "invert_rot":    true }                     // left/right mirror: spin direction must be negated
```

> `motor_order` must match the wiring; `invert_*` are the means to correct direction — do not reverse the motor wires.

Re-calibration order when changing a car or rewiring:

1. `Forward`: only check whether the car goes toward its nose (all four wheels in the same direction, insensitive to left/right swaps);
2. `Strafe left` + `turn left in place`: if both are reversed ⇒ set `invert_angle` + `invert_rot`; if only one is reversed ⇒ set only that one;
3. Per-wheel nudge (`/api/raw` with only one wheel at +30% each time): wheel top toward the nose is positive — if reversed, flip the corresponding bit in `motor_invert`;
4. Once all pass, **forward / strafe left / turn left in place** should all be correct automatically.

On-site calibration for `home_imu` square homing: drive one straight leg, measure the actual side length and set
`demo.home.scale` to `measured/target` (measured 330 mm but you want 300 mm → `scale ≈ 0.91`); before changing
cars or surfaces, run the `dist_check` demo to re-measure the distance and adjust `board.max_speed_mm_s` to the
measured value.

## 📁 Project structure

```text
demo-car-motion-control/
├── run.sh                 launcher (picks the interpreter, auto-detects the serial port)
├── install.sh             on-board deployment (.venv, dependencies, ~/carurl shortcut)
├── config.json            serial (1 Mbps) / dead-zone compensation / speed limits / heartbeat / IMU / Web port
├── requirements.txt       dependency list (only pyserial)
├── README.md              English documentation
├── README_zh.md           Chinese documentation
├── assets/                demo picture, demo video and UI screenshots
├── docs/
│   └── 使用手册.md         wiring, Wi-Fi, page operations, demos, calibration, safety, troubleshooting (Chinese)
├── scripts/
│   ├── show_access_url.sh print the usable access URLs
│   ├── preflight.sh       read-only checklist before driving for real (power / serial / link / network / service)
│   ├── setup_wifi_ap.sh   turns the M2 into a Wi-Fi hotspot
│   └── demo-car-motion-control.service  systemd autostart unit
├── src/
│   ├── main.py            entry point: argument parsing + start the service
│   └── wheelctl/
│       ├── protocol.py    0xAA55 protocol: official CRC8 table, framing/deframing, dead-zone compensation
│       ├── kinematics.py  Mecanum-wheel decomposition (pure geometry)
│       ├── transport.py   serial (1 Mbps) + simulated expansion board
│       ├── imu.py         MPU6050: bias calibration, closed-loop turning, heading hold
│       ├── driver.py      per-wheel duty command stream, heartbeat, e-stop, status aggregation
│       ├── demo.py        demo sequences (9 sets)
│       ├── netinfo.py     enumerates local IPv4 addresses and works out which URL to use
│       ├── config.py      config loading (including environment-variable overrides)
│       └── server.py      HTTP server and JSON API
├── test_tools/            tests and diagnostics (28 protocol, 16 kinematics, 106 simulation self-check cases)
└── web/                   front end: index.html / mobile.html / app.js / car3d.js / style*.css
```

## 📚 More documentation

Wiring and power details, Wi-Fi setup, page-by-page operations, demo descriptions, the IMU closed-loop turning
parameters, safety design and troubleshooting are in [docs/使用手册.md](docs/使用手册.md) (Chinese); third-party
licences are in [THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md).

All three self-checks run **without hardware**:

```bash
python3 test_tools/test_protocol.py     # 28 protocol unit tests
python3 test_tools/test_kinematics.py   # 16 kinematics unit tests
python3 test_tools/selfcheck_sim.py     # 106 end-to-end simulation self-check cases
```

## 📮 Reporting issues

If you run into problems, feel free to post a technical question in the
[Quectel Pi developer zone](https://forumschinese.quectel.com/c/quectel-pi/58) — our support team will get back
to you.

Issues and pull requests that improve this project are welcome.
