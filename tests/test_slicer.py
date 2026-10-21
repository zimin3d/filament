import pytest

import slicer
from conftest import add_spool

ORCA = """; HEADER_BLOCK_START
; model printing time: 2h 20m 5s; total estimated time: 2h 26m 31s
; HEADER_BLOCK_END
G1 X10
; filament used [mm] = 15000.12, 300.5
; filament used [g] = 44.73, 0.90
; total filament used [g] = 45.63
; estimated printing time (normal mode) = 2h 26m 31s
"""
PRUSA = """G1 X1
; filament used [mm] = 3301.21
; filament used [g] = 9.85
; estimated printing time (normal mode) = 1d 1h 3m 4s
"""
CURA = """;FLAVOR:Marlin
;TIME:3723
;Filament used: 2.5m
;Layer height: 0.2
"""
SLICE_INFO = """<?xml version="1.0" encoding="UTF-8"?>
<config><plate>
  <metadata key="index" value="1"/>
  <metadata key="prediction" value="5400"/>
  <metadata key="weight" value="31.07"/>
  <filament id="1" type="PETG" color="#FF0000" used_m="9.5" used_g="28.50" />
  <filament id="2" type="PETG" color="#FFFFFF" used_m="0.8" used_g="2.57" />
</plate></config>"""


@pytest.mark.parametrize("text,grams,minutes,per", [
    (ORCA, 45.6, 147, [44.7, 0.9]),
    (PRUSA, 9.8, 1503, []),
    (CURA, 7.5, 62, []),  # 2,5 м PLA 1,75 мм ≈ 7,46 г
])
def test_parse_gcode(text, grams, minutes, per):
    r = slicer.parse_gcode(text, "PLA", 1.75)
    assert (r["grams"], r["minutes"], r["per_filament"]) == (grams, minutes, per)


def test_parse_slice_info():
    r = slicer.parse_slice_info(SLICE_INFO)
    assert (r["grams"], r["minutes"], r["per_filament"]) == (31.1, 90, [28.5, 2.6])
    assert r["filaments"] == [{"slot": 1, "color": "#ff0000", "type": "PETG", "grams": 28.5},
                              {"slot": 2, "color": "#ffffff", "type": "PETG", "grams": 2.6}]


@pytest.mark.parametrize("raw,minutes", [("2h 26m 31s", 147), ("1d 1h", 1500), ("01:30:00", 90), ("3723", 62), ("x", None)])
def test_parse_time(raw, minutes):
    assert slicer.parse_time(raw) == minutes


def test_slice_endpoint(admin, anon):
    sid = add_spool(admin)
    r = admin.post("/spool/%d/slice" % sid, data={"csrf": "t", "kind": "gcode", "text": ORCA})
    assert r.get_json()["grams"] == 45.6
    r = admin.post("/spool/%d/slice" % sid, data={"csrf": "t", "kind": "3mf", "text": SLICE_INFO})
    assert r.get_json()["minutes"] == 90
    assert anon.post("/spool/%d/slice" % sid, data={"csrf": "t"}).status_code in (302, 400)


def test_gcode_filament_list():
    text = ORCA + "; filament_colour = #FF0000;#FFFFFF;#00FF00\n; filament_type = PETG;PLA;PLA\n"
    r = slicer.parse_gcode(text)
    # третий цвет не использован (граммов нет в списке) — но граммов 2 значения, цвета 3
    assert r["filaments"][:2] == [{"slot": 1, "color": "#ff0000", "type": "PETG", "grams": 44.7},
                                  {"slot": 2, "color": "#ffffff", "type": "PLA", "grams": 0.9}]
