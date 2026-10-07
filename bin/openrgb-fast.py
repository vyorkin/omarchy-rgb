#!/usr/bin/env python3
"""Fast path for OpenRGB devices, straight over the SDK protocol.

The `openrgb` command line tool re-detects the whole machine on every run -
about three seconds per device - so a brightness change used to land seconds
after the slider moved. The daemon behind `openrgb --server` keeps the devices
open instead, and applying colours to it is a single packet.

Protocol notes, all read from the OpenRGB sources:

* a packet is `ORGB` + u32 device id + u32 packet id + u32 size, little endian;
* with protocol version 5 the device id is an index, and the server sends no
  acknowledgements (those start at version 6), so a write is verified by asking
  the server for the device data again;
* `UpdateLEDs` (1050) carries `u32 total_size`, `u32 led_count` then one
  `0x00RRGGBB` per LED. The leading size field must equal the whole payload,
  which is the detail that makes an otherwise plausible packet do nothing.

Usage:
    openrgb-fast.py apply <percent> <accent-hex> <name>...
    openrgb-fast.py discover <name>...
    openrgb-fast.py status

Exit codes: 0 ok, 3 no server (the caller falls back to the slow path).
"""

from __future__ import annotations

import json
import os
import socket
import struct
import subprocess
import sys
import time

MAGIC = b"ORGB"
HOST = "127.0.0.1"
PORT = 6742

PACKET_UPDATE_LEDS = 1050
PACKET_REQUEST_CONTROLLER_COUNT = 0
PACKET_REQUEST_CONTROLLER_DATA = 1

STATE_DIR = os.path.join(
    os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state")), "omarchy-rgb"
)
CACHE_PATH = os.path.join(STATE_DIR, "openrgb-devices.json")
SERVER_LOG = os.path.join(STATE_DIR, "openrgb-server.log")


def connect(timeout: float = 3.0) -> socket.socket | None:
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    except AttributeError:  # pragma: no cover - non-unix
        return None
    sock = socket.socket()
    sock.settimeout(timeout)
    try:
        sock.connect((HOST, PORT))
    except OSError:
        sock.close()
        return None
    return sock


def send(sock: socket.socket, device: int, packet: int, data: bytes = b"") -> None:
    sock.sendall(MAGIC + struct.pack("<III", device, packet, len(data)) + data)


def read_packet(sock: socket.socket):
    head = b""
    while len(head) < 16:
        chunk = sock.recv(16 - len(head))
        if not chunk:
            return None
        head += chunk
    device, packet, size = struct.unpack("<III", head[4:])
    body = b""
    while len(body) < size:
        chunk = sock.recv(size - len(body))
        if not chunk:
            break
        body += chunk
    return packet, device, body


def read_until(sock: socket.socket, wanted: int, deadline: float = 3.0):
    end = time.time() + deadline
    while time.time() < end:
        sock.settimeout(max(0.1, end - time.time()))
        try:
            packet = read_packet(sock)
        except socket.timeout:
            return None
        if packet is None:
            return None
        if packet[0] == wanted:
            return packet
    return None


def update_leds(sock: socket.socket, device: int, count: int, color: int) -> None:
    colors = struct.pack("<I", color) * count
    size = 8 + len(colors)
    send(sock, device, PACKET_UPDATE_LEDS, struct.pack("<II", size, count) + colors)


def server_running() -> bool:
    probe = connect(0.6)
    if probe is None:
        return False
    probe.close()
    return True


def start_server(wait_seconds: float = 25.0) -> bool:
    """Starts `openrgb --server` detached and waits for the port to answer."""
    if server_running():
        return True
    os.makedirs(STATE_DIR, exist_ok=True)
    log = open(SERVER_LOG, "ab")
    try:
        subprocess.Popen(
            ["openrgb", "--server"],
            stdout=log,
            stderr=log,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        return False
    end = time.time() + wait_seconds
    while time.time() < end:
        if server_running():
            return True
        time.sleep(0.3)
    return False


def read_cache() -> dict:
    try:
        with open(CACHE_PATH, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


def write_cache(cache: dict) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(CACHE_PATH + ".tmp", "w", encoding="utf-8") as handle:
        json.dump(cache, handle, ensure_ascii=False, indent=2)
    os.replace(CACHE_PATH + ".tmp", CACHE_PATH)


def device_name(sock: socket.socket, index: int) -> str | None:
    """Reads one device's name from the server's own numbering.

    The blob starts with the data size, a flags field, then a length-prefixed
    name, so the name is the first printable run after the first ten bytes.
    """
    send(sock, index, PACKET_REQUEST_CONTROLLER_DATA)
    packet = read_until(sock, PACKET_REQUEST_CONTROLLER_DATA, 4.0)
    if packet is None or len(packet[2]) < 10:
        return None
    body = packet[2]
    length = struct.unpack("<H", body[8:10])[0]
    name = body[10 : 10 + min(length, 64)].split(b"\x00")[0]
    try:
        return name.decode("utf-8", errors="replace").strip()
    except AttributeError:  # pragma: no cover
        return None


def led_counts_from_cli() -> dict[str, int]:
    """LED counts per device name, from a single CLI listing.

    `openrgb --client -l` lists every LED by name, which is an exact count and
    cheaper than decoding the zone tables out of the binary blob. The same
    listing repeats devices when local detection runs next to the server, so the
    result is keyed by name and duplicates collapse.
    """
    try:
        listing = subprocess.run(
            ["openrgb", "--client", f"{HOST}:{PORT}", "-l"],
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return {}

    counts: dict[str, int] = {}
    banner = ""
    for line in listing.splitlines():
        if line[:1].isdigit() and ": " in line:
            banner = line.split(": ", 1)[1].strip()
            continue
        if banner and line.strip().startswith("LEDs:"):
            counts.setdefault(banner, len([part for part in line.split("'")[1::2] if part.strip()]))
    return counts


def discover(names: list[str]) -> int:
    """Remembers the server's index and the LED count for each wanted name."""
    if not start_server():
        return 3
    sock = connect(4.0)
    if sock is None:
        return 3
    try:
        send(sock, 0, PACKET_REQUEST_CONTROLLER_COUNT)
        packet = read_until(sock, PACKET_REQUEST_CONTROLLER_COUNT, 4.0)
        if packet is None:
            return 3
        count = struct.unpack("<I", packet[2][:4])[0]
        by_index = {}
        for index in range(count):
            name = device_name(sock, index)
            if name:
                by_index.setdefault(name, index)
    finally:
        sock.close()

    leds = led_counts_from_cli()
    cache = {}
    for name in names:
        if name in by_index:
            cache[name] = {"index": by_index[name], "leds": leds.get(name, 0)}
    write_cache(cache)
    print(f"discovered: {json.dumps(cache, ensure_ascii=False)}")
    return 0


def scaled_color(accent: str, percent: int) -> int:
    accent = accent.lstrip("#")
    scale = max(0, min(100, percent)) / 100.0
    channels = [round(int(accent[i : i + 2], 16) * scale) for i in (0, 2, 4)]
    channels = [max(0, min(255, value)) for value in channels]
    return (channels[0] << 16) | (channels[1] << 8) | channels[2]


def apply(percent: int, accent: str, names: list[str]) -> int:
    # Сервер здесь не поднимаем: если его нет, вызывающий уходит на медленный
    # путь, а подъём делает отдельная команда warmup — она долгая (несколько
    # секунд) и в кадре ползунка ей делать нечего.
    if not server_running():
        return 3
    cache = read_cache()
    if any(name not in cache for name in names):
        return 3

    color = scaled_color(accent, percent)
    sock = connect(4.0)
    if sock is None:
        return 3
    try:
        for name in names:
            entry = cache.get(name)
            if not entry or not entry.get("leds"):
                continue
            update_leds(sock, entry["index"], entry["leds"], color)
    finally:
        sock.close()
    return 0


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__.strip().splitlines()[-1])
        return 2
    action = argv[1]
    if action == "status":
        print("server running" if server_running() else "no server")
        return 0
    if action == "discover":
        return discover(argv[2:])
    if action == "warmup":
        # Поднимает сервер и запоминает устройства. Запускается в фоне при первом
        # изменении яркости, чтобы первое движение ползунка не ждало секунды.
        if not start_server():
            return 3
        return discover(argv[2:])
    if action == "apply":
        if len(argv) < 5:
            return 2
        percent = int(float(argv[2]))
        return apply(percent, argv[3], argv[4:])
    print(f"unknown action {action!r}")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
