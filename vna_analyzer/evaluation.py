"""將辨識資料轉成可追溯的三態檢驗結果。"""
from __future__ import annotations

from typing import Any

from .spec import Spec, to_float

FREQUENCY_TO_GHZ = {"hz": 1e-9, "khz": 1e-6, "mhz": 1e-3, "ghz": 1.0}


def normalize_unit(unit: Any) -> str:
    normalized = str(unit or "").strip().casefold()
    return "swr" if normalized in {"swr", "vswr"} else normalized


def frequency_to_ghz(value: Any, unit: Any) -> float | None:
    numeric = to_float(value)
    multiplier = FREQUENCY_TO_GHZ.get(str(unit).strip().lower())
    return None if numeric is None or multiplier is None else numeric * multiplier


def evaluate(spec: Spec, raw: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("辨識結果不是 JSON 物件")
    reasons: list[str] = []
    judgments: list[str] = []

    def compare(name: str, values: list[float | None], unit: Any, *, trace: bool = False) -> dict[str, Any]:
        item = spec.find(name)
        result = {"name": name, "unit": str(unit) if unit is not None else None, "spec_min": item.min if item else None,
                  "spec_max": item.max if item else None, "spec_unit": item.unit if item else None}
        notes: list[str] = []
        if item is None:
            result.update(judgment="NO_SPEC", note="非 spec 要求項目，僅記錄")
            return result
        unknown = False
        if normalize_unit(unit) != normalize_unit(item.unit):
            unknown = True
            notes.append(f"{name} 單位不符：實際 {unit or '缺少'}，要求 {item.unit}")
        else:
            for value in values:
                if value is not None and ((item.min is not None and value < item.min) or (item.max is not None and value > item.max)):
                    notes.append(f"{name} 超規：{value} {unit}，規格 {item.min} ~ {item.max}")
        failed = bool(notes) and not unknown
        if any(value is None for value in values):
            unknown = True
            notes.append(f"{name} 必要數據缺漏")
        if trace and all(value is not None for value in values) and values[0] > values[1]:
            unknown = True
            notes.append(f"{name} 實測最小值大於最大值")
        result.update(judgment="UNKNOWN" if unknown else "FAIL" if failed else "PASS", note="；".join(notes))
        judgments.append(result["judgment"])
        reasons.extend(notes)
        return result

    traces = []
    seen = set()
    for trace in raw.get("trace_extrema") or []:
        name = str(trace.get("name") or "").strip()
        item = spec.find(name)
        if item:
            seen.add(item.name)
        minimum, maximum = to_float(trace.get("min")), to_float(trace.get("max"))
        result = compare(name, [minimum, maximum], trace.get("unit"), trace=True)
        result.update(min=minimum, max=maximum)
        traces.append(result)
    for item in spec.items:
        if item.name not in seen:
            result = compare(item.name, [None, None], item.unit, trace=True)
            result.update(min=None, max=None)
            traces.append(result)

    measurements = []
    for measurement in raw.get("measurements") or []:
        value = to_float(measurement.get("value"))
        result = compare(str(measurement.get("name") or "").strip(), [value], measurement.get("unit"))
        result["value"] = value
        measurements.append(result)

    frequency_raw = raw.get("frequency") or {}
    start = frequency_to_ghz(frequency_raw.get("start"), frequency_raw.get("start_unit"))
    stop = frequency_to_ghz(frequency_raw.get("stop"), frequency_raw.get("stop_unit"))
    frequency = {"start_ghz": start, "stop_ghz": stop,
                 "required_start_ghz": spec.frequency_range.start_ghz if spec.frequency_range else None,
                 "required_stop_ghz": spec.frequency_range.stop_ghz if spec.frequency_range else None,
                 "judgment": "NO_SPEC", "note": ""}
    if spec.frequency_range:
        invalid_range = ((start is not None and start < 0) or (stop is not None and stop < 0)
                         or (start is not None and stop is not None and start >= stop))
        incomplete = start is None or stop is None or invalid_range
        for key, value, limit, ok in (
            ("start", start, spec.frequency_range.start_ghz, start is not None and start <= spec.frequency_range.start_ghz),
            ("stop", stop, spec.frequency_range.stop_ghz, stop is not None and stop >= spec.frequency_range.stop_ghz),
        ):
            judgment = "UNKNOWN" if value is None or invalid_range else "PASS" if ok else "FAIL"
            frequency[f"{key}_judgment"] = judgment
            judgments.append(judgment)
            if judgment == "FAIL":
                reasons.append(f"掃頻{'起點' if key == 'start' else '終點'}超規：{value} GHz，要求 {'≤' if key == 'start' else '≥'} {limit} GHz")
        if incomplete:
            frequency["note"] = "掃頻數據缺漏、單位無效或起終點順序錯誤"
            reasons.append(frequency["note"])
        frequency["judgment"] = "UNKNOWN" if incomplete else "FAIL" if not all(frequency[f"{key}_judgment"] == "PASS" for key in ("start", "stop")) else "PASS"
    overall = "UNKNOWN" if not judgments or "UNKNOWN" in judgments else "FAIL" if "FAIL" in judgments else "PASS"
    return {"frequency": frequency, "measurements": measurements, "trace_extrema": traces,
            "overall": overall, "reasons": list(dict.fromkeys(reasons)), "raw_vlm": raw,
            "vlm_pass_fail": raw.get("vlm_pass_fail")}
