#!/usr/bin/env python3
"""Темы для экрана водянки (480x480) с проверяемым контрастом.

Зачем отдельный файл: раньше шаблон собирался прямо в хуке темы, цвета брались
из палитры как попало, и на экране получались надписи одного цвета, а иногда и
тёмно-красная цифра на почти чёрном фоне (контраст 1.7 из 21 — читается хуже
некуда). Здесь цвет каждой надписи проверяется по контрасту к фону, и если
палитра дала нечитаемую пару, цвет осветляется до приемлемой.

Использование:
    lcd_themes.py --list
        печатает доступные темы: id и название
    lcd_themes.py --palette <colors.toml> [--current <lcd_templates.json>]
                  --write <lcd_templates.json> [--theme <id>]
        собирает все темы по палитре и записывает файл шаблонов демона.
        Источники датчиков берутся из уже существующего файла (там верные для
        этой машины k10temp, nvidia_gpu и cpu_usage), поэтому раскладку можно
        менять, не теряя привязку к железу.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

SIZE = 480
SENSOR_FALLBACK = {
    "cpu_temp": {"type": "hwmon", "name": "k10temp", "label": "Tctl"},
    "cpu_load": {"type": "cpu_usage"},
    "gpu_temp": {"type": "nvidia_gpu", "gpu_index": 0, "metric": "temp"},
    "gpu_load": {"type": "nvidia_gpu", "gpu_index": 0, "metric": "usage"},
}
SENSOR_IDS = {
    "val-cpu-temp": "cpu_temp",
    "val-cpu-load": "cpu_load",
    "val-gpu-temp": "gpu_temp",
    "val-gpu-load": "gpu_load",
}
LABELS = {"cpu_temp": "CPU TEMP", "cpu_load": "CPU LOAD", "gpu_temp": "GPU TEMP", "gpu_load": "GPU LOAD"}


# --------------------------------------------------------------- цвет и контраст ---

def luminance(color) -> float:
    def channel(value: float) -> float:
        value /= 255.0
        return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4

    red, green, blue = (channel(float(part)) for part in color[:3])
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def contrast(first, second) -> float:
    one, two = luminance(first), luminance(second)
    return (max(one, two) + 0.05) / (min(one, two) + 0.05)


def readable(color, background, minimum: float = 7.0):
    """Осветляет цвет, пока он не станет различимым на фоне.

    Контраст считается как в вебе (WCAG): 7 и выше — уверенно читается, 4.5 —
    минимум для мелкого текста. Палитры тем бывают тёмными, и без этой правки
    часть надписей сливается с фоном.
    """
    result = [int(part) for part in color[:3]]
    if contrast(result, background) >= minimum:
        return result + [255]
    for step in range(1, 21):
        blend = step / 20.0
        candidate = [round(result[index] + (255 - result[index]) * blend) for index in range(3)]
        if contrast(candidate, background) >= minimum:
            return candidate + [255]
    return [255, 255, 255, 255]


def with_alpha(color, alpha: int = 255):
    return [int(color[0]), int(color[1]), int(color[2]), alpha]


def range_upto(limit: float, color):
    """Диапазон цвета для полос и приборов.

    В схеме демона диапазон — это не интервал "от и до", а порог: до какого
    значения действует этот цвет. Один цвет на всю шкалу описывается одним
    порогом, равным максимуму.
    """
    return [{"max": float(limit), "color": [int(color[0]), int(color[1]), int(color[2])], "alpha": 255}]


# ------------------------------------------------------------------- источники ---

def sources_from_current(path: str | None) -> dict:
    sources = dict(SENSOR_FALLBACK)
    if not path or not os.path.exists(path):
        return sources
    try:
        with open(path, encoding="utf-8") as handle:
            templates = json.load(handle).get("templates") or []
    except (OSError, ValueError):
        return sources
    for template in templates:
        for widget in template.get("widgets") or []:
            key = SENSOR_IDS.get(widget.get("id", ""))
            kind = widget.get("kind") or {}
            if key and kind.get("source"):
                sources[key] = kind["source"]
    return sources


# -------------------------------------------------------------------- виджеты ---

def label(widget_id: str, text: str, x: float, y: float, size: float, color, width: float = 200):
    return {
        "id": widget_id,
        "kind": {"type": "label", "text": text, "font": {}, "font_size": size,
                 "color": with_alpha(color), "align": "center"},
        "x": x, "y": y, "width": width, "height": size * 1.4,
    }


def value(widget_id: str, source, x: float, y: float, size: float, color,
          unit: str = "", width: float = 220, value_max: float = 100):
    return {
        "id": widget_id,
        "kind": {"type": "value_text", "source": source, "format": "{:.0}", "unit": unit,
                 "font": {}, "font_size": size, "color": with_alpha(color), "align": "center",
                 "value_min": 0, "value_max": value_max},
        "x": x, "y": y, "width": width, "height": size * 1.3,
    }


def bar(widget_id: str, source, x: float, y: float, width: float, color, background):
    return {
        "id": widget_id,
        "kind": {"type": "horizontal_bar", "source": source, "value_min": 0, "value_max": 100,
                 "background_color": with_alpha(background),
                 "ranges": range_upto(100, color)},
        "x": x, "y": y, "width": width, "height": 14,
    }


def gauge(widget_id: str, source, x: float, y: float, size: float, color, background,
          value_max: float = 100):
    return {
        "id": widget_id,
        "kind": {"type": "radial_gauge", "source": source, "value_min": 0, "value_max": value_max,
                 "start_angle": 135.0, "sweep_angle": 270.0, "inner_radius_pct": 0.62,
                 "background_color": with_alpha(background),
                 "ranges": range_upto(value_max, color)},
        "x": x, "y": y, "width": size, "height": size,
    }


# --------------------------------------------------------------------- раскладки ---

def theme_grid(sources: dict, background, labels, values) -> tuple[str, str, list]:
    widgets = []
    positions = {
        "cpu_temp": (124, 56, 150, 132),
        "cpu_load": (356, 56, 150, 132),
        "gpu_temp": (124, 268, 150, 344),
        "gpu_load": (356, 268, 150, 344),
    }
    for key, (x, label_y, value_y, _) in positions.items():
        widgets.append(label(f"lbl-{key.replace('_', '-')}", LABELS[key], x, label_y, 30, labels))
        unit = "°C" if key.endswith("temp") else "%"
        maximum = 110 if key.endswith("temp") else 100
        widgets.append(value(f"val-{key.replace('_', '-')}", sources[key], x, value_y, 104,
                             values[key], unit=unit, width=240, value_max=maximum))
    return "grid", "Сетка", widgets


def theme_large(sources: dict, background, labels, values) -> tuple[str, str, list]:
    widgets = [
        label("lbl-cpu-temp", "CPU", 128, 74, 34, labels),
        value("val-cpu-temp", sources["cpu_temp"], 128, 190, 150, values["cpu_temp"],
              unit="°", width=260, value_max=110),
        label("lbl-gpu-temp", "GPU", 352, 74, 34, labels),
        value("val-gpu-temp", sources["gpu_temp"], 352, 190, 150, values["gpu_temp"],
              unit="°", width=260, value_max=110),
        label("lbl-cpu-load", "CPU", 128, 330, 28, labels),
        value("val-cpu-load", sources["cpu_load"], 128, 386, 64, values["cpu_load"],
              unit="%", width=200),
        label("lbl-gpu-load", "GPU", 352, 330, 28, labels),
        value("val-gpu-load", sources["gpu_load"], 352, 386, 64, values["gpu_load"],
              unit="%", width=200),
    ]
    return "large", "Крупные цифры", widgets


def theme_bars(sources: dict, background, labels, values) -> tuple[str, str, list]:
    widgets = []
    rows = [("cpu_temp", 70), ("cpu_load", 176), ("gpu_temp", 282), ("gpu_load", 388)]
    for key, y in rows:
        unit = "°C" if key.endswith("temp") else "%"
        maximum = 110 if key.endswith("temp") else 100
        widgets.append(label(f"lbl-{key.replace('_', '-')}", LABELS[key], 130, y, 30, labels, width=220))
        widgets.append(value(f"val-{key.replace('_', '-')}", sources[key], 392, y, 52,
                             values[key], unit=unit, width=150, value_max=maximum))
        widgets.append(bar(f"bar-{key.replace('_', '-')}", sources[key], 240, y + 40, 400,
                           values[key], background))
    return "bars", "Полосы", widgets


def theme_gauges(sources: dict, background, labels, values) -> tuple[str, str, list]:
    widgets = [
        gauge("gauge-cpu-temp", sources["cpu_temp"], 140, 150, 220, values["cpu_temp"],
              background, value_max=110),
        gauge("gauge-gpu-temp", sources["gpu_temp"], 340, 150, 220, values["gpu_temp"],
              background, value_max=110),
        label("lbl-cpu-temp", "CPU", 140, 60, 30, labels, width=160),
        label("lbl-gpu-temp", "GPU", 340, 60, 30, labels, width=160),
        value("val-cpu-temp", sources["cpu_temp"], 140, 150, 60, values["cpu_temp"],
              unit="°", width=180, value_max=110),
        value("val-gpu-temp", sources["gpu_temp"], 340, 150, 60, values["gpu_temp"],
              unit="°", width=180, value_max=110),
        label("lbl-cpu-load", "CPU LOAD", 140, 320, 26, labels, width=200),
        value("val-cpu-load", sources["cpu_load"], 140, 372, 56, values["cpu_load"],
              unit="%", width=180),
        label("lbl-gpu-load", "GPU LOAD", 340, 320, 26, labels, width=200),
        value("val-gpu-load", sources["gpu_load"], 340, 372, 56, values["gpu_load"],
              unit="%", width=180),
    ]
    return "gauges", "Приборы", widgets


LAYOUTS = [theme_grid, theme_large, theme_bars, theme_gauges]


# ------------------------------------------------------------------------ палитра ---

def read_palette(path: str) -> dict:
    palette = {}
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                if "=" not in line or line.strip().startswith("#"):
                    continue
                key, value = line.split("=", 1)
                palette[key.strip()] = value.strip().strip('"').lstrip("#")
    except OSError:
        pass
    return palette


def rgb_from(palette: dict, key: str, fallback):
    value = palette.get(key)
    if not value or len(value) < 6:
        return fallback
    try:
        return [int(value[index:index + 2], 16) for index in (0, 2, 4)]
    except ValueError:
        return fallback


def build(palette_path: str, current_path: str | None) -> tuple[list, dict]:
    palette = read_palette(palette_path)
    background = rgb_from(palette, "background", [16, 18, 22])
    # Фон делаем чуть светлее самого тёмного цвета палитры, чтобы подписи и цифры
    # имели с чем контрастировать, но экран не слепил в темноте.
    background = [min(255, round(channel * 0.85 + 12)) for channel in background]

    accent = rgb_from(palette, "accent", [200, 200, 200])
    light = rgb_from(palette, "foreground", [230, 230, 230])
    # Подписи — светлые и приглушённые, значения — цветные и яркие: так глаз
    # сразу цепляется за цифры, а не читает всё одинаковым.
    label_color = readable([round(channel * 0.72) for channel in light], background, 5.0)
    raw_values = {
        "cpu_temp": accent,
        "cpu_load": rgb_from(palette, "color4", accent),
        "gpu_temp": rgb_from(palette, "color6", accent),
        "gpu_load": rgb_from(palette, "color2", accent),
    }
    seen = []
    for key, color in raw_values.items():
        # Если палитра свела несколько значений в один цвет, разводим их по
        # яркости: иначе на экране четыре одинаковые цифры.
        while any(contrast(color, other) < 1.35 for other in seen):
            color = [min(255, round(channel * 1.18 + 8)) for channel in color]
        seen.append(color)
        raw_values[key] = color
    values = {key: readable(color, background, 7.0) for key, color in raw_values.items()}

    sources = sources_from_current(current_path)
    templates = []
    for layout in LAYOUTS:
        theme_id, name, widgets = layout(sources, background, label_color, values)
        templates.append({
            "id": f"aio-{theme_id}",
            "name": name,
            "base_width": SIZE,
            "base_height": SIZE,
            "background": {"type": "color", "rgb": with_alpha(background)},
            "widgets": widgets,
            "rotated": False,
            "target_device": None,
        })
    report = {
        "background": background,
        "label": label_color[:3],
        "values": {key: color[:3] for key, color in values.items()},
    }
    return templates, report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--palette", default=os.path.expanduser(
        "~/.local/state/omarchy/current/theme/colors.toml"))
    parser.add_argument("--current", default=os.path.expanduser("~/.config/lianli/lcd_templates.json"))
    parser.add_argument("--write")
    parser.add_argument("--theme")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--report", action="store_true")
    args = parser.parse_args()

    if args.list:
        for layout in LAYOUTS:
            theme_id, name, _ = layout(SENSOR_FALLBACK, [0, 0, 0], [0, 0, 0], {
                "cpu_temp": [0, 0, 0], "cpu_load": [0, 0, 0],
                "gpu_temp": [0, 0, 0], "gpu_load": [0, 0, 0]})
            print(f"{theme_id}\t{name}")
        return 0

    templates, report = build(args.palette, args.current)
    if args.report:
        print(json.dumps(report, ensure_ascii=False))
    if args.write:
        payload = {"templates": templates}
        os.makedirs(os.path.dirname(args.write), exist_ok=True)
        with open(args.write, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        if args.theme:
            print(args.theme)
    return 0


if __name__ == "__main__":
    sys.exit(main())
