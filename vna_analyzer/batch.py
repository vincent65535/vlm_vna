"""工作目錄批次：固定輸入清單、按型號載入規格、保存後歸檔。"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from .config import Config
from .openrouter_client import SUPPORTED_EXTS, is_payment_error
from .report import LABELS, write_excel_report, write_json_result
from .spec import Spec


def local_now() -> datetime:
    return datetime.now(timezone(timedelta(hours=8)))


@dataclass(frozen=True)
class Job:
    source: Path
    relative: Path
    spec_path: Path
    signature: tuple[int, int]


def signature(path: Path) -> tuple[int, int]:
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns


def collect_jobs(workdir: Path) -> list[Job]:
    pending = workdir / "待檢驗"
    if not pending.is_dir():
        raise ValueError(f"找不到待檢驗目錄：{pending}")
    jobs = []
    for category in sorted(pending.iterdir()):
        if not category.is_dir() or category.is_symlink() or category.name.casefold() == "spec":
            continue
        for model in sorted(category.iterdir()):
            if not model.is_dir() or model.is_symlink() or model.name.casefold() == "spec":
                continue
            for source in sorted(model.rglob("*")):
                relative = source.relative_to(pending)
                if (source.is_file() and source.suffix.lower() in SUPPORTED_EXTS
                        and not any(part.casefold() == "spec" for part in relative.parts[2:-1])
                        and not source.is_symlink()
                        and not any(parent.is_symlink() for parent in source.parents if parent != pending and pending in parent.parents)):
                    jobs.append(Job(source, relative, category / "spec" / f"spec-{model.name}.json", signature(source)))
    return jobs


def error_result(source: Path, model: str, message: str) -> dict[str, Any]:
    return {"source_file": str(source), "model": model, "overall": "UNKNOWN", "error": message,
            "reasons": [message], "frequency": {}, "measurements": [], "trace_extrema": [], "vlm_pass_fail": None}


def run_batch(workdir: Path, config: Config, prompt: str,
              process: Callable[[Config, Spec, str, Path], dict[str, Any]]) -> int:
    workdir = workdir.expanduser().resolve()
    jobs = collect_jobs(workdir)
    if not jobs:
        print("待檢驗目錄沒有支援的圖片或 PDF。")
        return 0
    # Load once before API work so every model uses one consistent spec this batch.
    specs: dict[Path, Spec | str] = {}
    for job in jobs:
        if job.spec_path not in specs:
            try:
                specs[job.spec_path] = Spec.load(job.spec_path)
            except Exception as exc:
                specs[job.spec_path] = f"無法讀取 spec {job.spec_path.name}：{exc}"
    batch_dir = workdir / "已完成" / local_now().strftime("%Y%m%d_%H%M%S_%f")
    batch_dir.mkdir(parents=True, exist_ok=False)
    results = []
    payment_error = None
    for job in jobs:
        if payment_error is not None:
            result = error_result(job.source, config.model, "未檢驗：API 費用問題（HTTP 402），本批次已停止")
            result.update(category=job.relative.parts[0], product_model=job.relative.parts[1],
                          spec_file=str(job.spec_path), inspected_at=None, elapsed_seconds=None, skipped=True)
            results.append(result)
            continue
        print(f"檢驗 {job.relative} ...")
        started = time.perf_counter()
        inspected_at = local_now().isoformat()
        try:
            spec = specs[job.spec_path]
            if isinstance(spec, str):
                raise ValueError(spec)
            if signature(job.source) != job.signature:
                raise ValueError("原始檔在批次啟動後已變更，留待下次檢驗")
            result = process(config, spec, prompt, job.source)
        except Exception as exc:
            if is_payment_error(exc):
                payment_error = str(exc)
                print("API 費用問題（HTTP 402）：停止後續檢驗，保存本次結果。")
            result = error_result(job.source, config.model,
                                  f"API 費用問題（HTTP 402）：{exc}" if payment_error else str(exc))
        result.update(category=job.relative.parts[0], product_model=job.relative.parts[1],
                      spec_file=str(job.spec_path), inspected_at=inspected_at,
                      elapsed_seconds=round(time.perf_counter() - started, 2))
        results.append(result)
        print(f"  {LABELS.get(result['overall'], '無法判定')}：{'；'.join(result.get('reasons') or [])}")

    # Recheck all sources after processing; changed or disappeared files never move.
    for job, result in zip(jobs, results):
        try:
            unchanged = signature(job.source) == job.signature
        except OSError:
            unchanged = False
        if not unchanged:
            result["overall"] = "UNKNOWN"
            result.setdefault("reasons", []).append("原始檔在檢驗期間變更或消失，留待重試")
        result["file_link"] = str(batch_dir / job.relative if result["overall"] in ("PASS", "FAIL") else job.source)

    def save_outputs() -> None:
        for job, result in zip(jobs, results):
            write_json_result(batch_dir / "JSON" / job.relative.parent, job.source.name, result)
        write_excel_report(results, batch_dir / "report.xlsx")

    # Any write failure propagates before the first rename.
    save_outputs()
    moved: list[tuple[Path, Path]] = []
    try:
        for job, result in zip(jobs, results):
            if result["overall"] not in ("PASS", "FAIL"):
                continue
            if signature(job.source) != job.signature:
                result["overall"] = "UNKNOWN"
                result.setdefault("reasons", []).append("原始檔在歸檔前變更，留待重試")
                raise OSError(f"原始檔已變更：{job.source}")
            target = batch_dir / job.relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                raise FileExistsError(str(target))
            job.source.rename(target)
            moved.append((job.source, target))
    except OSError as exc:
        # Roll back completed renames and correct links to each actual location.
        recovery_errors = []
        for source, target in reversed(moved):
            try:
                if source.exists():
                    raise FileExistsError(str(source))
                target.rename(source)
            except OSError as recovery:
                recovery_errors.append(str(recovery))
        for job, result in zip(jobs, results):
            target = batch_dir / job.relative
            result["file_link"] = str(target if target.exists() else job.source)
            result["archive_error"] = f"歸檔失敗：{exc}" + (f"；還原異常：{'；'.join(recovery_errors)}" if recovery_errors else "；已還原移動")
        save_outputs()
        print(f"歸檔失敗，請查看報告：{batch_dir / 'report.xlsx'}")
        return 2
    print(f"Excel 報告：{batch_dir / 'report.xlsx'}")
    counts = {status: sum(result["overall"] == status for result in results) for status in ("PASS", "FAIL", "UNKNOWN")}
    print(f"完成：共 {len(results)} 個檔案，合格 {counts['PASS']}，不合格 {counts['FAIL']}，無法判定 {counts['UNKNOWN']}。")
    return 2 if payment_error is not None else 1 if counts["FAIL"] or counts["UNKNOWN"] else 0
