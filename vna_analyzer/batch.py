"""工作目錄批次：固定輸入清單、按型號載入規格、保存後歸檔。"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from .config import Config
from .divider import ISOLATION, MAIN, divider_filename, process_divider
from .openrouter_client import SUPPORTED_EXTS, batch_stop_reason, call_vlm
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


@dataclass(frozen=True)
class Inspection:
    jobs: tuple[Job, ...]
    divider_id: str | None = None
    pairing_error: str | None = None

    @property
    def is_divider(self) -> bool:
        return self.jobs[0].relative.parts[0].casefold() == "divider"


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


def group_inspections(jobs: list[Job]) -> list[Inspection]:
    inspections = []
    groups: dict[tuple[Path, str], list[tuple[Job, str | None, str | None]]] = {}
    for job in jobs:
        if job.relative.parts[0].casefold() != "divider":
            inspections.append(Inspection((job,)))
            continue
        identifier, role, error = divider_filename(job.source)
        if identifier is None:
            inspections.append(Inspection((job,), pairing_error=f"{job.source.name}：{error}"))
            continue
        groups.setdefault((job.spec_path, identifier), []).append((job, role, error))
    for (_, identifier), entries in groups.items():
        errors = [f"{job.source.name}：{error}" for job, _, error in entries if error]
        ordered = []
        for role in (MAIN, ISOLATION):
            matching = [job for job, kind, _ in entries if kind == role]
            if not matching:
                errors.append(f"缺少{role}")
            elif len(matching) > 1:
                errors.append(f"{role}重複（{len(matching)} 張）")
            ordered.extend(matching)
        ordered.extend(job for job, role, _ in entries if role is None)
        inspections.append(Inspection(tuple(ordered), identifier, "；".join(errors) or None))
    return sorted(inspections, key=lambda inspection: min(job.relative for job in inspection.jobs))


def sources_unchanged(inspection: Inspection) -> bool:
    try:
        return all(signature(job.source) == job.signature for job in inspection.jobs)
    except OSError:
        return False


def set_links(inspection: Inspection, result: dict[str, Any], locations: dict[Path, Path]) -> None:
    result["file_link"] = str(locations[inspection.jobs[0].source])
    if inspection.is_divider:
        reports = {entry["source_file"]: entry for entry in result.get("source_reports", [])}
        result["source_reports"] = []
        for job in inspection.jobs:
            source = str(job.source)
            report = reports.get(source)
            if report is None:
                _, role, _ = divider_filename(job.source)
                report = {"source_file": source, "role": role}
            report["file_link"] = str(locations[job.source])
            result["source_reports"].append(report)


def run_batch(workdir: Path, config: Config, prompt: str,
              process: Callable[[Config, Spec, str, Path], dict[str, Any]], *,
              archive_policy: str = "processed", read_vlm=call_vlm) -> int:
    if archive_policy not in ("processed", "decided"):
        raise ValueError("archive_policy 必須為 processed 或 decided")
    workdir = workdir.expanduser().resolve()
    inspections = group_inspections(collect_jobs(workdir))
    if not inspections:
        print("待檢驗目錄沒有支援的圖片或 PDF。")
        return 0
    # Load once before API work so every model uses one consistent spec this batch.
    specs: dict[Path, Spec | str] = {}
    for inspection in inspections:
        job = inspection.jobs[0]
        if job.spec_path not in specs:
            try:
                specs[job.spec_path] = Spec.load(job.spec_path)
            except Exception as exc:
                specs[job.spec_path] = f"無法讀取 spec {job.spec_path.name}：{exc}"
    batch_dir = workdir / "已完成" / local_now().strftime("%Y%m%d_%H%M%S_%f")
    batch_dir.mkdir(parents=True, exist_ok=False)
    results = []
    stop_reason = None
    for inspection in inspections:
        job = inspection.jobs[0]
        if stop_reason is not None:
            message = f"未檢驗：{stop_reason}，本批次已停止"
            if inspection.pairing_error:
                message += f"；{inspection.pairing_error}"
            result = error_result(job.source, config.model, message)
            result.update(category=job.relative.parts[0], product_model=job.relative.parts[1],
                          spec_file=str(job.spec_path), inspected_at=None, elapsed_seconds=None, skipped=True)
            results.append(result)
            continue
        print(f"檢驗 {job.relative} ...")
        started = time.perf_counter()
        inspected_at = local_now().isoformat()
        try:
            if inspection.pairing_error:
                raise ValueError(inspection.pairing_error)
            spec = specs[job.spec_path]
            if isinstance(spec, str):
                raise ValueError(spec)
            if not sources_unchanged(inspection):
                raise ValueError("原始檔在批次啟動後已變更，留待下次檢驗")
            if inspection.is_divider:
                result = process_divider(config, spec, job.source, inspection.jobs[1].source, read_vlm=read_vlm)
                stop_reason = result.pop("stop_reason", None)
            else:
                result = process(config, spec, prompt, job.source)
        except Exception as exc:
            stop_reason = batch_stop_reason(exc)
            result = error_result(job.source, config.model,
                                  f"{stop_reason}：{exc}" if stop_reason else str(exc))
            if stop_reason is not None:
                result["api_failed"] = True
        if stop_reason is not None:
            print(f"{stop_reason}：停止後續檢驗，保存本次結果。")
        result.update(category=job.relative.parts[0], product_model=job.relative.parts[1],
                      spec_file=str(job.spec_path), inspected_at=inspected_at,
                      elapsed_seconds=round(time.perf_counter() - started, 2))
        results.append(result)
        print(f"  {LABELS.get(result['overall'], '無法判定')}：{'；'.join(result.get('reasons') or [])}")

    # Recheck all sources after processing; changed or disappeared files never move.
    archive_units: set[Inspection] = set()
    for inspection, result in zip(inspections, results):
        unchanged = sources_unchanged(inspection)
        if not unchanged:
            result["overall"] = "UNKNOWN"
            result.setdefault("reasons", []).append("原始檔在檢驗期間變更或消失，留待重試")
        should_archive = (unchanged and not result.get("skipped") and not result.get("api_failed")
                          and not inspection.pairing_error and not result.get("keep_pending")
                          and (archive_policy == "processed" or result["overall"] in ("PASS", "FAIL")))
        if should_archive:
            archive_units.add(inspection)
        result["archive_policy"] = archive_policy
        if inspection.is_divider:
            result["divider_id"] = inspection.divider_id
        set_links(inspection, result, {
            job.source: batch_dir / job.relative if should_archive else job.source for job in inspection.jobs
        })

    def save_outputs() -> None:
        for inspection, result in zip(inspections, results):
            job = inspection.jobs[0]
            write_json_result(batch_dir / "JSON" / job.relative.parent, job.source.name, result)
        write_excel_report(results, batch_dir / "report.xlsx")

    # Any write failure propagates before the first rename.
    save_outputs()
    moved: list[tuple[Path, Path]] = []
    try:
        for inspection, result in zip(inspections, results):
            if inspection not in archive_units:
                continue
            if not sources_unchanged(inspection):
                result["overall"] = "UNKNOWN"
                result.setdefault("reasons", []).append("原始檔在歸檔前變更，留待重試")
                raise OSError(f"原始檔已變更：{inspection.jobs[0].source}")
            for job in inspection.jobs:
                target = batch_dir / job.relative
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    raise FileExistsError(str(target))
                job.source.rename(target)
                moved.append((job.source, target))
            if inspection.is_divider and any(signature(batch_dir / job.relative) != job.signature for job in inspection.jobs):
                result["overall"] = "UNKNOWN"
                result.setdefault("reasons", []).append("原始檔在歸檔期間變更，留待重試")
                raise OSError(f"原始檔已變更：{inspection.jobs[0].source}")
    except OSError as exc:
        # Roll back completed renames and correct links to each actual location.
        recovery_errors = []
        unrecovered = {}
        for source, target in reversed(moved):
            try:
                if source.exists():
                    raise FileExistsError(str(source))
                target.rename(source)
            except OSError as recovery:
                recovery_errors.append(str(recovery))
                if target.exists():
                    unrecovered[source] = target
        for inspection, result in zip(inspections, results):
            set_links(inspection, result, {job.source: unrecovered.get(job.source, job.source) for job in inspection.jobs})
            result["archive_error"] = f"歸檔失敗：{exc}" + (f"；還原異常：{'；'.join(recovery_errors)}" if recovery_errors else "；已還原移動")
        save_outputs()
        print(f"歸檔失敗，請查看報告：{batch_dir / 'report.xlsx'}")
        return 2
    print(f"Excel 報告：{batch_dir / 'report.xlsx'}")
    counts = {status: sum(result["overall"] == status for result in results) for status in ("PASS", "FAIL", "REVIEW", "UNKNOWN")}
    unit = "個檢驗單位" if any(inspection.is_divider for inspection in inspections) else "個檔案"
    print(f"完成：共 {len(results)} {unit}，合格 {counts['PASS']}，不合格 {counts['FAIL']}，待人工複判 {counts['REVIEW']}，無法判定 {counts['UNKNOWN']}。")
    return 2 if stop_reason is not None else 1 if any(counts[status] for status in ("FAIL", "REVIEW", "UNKNOWN")) else 0
