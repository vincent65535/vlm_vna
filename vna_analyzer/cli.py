"""VNA 檢驗命令列入口，支援工作目錄批次與既有指定路徑模式。"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Any

from .batch import error_result, local_now, run_batch
from .config import DEFAULT_MODEL, DEFAULT_PROMPT, Config, get_api_key, get_model
from .evaluation import evaluate, frequency_to_ghz  # re-export for existing callers
from .openrouter_client import SUPPORTED_EXTS, call_vlm, is_payment_error
from .report import LABELS, write_excel_report, write_json_result
from .spec import Spec


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="vna_analyzer", description="VNA 圖片/PDF 規格檢驗與 Excel 報告")
    parser.add_argument("path", nargs="?", help="既有模式：單一圖片/PDF 或資料夾；省略則使用工作目錄批次模式")
    parser.add_argument("--workdir", default=None, help=r"批次工作目錄（預設 C:\public）；不可與 path 同時使用")
    parser.add_argument("--prompt", default=DEFAULT_PROMPT, help="自訂解析 prompt")
    parser.add_argument("--provider", choices=("openrouter", "nvidia"), default="openrouter")
    parser.add_argument("--model", default=None, help=f"覆蓋設定檔中的模型（OpenRouter 未設定時預設 {DEFAULT_MODEL}）")
    parser.add_argument("--spec", default=None, help="指定路徑模式的規格檔（預設 ./spec.json）")
    parser.add_argument("--output", default=None, help="指定路徑模式的輸出目錄（預設 ./output）")
    parser.add_argument("--excel", default=None, help="指定路徑模式的 Excel 報告路徑")
    parser.add_argument("--no-excel", action="store_true", help="指定路徑模式不產生 Excel")
    parser.add_argument("--timeout", type=int, default=None, help="API 逾時秒數（OpenRouter 120；NVIDIA 300）")
    args = parser.parse_args(argv)
    if args.path and args.workdir is not None:
        parser.error("path 與 --workdir 不可同時使用")
    if not args.path and (args.spec is not None or args.output is not None or args.excel is not None or args.no_excel):
        parser.error("工作目錄批次模式固定依型號選 spec 並保存 Excel；--spec/--output/--excel/--no-excel 僅供指定路徑模式")
    if args.timeout is not None and args.timeout <= 0:
        parser.error("--timeout 必須大於 0")
    return args


def collect_files(path: Path) -> list[Path]:
    if path.is_file():
        return [path] if path.suffix.lower() in SUPPORTED_EXTS else []
    return sorted(p for p in path.rglob("*") if p.is_file() and p.suffix.lower() in SUPPORTED_EXTS)


def process_file(config: Config, spec: Spec, prompt: str, file_path: Path) -> dict[str, Any]:
    raw = call_vlm(config, prompt, [file_path], model=config.model)
    return {"source_file": str(file_path), "model": config.model, **evaluate(spec, raw)}


def print_summary(file_path: Path, result: dict[str, Any]) -> None:
    print(f"[{file_path.name}] {LABELS.get(result['overall'], '無法判定')}")
    for reason in result.get("reasons") or []:
        print(f"  {reason}")


def run_legacy(args: argparse.Namespace, config: Config) -> int:
    spec_path = Path(args.spec or "spec.json").resolve()
    spec = Spec.load(spec_path)
    source = Path(args.path).resolve()
    files = collect_files(source)
    if not files:
        print(f"錯誤：{source} 中沒有找到支援的圖片/PDF 檔案。", file=sys.stderr)
        return 2
    output_dir = Path(args.output or "output")
    results = []
    payment_error = None
    for file_path in files:
        if payment_error is None:
            print(f"解析 {file_path} ...")
        started = time.perf_counter()
        inspected = local_now().isoformat()
        try:
            if payment_error is not None:
                result = error_result(file_path, config.model, "未檢驗：API 費用問題（HTTP 402），本批次已停止")
                result["skipped"] = True
            else:
                result = process_file(config, spec, args.prompt, file_path)
        except Exception as exc:
            if is_payment_error(exc):
                payment_error = str(exc)
                print("API 費用問題（HTTP 402）：停止後續檢驗，保存本次結果。")
            result = error_result(file_path, config.model,
                                  f"API 費用問題（HTTP 402）：{exc}" if payment_error else str(exc))
        result.update(elapsed_seconds=None if result.get("skipped") else round(time.perf_counter() - started, 2),
                      inspected_at=None if result.get("skipped") else inspected, spec_file=str(spec_path))
        relative = Path(file_path.name) if source.is_file() else file_path.relative_to(source)
        # Keep the existing .json name unless equal stems would collide.
        same_stem = any(other != file_path and other.parent == file_path.parent and other.stem == file_path.stem for other in files)
        write_json_result(output_dir / relative.parent, file_path.name if same_stem else file_path.stem, result)
        print_summary(file_path, result)
        results.append(result)
    if not args.no_excel:
        excel_path = Path(args.excel) if args.excel else output_dir / f"report_{local_now():%Y%m%d_%H%M%S_%f}.xlsx"
        try:
            write_excel_report(results, excel_path)
        except PermissionError:
            excel_path = excel_path.with_name(f"{excel_path.stem}_{local_now():%Y%m%d_%H%M%S_%f}{excel_path.suffix}")
            write_excel_report(results, excel_path)
        print(f"Excel 彙整報告：{excel_path}")
    print(f"完成：共 {len(results)} 個檔案。")
    return 2 if payment_error is not None else 1 if any(result["overall"] != "PASS" for result in results) else 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    api_key = get_api_key(args.provider)
    if not api_key:
        key_name = "NVIDIA_API_KEY" if args.provider == "nvidia" else "OPENROUTER_API_KEY"
        print(f"錯誤：未設定 {key_name}。", file=sys.stderr)
        return 2
    config = Config(api_key=api_key, model=args.model or get_model(args.provider),
                    timeout=args.timeout or (300 if args.provider == "nvidia" else 120), provider=args.provider)
    try:
        if args.path:
            return run_legacy(args, config)
        return run_batch(Path(args.workdir or r"C:\public"), config, args.prompt, process_file)
    except Exception as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
