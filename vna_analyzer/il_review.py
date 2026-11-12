"""IL 局部凹陷觀察：只觸發人工複判，不代替數值規格判定。"""
from __future__ import annotations

import json
import re
from typing import Any

from .spec import Spec


def _key(name: str) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", name.casefold())


def evaluate_il_review(spec: Spec, raw: dict[str, Any], *, trace_name: str | None = None) -> list[dict[str, Any]]:
    expected: dict[str, str] = {}
    uncertain: set[str] = set()
    candidates = [vars(item) for item in spec.items] + (raw.get("trace_extrema") or [])
    for trace in candidates:
        name = str(trace.get("name") or "").strip()
        key = _key(name)
        kind = str(trace.get("measurement_type") or "").upper()
        transmission = key in {"s21", "s12"} and str(trace.get("unit") or "").strip().casefold() == "db"
        labelled_il = bool(re.search(r"(?i)(?:^|[^a-z])IL(?:$|[^a-z])|insertion\s*loss|插入損耗", name))
        if name and (name == trace_name if trace_name else transmission or labelled_il or kind in {"IL", "UNKNOWN"}):
            expected[key] = name
        if kind == "UNKNOWN":
            uncertain.add(key)

    if trace_name:
        expected[_key(trace_name)] = trace_name
    checks: list[dict[str, Any]] = []

    def add(name: str, status: str, evidence: str, dips: list[Any]) -> None:
        review = status != "CLEAR"
        note = f"{name}：" + ("異常局部凹陷，待人工複判" if status == "DIP" else
                              "異常局部凹陷無法確認，待人工複判" if review else "未見異常局部凹陷（允許緩變淺凹）")
        if evidence:
            note += f"；{evidence}"
        if dips:
            # Keep every observed dip and nullable estimate visible in the report.
            note += "；凹陷資料（GHz / dB）：" + json.dumps(dips, ensure_ascii=False)
        checks.append({"name": name, "status": status, "evidence": evidence, "dips": dips,
                       "judgment": "REVIEW" if review else "PASS", "note": note})

    entries = raw.get("il_local_dips")
    if not isinstance(entries, list):
        add("IL 檢查", "UNKNOWN", "缺少有效的 il_local_dips 檢查結果", [])
        entries = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict) or not str(entry.get("name") or "").strip():
            add("IL 檢查", "UNKNOWN", "局部凹陷檢查格式錯誤或缺少曲線名稱", [])
            continue
        name = str(entry["name"]).strip()
        key = _key(name)
        status = str(entry.get("status") or "").strip().upper()
        evidence = entry.get("evidence")
        dips = entry.get("dips")
        if (status not in {"DIP", "CLEAR", "UNKNOWN"} or not isinstance(evidence, str)
                or not evidence.strip() or not isinstance(dips, list)
                or (status == "CLEAR" and (dips or key in uncertain)) or key in seen):
            status = "UNKNOWN"
            evidence = "檢查結果缺漏、重複、類型不明或與凹陷資料矛盾"
        add(name, status, evidence, dips if isinstance(dips, list) else [])
        seen.add(key)
    for key, name in expected.items():
        if key not in seen:
            add(name, "UNKNOWN", "未回報此曲線的局部凹陷檢查", [])
    return checks
