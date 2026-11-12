"""將辨識資料轉成可追溯的三態檢驗結果。"""
from __future__ import annotations

import math
from typing import Any

from .spec import Spec, SpecBand, to_float
from .il_review import evaluate_il_review

FREQUENCY_TO_GHZ = {"hz": 1e-9, "khz": 1e-6, "mhz": 1e-3, "ghz": 1.0}


def normalize_unit(unit: Any, *, unitless_swr: bool = False) -> str:
    normalized = str(unit or "").strip().casefold()
    return "swr" if normalized in {"swr", "vswr"} or (unitless_swr and normalized == "u") else normalized


def frequency_to_ghz(value: Any, unit: Any) -> float | None:
    numeric = to_float(value)
    multiplier = FREQUENCY_TO_GHZ.get(str(unit).strip().lower())
    return None if numeric is None or multiplier is None else numeric * multiplier


def evaluate_frequency(spec: Spec, frequency_raw: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    reasons: list[str] = []
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
            ("stop", stop, spec.frequency_range.stop_ghz,
             stop is not None and math.isclose(stop, spec.frequency_range.stop_ghz, rel_tol=1e-9, abs_tol=1e-12)),
        ):
            judgment = "UNKNOWN" if value is None or invalid_range else "PASS" if ok else "REVIEW" if key == "stop" else "FAIL"
            frequency[f"{key}_judgment"] = judgment
            note = ""
            if judgment == "REVIEW":
                note = f"掃頻終點與 spec 不符：實際 {value:g} GHz，規格 stop_ghz = {limit:g} GHz，待人工複判"
            if judgment == "FAIL":
                note = f"掃頻起點超規：{value} GHz，要求 ≤ {limit} GHz"
            frequency[f"{key}_note"] = note
            if note:
                reasons.append(note)
        frequency["note"] = "；".join(filter(None, (frequency["start_note"], frequency["stop_note"])))
        if incomplete:
            note = "掃頻數據缺漏、單位無效或起終點順序錯誤"
            frequency["note"] = "；".join(filter(None, (frequency["note"], note)))
            for key in ("start", "stop"):
                if frequency[f"{key}_judgment"] == "UNKNOWN":
                    frequency[f"{key}_note"] = note
            reasons.append(note)
        frequency["judgment"] = ("FAIL" if frequency["start_judgment"] == "FAIL" else "UNKNOWN" if incomplete
                                 else "REVIEW" if frequency["stop_judgment"] == "REVIEW" else "PASS")
    return frequency, reasons


def _same_frequency(actual: Any, required: float) -> bool:
    value = to_float(actual)
    return value is not None and math.isclose(value, required, rel_tol=1e-9, abs_tol=1e-12)


def evaluate(spec: Spec, raw: dict[str, Any], *, il_trace_name: str | None = None,
             unitless_swr: bool = False) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("辨識結果不是 JSON 物件")
    reasons: list[str] = []
    judgments: list[str] = []

    def compare(name: str, values: list[float | None], unit: Any, *, trace: bool = False,
                band: SpecBand | None = None) -> dict[str, Any]:
        item = spec.find(name)
        limits = band or item
        result = {"name": name, "unit": str(unit) if unit is not None else None, "spec_min": limits.min if limits else None,
                  "spec_max": limits.max if limits else None, "spec_unit": item.unit if item else None}
        notes: list[str] = []
        if item is None:
            result.update(judgment="NO_SPEC", note="非 spec 要求項目，僅記錄")
            return result
        if band:
            result["frequency_band"] = {"start_ghz": band.start_ghz, "stop_ghz": band.stop_ghz}
            name = f"{name}（{band.start_ghz:g}–{band.stop_ghz:g} GHz）"
        unknown = False
        swr_ratio = unitless_swr and item.name.casefold() in {"s11", "s22"}
        if normalize_unit(unit, unitless_swr=swr_ratio) != normalize_unit(item.unit, unitless_swr=swr_ratio):
            unknown = True
            notes.append(f"{name} 單位不符：實際 {unit or '缺少'}，要求 {item.unit}")
        else:
            for value in values:
                if value is not None and ((limits.min is not None and value < limits.min) or (limits.max is not None and value > limits.max)):
                    notes.append(f"{name} 超規：{value} {unit}，規格 {limits.min} ~ {limits.max}")
        failed = bool(notes) and not unknown
        if any(value is None for value in values):
            unknown = True
            notes.append(f"{name} 必要數據缺漏")
        if trace and all(value is not None for value in values) and values[0] > values[1]:
            unknown = True
            notes.append(f"{name} 實測最小值大於最大值")
        note = "；".join(notes)
        if band:
            note = f"頻段 {band.start_ghz:g}–{band.stop_ghz:g} GHz" + (f"；{note}" if note else "")
        result.update(judgment="UNKNOWN" if unknown else "FAIL" if failed else "PASS", note=note)
        judgments.append(result["judgment"])
        reasons.extend(notes)
        return result

    traces = []
    seen = set()
    band_sources: dict[str, list[dict[str, Any]]] = {}
    for trace in raw.get("trace_extrema") or []:
        name = str(trace.get("name") or "").strip()
        item = spec.find(name)
        if item:
            seen.add(item.name)
            entries = trace.get("bands")
            if isinstance(entries, list):
                band_sources.setdefault(item.name, []).extend(entry for entry in entries if isinstance(entry, dict))
        minimum, maximum = to_float(trace.get("min")), to_float(trace.get("max"))
        result = compare(name, [minimum, maximum], trace.get("unit"), trace=True)
        result.update(min=minimum, max=maximum)
        traces.append(result)
    for item in spec.items:
        if item.name not in seen:
            result = compare(item.name, [None, None], item.unit, trace=True)
            result.update(min=None, max=None)
            traces.append(result)

    for item in spec.items:
        for band in item.bands:
            entries = [entry for entry in band_sources.get(item.name, [])
                       if _same_frequency(entry.get("start_ghz"), band.start_ghz)
                       and _same_frequency(entry.get("stop_ghz"), band.stop_ghz)]
            entry = entries[0] if len(entries) == 1 else {}
            minimum, maximum = to_float(entry.get("min")), to_float(entry.get("max"))
            result = compare(item.name, [minimum, maximum], entry.get("unit", item.unit), trace=True, band=band)
            result.update(min=minimum, max=maximum)
            traces.append(result)

    measurements = []
    for measurement in raw.get("measurements") or []:
        value = to_float(measurement.get("value"))
        name = str(measurement.get("name") or "").strip()
        item = spec.find(name)
        frequency_ghz = to_float(measurement.get("frequency_ghz"))
        bands = [band for band in item.bands if frequency_ghz is not None
                 and (frequency_ghz >= band.start_ghz or _same_frequency(frequency_ghz, band.start_ghz))
                 and (frequency_ghz <= band.stop_ghz or _same_frequency(frequency_ghz, band.stop_ghz))] if item else []
        unknown_band = bool(item and item.bands and not bands)
        for band in bands or [None]:
            result = compare(name, [None if unknown_band else value], measurement.get("unit"), band=band)
            result["value"] = value
            if item and item.bands:
                result["frequency_ghz"] = frequency_ghz
            if unknown_band:
                note = f"{name} Marker 頻率缺漏或不在 spec 頻段內，無法選擇分頻門檻"
                result["note"] += f"；{note}"
                reasons.append(note)
            measurements.append(result)

    frequency, frequency_reasons = evaluate_frequency(spec, raw.get("frequency") or {})
    reasons.extend(frequency_reasons)
    if spec.frequency_range:
        judgments.extend(frequency[f"{key}_judgment"] for key in ("start", "stop"))
    overall = ("FAIL" if "FAIL" in judgments else "UNKNOWN" if not judgments or "UNKNOWN" in judgments
               else "REVIEW" if "REVIEW" in judgments else "PASS")
    spec_overall = overall
    il_checks = evaluate_il_review(spec, raw, trace_name=il_trace_name)
    review_required = frequency.get("stop_judgment") == "REVIEW" or any(item["judgment"] == "REVIEW" for item in il_checks)
    reasons.extend(item["note"] for item in il_checks if item["judgment"] == "REVIEW")
    if review_required and overall == "PASS":
        overall = "REVIEW"
    return {"frequency": frequency, "measurements": measurements, "trace_extrema": traces,
            "spec_overall": spec_overall, "il_local_dips": il_checks, "review_required": review_required,
            "overall": overall, "reasons": list(dict.fromkeys(reasons)), "raw_vlm": raw,
            "vlm_pass_fail": raw.get("vlm_pass_fail")}
