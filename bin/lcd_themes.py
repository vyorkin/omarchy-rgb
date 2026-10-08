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

# Шрифты для экрана. Жирное начертание для значений и обычное для подписей — оба
# из стандартного набора Liberation, он есть в Arch вместе с fonts-liberation.
FONT_BOLD = "/usr/share/fonts/liberation/LiberationSans-Bold.ttf"
FONT_REGULAR = "/usr/share/fonts/liberation/LiberationSans-Regular.ttf"

# Ширина одного знака в долях кегля. Не оценка: измерено у самих файлов шрифтов
# (Pillow, getlength("100")/3/размер), поэтому размеры рамок и кегли считаются
# по факту. У Liberation Sans Bold знак занимает 0.556 кегля, у обычного столько
# же; прежде здесь стояла оценка 0.66, из-за чего кегль получался на четверть
# меньше возможного.
DIGIT_RATIO = 0.556
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

def font_ref(path: str) -> dict:
    """Ссылка на шрифт для виджета.

    Пустой объект означает встроенный шрифт демона. Указанный файл он загружает
    сам; требуется обычный файл не больше 32 МиБ, что для системных шрифтов так.
    """
    return {"path": path} if path and os.path.exists(path) else {}


def label(widget_id: str, text: str, x: float, y: float, size: float, color, width: float = 200):
    return {
        "id": widget_id,
        "kind": {"type": "label", "text": text, "font": font_ref(FONT_REGULAR), "font_size": size,
                 "color": with_alpha(color), "align": "center"},
        "x": x, "y": y, "width": width, "height": size * 1.4,
    }


def value_box(size: float, digits: int = 3, margin: float = 1.08) -> tuple[float, float]:
    """Рамка под число с запасом.

    Ширина считается по шрифту, а не на глаз: если рамка меньше нарисованного
    текста, дисплей затирает только её, и прежние цифры остаются на экране —
    выглядит как наложенные друг на друга символы. Единицу измерения поэтому
    показывает подпись, а значение занимает не больше трёх знаков.
    """
    width = size * DIGIT_RATIO * digits * margin
    height = size * 1.28 * margin
    return round(width), round(height)


def value(widget_id: str, source, x: float, y: float, size: float, color,
          unit: str = "", width: float | None = None, value_max: float = 100):
    box_width, box_height = value_box(size)
    return {
        "id": widget_id,
        "kind": {"type": "value_text", "source": source, "format": "{:.0}", "unit": unit,
                 "font": font_ref(FONT_BOLD), "font_size": size, "color": with_alpha(color),
                 "align": "center", "value_min": 0, "value_max": value_max},
        "x": x, "y": y, "width": width or box_width, "height": box_height,
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
    # Ячейка 480x480 делится на четыре по 240: и подписи, и значения остаются
    # внутри своей ячейки, иначе соседние виджеты затирают друг друга.
    cells = {
        "cpu_temp": (120, 38, 158),
        "cpu_load": (360, 38, 158),
        "gpu_temp": (120, 268, 388),
        "gpu_load": (360, 268, 388),
    }
    for key, (x, label_y, value_y) in cells.items():
        unit = "°C" if key.endswith("temp") else "%"
        maximum = 110 if key.endswith("temp") else 100
        # Подпись здесь вспомогательная, поэтому она мельче и ужата к краю: место
        # в ячейке отдано числу, ради которого на экран и смотрят.
        widgets.append(label(f"lbl-{key.replace('_', '-')}",
                             LABELS[key].replace("TEMP", "TEMP " + unit).replace("LOAD", "LOAD " + unit),
                             x, label_y, 26, labels, width=230))
        widgets.append(value(f"val-{key.replace('_', '-')}", sources[key], x, value_y, 130,
                             values[key], unit="", width=236, value_max=maximum))
    return "grid", "Сетка", widgets


def theme_large(sources: dict, background, labels, values) -> tuple[str, str, list]:
    widgets = [
        label("lbl-cpu-temp", "CPU °C", 120, 44, 32, labels, width=230),
        value("val-cpu-temp", sources["cpu_temp"], 120, 176, 128, values["cpu_temp"],
              unit="", width=230, value_max=110),
        label("lbl-gpu-temp", "GPU °C", 360, 44, 32, labels, width=230),
        value("val-gpu-temp", sources["gpu_temp"], 360, 176, 128, values["gpu_temp"],
              unit="", width=230, value_max=110),
        label("lbl-cpu-load", "CPU %", 120, 330, 30, labels, width=230),
        value("val-cpu-load", sources["cpu_load"], 120, 420, 76, values["cpu_load"],
              unit="", width=230),
        label("lbl-gpu-load", "GPU %", 360, 330, 30, labels, width=230),
        value("val-gpu-load", sources["gpu_load"], 360, 420, 76, values["gpu_load"],
              unit="", width=230),
    ]
    return "large", "Крупные цифры", widgets


def theme_bars(sources: dict, background, labels, values) -> tuple[str, str, list]:
    widgets = []
    # Четыре строки на 480 px — по 116 px на каждую: значение с полосой под ним
    # должны укладываться в свою строку, иначе рамки начинают пересекаться.
    rows = [("cpu_temp", 58), ("cpu_load", 176), ("gpu_temp", 294), ("gpu_load", 412)]
    for key, y in rows:
        unit = "°C" if key.endswith("temp") else "%"
        maximum = 110 if key.endswith("temp") else 100
        widgets.append(label(f"lbl-{key.replace('_', '-')}", f"{LABELS[key]} {unit}",
                             120, y, 30, labels, width=220))
        widgets.append(value(f"val-{key.replace('_', '-')}", sources[key], 392, y, 66,
                             values[key], unit="", width=150, value_max=maximum))
        widgets.append(bar(f"bar-{key.replace('_', '-')}", sources[key], 240, y + 54, 400,
                           values[key], background))
    return "bars", "Полосы", widgets


def theme_gauges(sources: dict, background, labels, values) -> tuple[str, str, list]:
    widgets = [
        gauge("gauge-cpu-temp", sources["cpu_temp"], 130, 168, 190, values["cpu_temp"],
              background, value_max=110),
        gauge("gauge-gpu-temp", sources["gpu_temp"], 350, 168, 190, values["gpu_temp"],
              background, value_max=110),
        label("lbl-cpu-temp", "CPU °C", 130, 40, 30, labels, width=180),
        label("lbl-gpu-temp", "GPU °C", 350, 40, 30, labels, width=180),
        value("val-cpu-temp", sources["cpu_temp"], 130, 168, 64, values["cpu_temp"],
              unit="", width=140, value_max=110),
        value("val-gpu-temp", sources["gpu_temp"], 350, 168, 64, values["gpu_temp"],
              unit="", width=140, value_max=110),
        label("lbl-cpu-load", "CPU LOAD %", 130, 306, 26, labels, width=220),
        value("val-cpu-load", sources["cpu_load"], 130, 392, 74, values["cpu_load"],
              unit="", width=200),
        label("lbl-gpu-load", "GPU LOAD %", 350, 306, 26, labels, width=220),
        value("val-gpu-load", sources["gpu_load"], 350, 392, 74, values["gpu_load"],
              unit="", width=200),
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


PLACEHOLDER = None


def check(templates: list) -> list:
    """Проверяет макет до отправки на экран.

    Рамка виджета — это и область, которую дисплей затирает перед новой
    отрисовкой. Поэтому важны две вещи: текст должен в неё помещаться (иначе
    остаются прежние символы, и цифры накладываются друг на друга), и рамки не
    должны пересекаться между собой.
    """
    problems = []
    for template in templates:
        boxes = []
        for widget in template["widgets"]:
            kind = widget["kind"]
            x, y = widget["x"], widget["y"]
            width, height = widget["width"], widget["height"]
            boxes.append((widget["id"], x - width / 2, y - height / 2, x + width / 2, y + height / 2))
            size = kind.get("font_size", 0)
            if kind["type"] == "label":
                needed = len(kind.get("text", "")) * size * DIGIT_RATIO
            elif kind["type"] == "value_text":
                needed = 3 * size * DIGIT_RATIO
            else:
                needed = 0
            if needed and needed > width:
                problems.append(
                    f"{template['id']}: текст {widget['id']} шире рамки "
                    f"({needed:.0f} > {width:.0f})")
        kinds = {widget["id"]: widget["kind"]["type"] for widget in template["widgets"]}

        def allowed_overlap(first_id: str, second_id: str) -> bool:
            # Число внутри круглой шкалы — задуманная композиция: кольцо рисуется
            # по краю, середина остаётся под значение.
            pair = {kinds.get(first_id), kinds.get(second_id)}
            return pair == {"radial_gauge", "value_text"}

        for index, first in enumerate(boxes):
            if first[1] < 0 or first[2] < 0 or first[3] > SIZE or first[4] > SIZE:
                problems.append(f"{template['id']}: {first[0]} выходит за экран")
            for second in boxes[index + 1:]:
                if allowed_overlap(first[0], second[0]):
                    continue
                if (first[1] < second[3] and second[1] < first[3]
                        and first[2] < second[4] and second[2] < first[4]):
                    problems.append(f"{template['id']}: {first[0]} и {second[0]} перекрываются")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--palette", default=os.path.expanduser(
        "~/.local/state/omarchy/current/theme/colors.toml"))
    parser.add_argument("--current", default=os.path.expanduser("~/.config/lianli/lcd_templates.json"))
    parser.add_argument("--write")
    parser.add_argument("--theme")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--report", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()

    if args.list:
        for layout in LAYOUTS:
            theme_id, name, _ = layout(SENSOR_FALLBACK, [0, 0, 0], [0, 0, 0], {
                "cpu_temp": [0, 0, 0], "cpu_load": [0, 0, 0],
                "gpu_temp": [0, 0, 0], "gpu_load": [0, 0, 0]})
            print(f"{theme_id}\t{name}")
        return 0

    templates, report = build(args.palette, args.current)
    if args.check:
        problems = check(templates)
        for problem in problems:
            print(problem)
        print(f"проверено {len(templates)} тем, замечаний {len(problems)}")
        return 1 if problems else 0
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
