"""本机网络地址探测：把"该用什么网址访问控制页面"算出来。

为什么需要它：M2 每次开机/Wi-Fi 重连拿到的 IP 可能不一样（DHCP），
现场只看 `127.0.0.1` 是没法用手机访问的，所以启动时把**所有非回环 IPv4**都列出来。

实现优先级（尽量不引入依赖）：
1. ``ip -4 -o addr show scope global``（Linux，最准，还能带网卡名）；
2. ``psutil.net_if_addrs()``（装了 psutil 的话）；
3. ``socket`` UDP 取默认出口地址 + ``getaddrinfo(hostname)``（跨平台兜底）。
"""

from __future__ import annotations

import socket
import subprocess
import sys
from typing import List, Tuple


def _is_usable(ip: str) -> bool:
    return bool(ip) and not ip.startswith(("127.", "169.254.", "0."))


def _from_ip_command() -> List[Tuple[str, str]]:
    if sys.platform == "win32":
        return []
    try:
        out = subprocess.check_output(
            ["ip", "-4", "-o", "addr", "show", "scope", "global"],
            stderr=subprocess.DEVNULL,
            timeout=2.0,
        ).decode("utf-8", "ignore")
    except Exception:
        return []
    result: List[Tuple[str, str]] = []
    for line in out.splitlines():
        parts = line.split()
        # 形如: 2: wlan0    inet 192.168.1.50/24 brd ... scope global wlan0
        if len(parts) < 4 or parts[2] != "inet":
            continue
        iface = parts[1].rstrip(":")
        ip = parts[3].split("/")[0]
        if _is_usable(ip):
            result.append((iface, ip))
    return result


def _from_psutil() -> List[Tuple[str, str]]:
    try:
        import psutil  # type: ignore
    except Exception:
        return []
    result: List[Tuple[str, str]] = []
    try:
        for iface, addrs in psutil.net_if_addrs().items():
            for addr in addrs:
                if getattr(addr, "family", None) == socket.AF_INET and _is_usable(addr.address):
                    result.append((iface, addr.address))
    except Exception:
        return []
    return result


def _from_socket() -> List[Tuple[str, str]]:
    result: List[Tuple[str, str]] = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            ip = sock.getsockname()[0]
            if _is_usable(ip):
                result.append(("-", ip))
    except Exception:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if _is_usable(ip) and all(ip != item[1] for item in result):
                result.append(("-", ip))
    except Exception:
        pass
    return result


def local_addresses() -> List[Tuple[str, str]]:
    """返回 ``[(网卡名, IPv4), ...]``；探测不到返回空列表。"""

    for getter in (_from_ip_command, _from_psutil, _from_socket):
        found = getter()
        if found:
            # 去重（同一 IP 可能出现在多个来源里）
            seen = set()
            unique: List[Tuple[str, str]] = []
            for iface, ip in found:
                if ip in seen:
                    continue
                seen.add(ip)
                unique.append((iface, ip))
            return unique
    return []


def access_urls(port: int, limit: int = 4) -> List[Tuple[str, str]]:
    """返回 ``[(地址, URL), ...]``，按"无线优先"排序（手机场景最常用）。"""

    addrs = local_addresses()

    def rank(item: Tuple[str, str]) -> int:
        iface, _ = item
        if iface.startswith(("wlan", "wl", "wlp", "wifi")):
            return 0
        if iface.startswith(("eth", "en")):
            return 1
        return 2

    addrs = sorted(addrs, key=rank)[:limit]
    return [(iface, f"http://{ip}:{port}/") for iface, ip in addrs]


def format_access_block(port: int) -> str:
    """拼成给人看的启动日志块。"""

    items = access_urls(port)
    if not items:
        return ("  访问地址 : （没探测到可用的 IPv4 —— 网络没连上？先看 ip -4 a）\n")
    lines = ["  访问地址 :"]
    for iface, url in items:
        tag = f"({iface})" if iface and iface != "-" else ""
        lines.append(f"              {url}   {tag}".rstrip())
    first = items[0][1].rstrip("/")
    lines.append(f"  深色主题 : {first}/dark")
    lines.append("  提示     : IP 每次开机/Wi-Fi 重连可能变化，以这里打印的为准；")
    lines.append("             手机连同一个 Wi-Fi 后打开上面任一地址即可。")
    return "\n".join(lines) + "\n"


def save_urls(port: int, path) -> str:
    """把"当前该用什么网址访问"写进文件，方便直接 ``cat`` 查（无终端场景）。

    文件里只放纯文本，重启/换 IP 会被覆盖。
    """

    import time as _time
    from pathlib import Path

    block = format_access_block(port)
    text = f"# 生成时间 {_time.strftime('%Y-%m-%d %H:%M:%S')}\n" + block
    try:
        Path(path).write_text(text, encoding="utf-8")
    except Exception:
        pass
    return text
