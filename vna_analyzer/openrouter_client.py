"""OpenRouter VLM 客戶端：圖片/PDF 上傳與 JSON 解析。"""
from __future__ import annotations

import base64
import io
import json
import math
import mimetypes
import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import fitz  # PyMuPDF
import requests
from PIL import Image

from .config import Config

API_URL = "https://openrouter.ai/api/v1/chat/completions"

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
PDF_EXTS = {".pdf"}
SUPPORTED_EXTS = IMAGE_EXTS | PDF_EXTS

MAX_DIMENSION = 2048
MAX_BYTES = 4 * 1024 * 1024
RATE_LIMIT_DELAYS = (10, 20, 40)


def is_payment_error(exc: Exception) -> bool:
    """HTTP 402 is fatal for the batch; do not retry later files."""
    return isinstance(exc, requests.HTTPError) and exc.response is not None and exc.response.status_code == 402


def batch_stop_reason(exc: Exception) -> str | None:
    """A raised 429 has already exhausted the client's per-file retry budget."""
    if is_payment_error(exc):
        return "API 費用問題（HTTP 402）"
    if isinstance(exc, requests.HTTPError) and exc.response is not None and exc.response.status_code == 429:
        return "API 限流（HTTP 429），重試次數已耗盡"
    return None


def retry_delay(response: requests.Response, fallback: int) -> float:
    """Honor Retry-After seconds or HTTP date; malformed headers use backoff."""
    header = response.headers.get("Retry-After", "").strip()
    try:
        seconds = float(header)
        if math.isfinite(seconds) and seconds >= 0:
            return seconds
    except ValueError:
        pass
    try:
        retry_at = parsedate_to_datetime(header)
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        return max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())
    except (ValueError, TypeError, OverflowError):
        return float(fallback)


def _downscale_image(path: Path) -> bytes:
    img = Image.open(path)
    img = img.convert("RGB")
    if max(img.size) > MAX_DIMENSION:
        ratio = MAX_DIMENSION / max(img.size)
        img = img.resize((int(img.width * ratio), int(img.height * ratio)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


def image_to_data_url(path: Path) -> str:
    data = path.read_bytes()
    mime = mimetypes.guess_type(path.name)[0] or "image/jpeg"
    if len(data) > MAX_BYTES:
        data = _downscale_image(path)
        mime = "image/jpeg"
    else:
        img = Image.open(io.BytesIO(data))
        if max(img.size) > MAX_DIMENSION:
            data = _downscale_image(path)
            mime = "image/jpeg"
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def pdf_to_image_data_urls(path: Path) -> list[str]:
    urls: list[str] = []
    with fitz.open(path) as doc:
        for page in doc:
            pix = page.get_pixmap(dpi=150)
            b64 = base64.b64encode(pix.tobytes("png")).decode("ascii")
            urls.append(f"data:image/png;base64,{b64}")
    return urls


def extract_json(text: str) -> dict[str, Any]:
    """從 VLM 回傳文字中取出 JSON 物件（容忍 markdown code fence 與前後說明文字）。"""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        text = text[start : end + 1]
    return json.loads(text)


def call_vlm(config: Config, prompt: str, image_paths: list[Path], model: str | None = None) -> dict[str, Any]:
    """上傳圖片/PDF 並呼叫 OpenAI 相容 VLM API，回傳 JSON dict。"""
    model = model or config.model
    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    for path in image_paths:
        if path.suffix.lower() in PDF_EXTS:
            content.extend(
                {"type": "image_url", "image_url": {"url": url}}
                for url in pdf_to_image_data_urls(path)
            )
        else:
            content.append(
                {"type": "image_url", "image_url": {"url": image_to_data_url(path)}}
            )

    payload: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": content}],
        "response_format": {"type": "json_object"},
        "temperature": 0,
    }
    headers = {
        "Authorization": f"Bearer {config.api_key}",
        "Content-Type": "application/json",
    }
    retries = 0
    while True:
        resp = requests.post(config.api_url, headers=headers, json=payload, timeout=config.timeout)
        if resp.status_code == 429 and retries < len(RATE_LIMIT_DELAYS):
            delay = retry_delay(resp, RATE_LIMIT_DELAYS[retries])
            retries += 1
            print(f"API 限流（HTTP 429），{delay:g} 秒後重試（{retries}/{len(RATE_LIMIT_DELAYS)}）", flush=True)
            time.sleep(delay)
            continue
        if resp.status_code == 400 and "response_format" in payload:
            # Compatibility retry shares the same 429 budget; never reset it.
            payload.pop("response_format")
            continue
        break
    resp.raise_for_status()
    data = resp.json()
    try:
        message = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"{config.provider} 回傳格式異常: {data}") from exc
    return extract_json(message)
