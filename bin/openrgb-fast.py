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

import itertools
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


def server_port() -> int:
    try:
        return int(os.environ.get("OMARCHY_RGB_OPENRGB_PORT", "6742"))
    except ValueError:
        return 6742


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


GAINS_PATH = os.path.expanduser(
    os.path.join(os.environ.get("XDG_STATE_HOME", "~/.local/state"), "omarchy-rgb", "board-gains")
)


def board_curve() -> list[tuple[float, float]]:
    """Кривая отдачи каналов у цепочки на разъёмах платы.

    Одна и та же пара значений на контроллере Lian Li и на разъёмах платы
    светит по-разному: у платы зелёный зажигается заметно позже остальных
    каналов. На полной яркости разницы нет, а на слабых уровнях зелёного
    не хватает и нейтральный серый выглядит розовым. Поэтому для каждого
    канала задаётся пара "множитель и показатель степени": выход =
    множитель * 255 * (вход / 255) ** показатель. Показатель меньше единицы
    поднимает слабые уровни, не трогая верх.

    Файл: board-gains в каталоге состояния, шесть чисел
    "множительR показательR множительG показательG множительB показательB",
    по умолчанию "1 1 1 1 1 1".
    """
    default = [(1.0, 1.0), (1.0, 1.0), (1.0, 1.0)]
    try:
        with open(GAINS_PATH, encoding="utf-8") as handle:
            parts = [float(part) for part in handle.read().split()]
    except (OSError, ValueError):
        return default
    if len(parts) == 3:
        # Старый формат: только множители.
        return [(max(0.0, min(1.0, part)), 1.0) for part in parts]
    if len(parts) >= 6:
        return [
            (max(0.0, min(2.0, parts[index])), max(0.05, min(4.0, parts[index + 1])))
            for index in (0, 2, 4)
        ]
    return default


def calibrated(color: int) -> int:
    curve = board_curve()
    channels = [(color >> 16) & 0xFF, (color >> 8) & 0xFF, color & 0xFF]
    out = []
    for value, (gain, gamma) in zip(channels, curve):
        shaped = 255.0 * (value / 255.0) ** gamma
        out.append(max(0, min(255, round(shaped * gain))))
    return (out[0] << 16) | (out[1] << 8) | out[2]


def scaled_color(accent: str, percent: int) -> int:
    accent = accent.lstrip("#")
    scale = max(0, min(100, percent)) / 100.0
    channels = [round(int(accent[i : i + 2], 16) * scale) for i in (0, 2, 4)]
    channels = [max(0, min(255, value)) for value in channels]
    return calibrated((channels[0] << 16) | (channels[1] << 8) | channels[2])


def resolve(names: list[str], sock: socket.socket) -> dict:
    """Возвращает имя -> запись кэша, проверяя, что индекс всё ещё тот самый.

    Порядок устройств на сервере не постоянен: он зависит от того, какие
    устройства успели определиться. Писать цвет по устаревшему индексу означает
    покрасить чужое устройство и оставить нужное как было, поэтому индекс
    подтверждается именем, а при расхождении кэш пересобирается.
    """
    cache = read_cache()
    stale = False
    for name in names:
        entry = cache.get(name)
        if not entry or not isinstance(entry.get("index"), int):
            stale = True
            break
        actual = device_name(sock, entry["index"])
        if actual != name:
            stale = True
            break
    if stale:
        discover(names)
        cache = read_cache()
    return cache


PACKET_UPDATE_MODE = 1101


class BlobReader:
    """Читает описание устройства так, как его отдаёт сервер OpenRGB."""

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0

    def u32(self) -> int:
        value = struct.unpack("<I", self.data[self.pos : self.pos + 4])[0]
        self.pos += 4
        return value

    def u16(self) -> int:
        value = struct.unpack("<H", self.data[self.pos : self.pos + 2])[0]
        self.pos += 2
        return value

    def text(self) -> str:
        length = self.u16()
        raw = self.data[self.pos : self.pos + length]
        self.pos += length
        return raw.split(b"\x00")[0].decode("utf-8", errors="replace")

    def mode(self) -> dict:
        # Сервер отдаёт девять 32-битных полей и счётчик цветов. Порядок полей:
        # значение режима, флаги, границы скорости, границы числа цветов,
        # скорость, яркость и способ окраски.
        record = {
            "name": self.text(),
            "value": self.u32(),
            "flags": self.u32(),
            "speed_min": self.u32(),
            "speed_max": self.u32(),
            "colors_min": self.u32(),
            "colors_max": self.u32(),
            "speed": self.u32(),
            "brightness": self.u32(),
            "color_mode": self.u32(),
        }
        record["colors"] = [self.u32() for _ in range(self.u16())]
        return record

    def device(self) -> dict:
        self.u32()  # размер данных
        info = {"type": self.u32(), "name": self.text(), "vendor": self.text(),
                "description": self.text(), "version": self.text(),
                "serial": self.text(), "location": self.text()}
        info["modes"] = [self.mode() for _ in range(self.u16())]
        info["zone_count"] = self.u16()
        info["led_count"] = self.pos  # заполняется ниже, после разбора зон
        return info


def parse_modes(body: bytes) -> list[dict]:
    """Возвращает список режимов устройства из его описания."""
    reader = BlobReader(body)
    reader.u32()
    reader.u32()
    # name, vendor, description, version, serial, location
    for _ in range(5):
        reader.text()
    count = reader.u16()
    reader.u32()  # индекс активного режима
    return [reader.mode() for _ in range(count)]


def pack_mode(record: dict) -> bytes:
    name = record["name"].encode("utf-8") + b"\x00"
    out = struct.pack("<H", len(name)) + name
    out += struct.pack(
        "<IIIIIIIII",
        record["value"], record["flags"], record["speed_min"], record["speed_max"],
        record["colors_min"], record["colors_max"], record["speed"],
        record["brightness"], record["color_mode"],
    )
    colors = record["colors"]
    out += struct.pack("<H", len(colors))
    out += b"".join(struct.pack("<I", color) for color in colors)
    return out


def set_mode_fast(percent: int, accent: str, name: str, port: int = 6742) -> int:
    """Задаёт цвет платы так же, как командная строка, но по протоколу.

    Драйвер ASRock берёт цвет для режима из массива светодиодов зоны, поэтому
    записи две: сначала новый цвет в массив, затем повторное применение режима,
    которое этим цветом и закрашивает ленту. Вместе это десятки миллисекунд
    против секунды, которую командная строка тратит на опрос всей машины.
    """
    if not server_running():
        return 3
    sock = connect(4.0)
    if sock is None:
        return 3
    try:
        entry = resolve([name], sock).get(name)
        if not entry:
            return 3
        index = entry["index"]
        if not entry.get("leds"):
            return 3
        send(sock, index, PACKET_REQUEST_CONTROLLER_DATA)
        packet = read_until(sock, PACKET_REQUEST_CONTROLLER_DATA, 4.0)
        if packet is None:
            return 3
        modes = parse_modes(packet[2])
        wanted = "Off" if percent <= 0 else "Static"
        target = next((i for i, m in enumerate(modes) if m["name"] == wanted), None)
        if target is None:
            return 3
        color = scaled_color(accent, percent)
        # Порядок и содержимое как у 'openrgb -m Static -c ...': сначала режим,
        # причём с color_mode = 0 и одним чёрным цветом — это значит "применить
        # режим, свои цвета не задавать". Только после этого массив светодиодов,
        # из которого драйвер ASRock и берёт цвет. Обратный порядок заставлял
        # устройство рисовать старый цвет, а color_mode = 1 из описания — ждать
        # цвета внутри записи режима.
        record = {
            "name": modes[target]["name"],
            "value": modes[target]["value"],
            "flags": modes[target]["flags"],
            "speed_min": modes[target]["speed_min"],
            "speed_max": modes[target]["speed_max"],
            "colors_min": modes[target]["colors_min"],
            "colors_max": modes[target]["colors_max"],
            "speed": modes[target]["speed"],
            "brightness": 0,
            "color_mode": 0,
            "colors": [0],
        }
        payload = struct.pack("<II", 8 + len(pack_mode(record)), target) + pack_mode(record)
        send(sock, index, PACKET_UPDATE_MODE, payload)
        update_leds(sock, index, entry["leds"], color)
        return 0
    finally:
        sock.close()


def set_mode(percent: int, accent: str, names: list[str], port: int = 6742) -> int:
    """Задаёт цвет режимом, через уже поднятый сервер OpenRGB.

    Единственный путь записи для платы: железо хранит цвет режима отдельно от
    массива светодиодов, и если писать двумя путями, оно показывает то, что
    записано последним. Плюс клиентский режим не пересканирует машину заново,
    поэтому запись занимаетсекунду вместо трёх.
    """
    if not server_running():
        return 3
    color = scaled_color(accent, percent)
    if percent <= 0:
        mode, color = "Off", None
    else:
        mode = "Static"
    for name in names:
        command = ["openrgb", "--client", f"127.0.0.1:{port}", "-d", name, "-m", mode]
        if color is not None:
            command += ["-c", f"{color:06x}"]
        result = subprocess.run(command, capture_output=True, timeout=60)
        if result.returncode != 0:
            return 1
    return 0


def apply(percent: int, accent: str, names: list[str]) -> int:
    # Сервер здесь не поднимаем: если его нет, вызывающий уходит на медленный
    # путь, а подъём делает отдельная команда warmup — она долгая (несколько
    # секунд) и в кадре ползунка ей делать нечего.
    if not server_running():
        return 3
    sock = connect(4.0)
    if sock is None:
        return 3
    try:
        cache = resolve(names, sock)
        if any(name not in cache for name in names):
            return 3

        color = scaled_color(accent, percent)
        for name in names:
            entry = cache.get(name)
            if not entry or not entry.get("leds"):
                continue
            update_leds(sock, entry["index"], entry["leds"], color)
    finally:
        sock.close()
    return 0


def read_device(sock: socket.socket, device: int) -> bytes:
    """Возвращает описание устройства целиком (как его отдаёт сервер)."""
    body = read_until(sock, PACKET_REQUEST_CONTROLLER_DATA, 4.0)
    if body is None:
        return b""
    return body[2]


def verify(percent: int, accent: str, names: list[str], verbose: bool = False) -> int:
    """Проверяет, что записанный цвет действительно лежит в железе.

    Каналы в описании устройства хранятся в собственном порядке драйвера, а не
    в том, что мы отправляли (у ASRock Polychrome USB порядок байт на светодиод
    отличается от нашего). Поэтому ищем нужные три байта в любом порядке: так
    проверка отвечает на вопрос "дошёл ли цвет", а не "совпали ли байты".
    """
    if not server_running():
        return 3
    color = scaled_color(accent, percent)
    want = {bytes([color & 0xFF, (color >> 8) & 0xFF, (color >> 16) & 0xFF])}
    red, green, blue = color & 0xFF, (color >> 8) & 0xFF, (color >> 16) & 0xFF
    for order in itertools.permutations((red, green, blue)):
        want.add(bytes(order))
    sock = connect(4.0)
    if sock is None:
        return 3
    ok = True
    try:
        cache = resolve(names, sock)
        for name in names:
            entry = cache.get(name)
            if not entry or not entry.get("leds"):
                continue
            send(sock, entry["index"], PACKET_REQUEST_CONTROLLER_DATA)
            body = read_device(sock, entry["index"])
            hits = sum(
                1
                for offset in range(len(body) - 2)
                if body[offset:offset + 3] in want
            )
            expected = entry["leds"]
            matched = hits >= expected * 0.9
            ok = ok and matched
            if verbose or not matched:
                print(f"{name}: {hits} совпадений из {expected} светодиодов "
                      f"для #{red:02x}{green:02x}{blue:02x} — {'ок' if matched else 'НЕ СОВПАЛО'}")
    finally:
        sock.close()
    return 0 if ok else 1


def main(argv: list[str]) -> int:
    if len(argv) == 3 and argv[1] == "gains":
        curve = board_curve()
        sc = scaled_color(f"#{argv[2].lstrip('#')}", 100)
        described = " ".join(f"{channel}: x{gain} ^ {gamma}" for channel, (gain, gamma) in zip("RGB", curve))
        print(f"кривая {described} -> #{(sc >> 16) & 0xFF:02x}{(sc >> 8) & 0xFF:02x}{sc & 0xFF:02x}")
        return 0
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
    if action == "verify":
        if len(argv) < 5:
            return 2
        return verify(int(float(argv[2])), argv[3], argv[4:])
    if action == "fastmode":
        # ОТКЛЮЧЕНО: железо рисовало с этими пакетами другой цвет, чем командная
        # строка, хотя байты совпадали. Пока не выяснено, почему, действие
        # отказывается работать, чтобы его случайно не использовали.
        print("fastmode отключён: используйте mode (командная строка)")
        return 1
    if action == "mode":
        if len(argv) < 5:
            return 2
        return set_mode(int(float(argv[2])), argv[3], argv[4:])
    if action == "apply":
        if len(argv) < 5:
            return 2
        percent = int(float(argv[2]))
        return apply(percent, argv[3], argv[4:])
    print(f"unknown action {action!r}")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
