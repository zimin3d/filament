"""Граммы и время печати из файла слайсера.

Браузер присылает только нужный кусок файла (начало и конец G-code или
Metadata/slice_info.config из .3mf), поэтому размер файла не важен.
Поддержка: Creality Print / Orca / Bambu Studio / PrusaSlicer / SuperSlicer / Cura.
"""
import math
import re

DENSITY = {"PLA": 1.24, "PETG": 1.27, "ABS": 1.04, "ASA": 1.07, "TPU": 1.21, "PA": 1.14,
           "PC": 1.20, "HIPS": 1.04, "PVA": 1.23}
DEFAULT_DENSITY = 1.24


def density(material):
    mat = (material or "").upper()
    hits = [k for k in DENSITY if k in mat]
    return DENSITY[max(hits, key=len)] if hits else DEFAULT_DENSITY


def parse_time(raw):
    """«1d 2h 3m 4s», «2h 26m», «02:03:04», «3723» (секунды) -> минуты."""
    raw = raw.strip().lower()
    if re.fullmatch(r"\d+(\.\d+)?", raw):
        return round(float(raw) / 60)
    m = re.fullmatch(r"(?:(\d+):)?(\d+):(\d+)", raw)
    if m:
        return int(m.group(1) or 0) * 60 + int(m.group(2)) + round(int(m.group(3)) / 60)
    total = 0
    found = False
    for value, unit in re.findall(r"(\d+(?:\.\d+)?)\s*([dhms])", raw):
        found = True
        total += float(value) * {"d": 1440, "h": 60, "m": 1, "s": 1 / 60}[unit]
    return round(total) if found else None


def _numbers(raw):
    return [float(x) for x in re.findall(r"\d+(?:\.\d+)?", raw)]


def parse_gcode(text, material="", diameter=1.75):
    """Комментарии слайсеров в начале/конце G-code -> {grams, minutes, per_filament}."""
    grams = minutes = None
    per, colors, types = [], [], []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith(";"):
            continue
        low = line.lower()
        key, _, value = low[1:].partition("=") if "=" in low else low[1:].partition(":")
        key, value = key.strip(), value.strip()
        if key in ("total filament weight [g]", "total filament used [g]"):
            grams = sum(_numbers(value)) or grams
        elif key == "filament used [g]":
            nums = _numbers(value)
            if nums:
                per = nums
                grams = grams or sum(nums)
        elif key == "filament_colour" or (key == "extruder_colour" and not colors):
            colors = [c.strip() for c in re.split(r"[;,]", value) if c.strip()]
        elif key == "filament_type":
            types = [t.strip().upper() for t in re.split(r"[;,]", value) if t.strip()]
        elif key in ("estimated printing time (normal mode)", "total estimated time", "print time"):
            minutes = parse_time(value) or minutes
        elif key.startswith("model printing time") and ";" in value:
            # Orca: «model printing time: 1h 2m; total estimated time: 1h 5m»
            minutes = parse_time(value.split("total estimated time:")[-1]) or minutes
        elif key == "time" and minutes is None:  # Cura ;TIME:3723
            minutes = parse_time(value)
        elif key == "filament used" and grams is None and value.endswith("m"):  # Cura ;Filament used: 2.5m
            meters = sum(_numbers(value))
            area = math.pi * (diameter / 2) ** 2  # мм²
            grams = round(meters * 1000 * area * density(material) / 1000, 1)
    m = re.search(r";\s*model printing time:[^;\n]*;\s*total estimated time:\s*([^\n]+)", text, re.I)
    if m and minutes is None:
        minutes = parse_time(m.group(1))
    return {"grams": round(grams, 1) if grams else None, "minutes": minutes,
            "per_filament": [round(x, 1) for x in per] if len(per) > 1 else [],
            "filaments": _filaments(colors, types, per)}


def _filaments(colors, types, grams):
    """Список филаментов проекта: цвет, тип, граммы; неиспользуемые (0 г) отброшены."""
    n = max(len(colors), len(types), len(grams))
    out = []
    for i in range(n):
        g = grams[i] if i < len(grams) else None
        if g is not None and g <= 0:
            continue
        color = colors[i] if i < len(colors) else ""
        out.append({"slot": i + 1, "color": color.lower() if re.fullmatch(r"#[0-9a-fA-F]{6}", color) else "",
                    "type": types[i] if i < len(types) else "", "grams": round(g, 1) if g is not None else None})
    return out


def parse_slice_info(xml):
    """Metadata/slice_info.config из .3mf (Orca, Bambu, Creality Print)."""
    grams = minutes = None
    per, fil = [], []
    for plate in re.findall(r"<plate>(.*?)</plate>", xml, re.S) or [xml]:
        meta = dict(re.findall(r'<metadata key="([^"]+)" value="([^"]*)"', plate))
        if meta.get("weight"):
            grams = (grams or 0) + float(meta["weight"])
        if meta.get("prediction"):
            minutes = (minutes or 0) + round(float(meta["prediction"]) / 60)
        for tag in re.findall(r"<filament\b([^>]*)/?>", plate):
            attrs = dict(re.findall(r'(\w+)="([^"]*)"', tag))
            if attrs.get("used_g"):
                per.append(float(attrs["used_g"]))
                fil.append(attrs)
    if grams is None and per:
        grams = sum(per)
    return {"grams": round(grams, 1) if grams else None, "minutes": minutes,
            "per_filament": [round(x, 1) for x in per] if len(per) > 1 else [],
            "filaments": _filaments([f.get("color", "") for f in fil], [f.get("type", "").upper() for f in fil],
                                    [float(f["used_g"]) for f in fil])}
