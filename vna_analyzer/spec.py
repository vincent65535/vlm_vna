"""規格載入、數值比對與判定。"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class SpecItem:
    name: str
    min: float | None
    max: float | None
    unit: str | None


@dataclass
class FrequencyRange:
    start_ghz: float
    stop_ghz: float


def _normalize(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


class Spec:
    def __init__(self, items: dict[str, SpecItem], frequency_range: FrequencyRange | None = None) -> None:
        self._items = items
        self.frequency_range = frequency_range
        self._normalized: dict[str, SpecItem] = {_normalize(name): item for name, item in items.items()}

    @classmethod
    def load(cls, path: Path | str) -> "Spec":
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict) or not isinstance(data.get("measurements", {}), dict):
            raise ValueError("spec 必須是物件，measurements 必須是物件")
        for name, limits in data.get("measurements", {}).items():
            if not name.strip() or not isinstance(limits, dict):
                raise ValueError("spec 量測名稱或規格格式錯誤")
            for key in ("min", "max"):
                value = limits.get(key)
                if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)):
                    raise ValueError(f"{name} 的 {key} 必須為有限數值")
            if limits.get("min") is not None and limits.get("max") is not None and limits["min"] > limits["max"]:
                raise ValueError(f"{name} 的下限不可大於上限")
            if not isinstance(limits.get("unit"), str) or not limits["unit"].strip():
                raise ValueError(f"{name} 缺少有效單位")
        items = {
            name: SpecItem(name=name, min=limits.get("min"), max=limits.get("max"), unit=limits.get("unit"))
            for name, limits in data.get("measurements", {}).items()
        }
        range_data = data.get("frequency_range")
        if range_data is not None:
            if not isinstance(range_data, dict):
                raise ValueError("spec 掃頻範圍必須是物件")
            for key in ("start_ghz", "stop_ghz"):
                value = range_data.get(key)
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                    raise ValueError(f"spec 掃頻 {key} 必須為有限數值")
        frequency_range = None if range_data is None else FrequencyRange(
            start_ghz=float(range_data["start_ghz"]), stop_ghz=float(range_data["stop_ghz"])
        )
        if frequency_range and (not math.isfinite(frequency_range.start_ghz) or not math.isfinite(frequency_range.stop_ghz)
                                or frequency_range.start_ghz < 0 or frequency_range.start_ghz >= frequency_range.stop_ghz):
            raise ValueError("spec 掃頻範圍無效")
        if not items and frequency_range is None:
            raise ValueError("spec 沒有檢驗項目")
        return cls(items, frequency_range)

    @property
    def items(self) -> list[SpecItem]:
        return list(self._items.values())

    def find(self, measurement_name: str) -> SpecItem | None:
        norm = _normalize(measurement_name)
        if norm in self._normalized:
            return self._normalized[norm]
        for key in sorted(self._normalized, key=len, reverse=True):
            if norm.startswith(key):
                return self._normalized[key]
        return None


def to_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(value) else None
    if isinstance(value, str):
        match = re.search(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?", value.replace(",", ""))
        if match:
            number = float(match.group())
            return number if math.isfinite(number) else None
    return None


def compare_measurement(spec: Spec, name: str, value: float, unit: str | None) -> dict[str, Any]:
    result: dict[str, Any] = {"name": name, "value": value, "unit": unit, "spec_min": None, "spec_max": None, "spec_unit": None, "judgment": "NO_SPEC"}
    item = spec.find(name)
    if item is None:
        return result
    result.update(spec_min=item.min, spec_max=item.max, spec_unit=item.unit)
    result["judgment"] = "FAIL" if (item.min is not None and value < item.min) or (item.max is not None and value > item.max) else "PASS"
    return result


def overall_judgment(judgments: list[str]) -> str:
    if not judgments:
        return "NO_DATA"
    if "FAIL" in judgments:
        return "FAIL"
    if "NO_DATA" in judgments:
        return "NO_DATA"
    if any(judgment == "PASS" for judgment in judgments):
        return "PASS"
    return "NO_SPEC"
