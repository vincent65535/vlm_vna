"""OpenRouter VLM 客戶端：圖片/PDF 上傳與 JSON 解析。"""
from __future__ import annotations

import base64
import io
import json
import mimetypes
import re
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


def is_payment_error(exc: Exception) -> bool:
    """HTTP 402 is fatal for the batch; do not retry later files."""
    return isinstance(exc, requests.HTTPError) and exc.response is not None and exc.response.status_code == 402


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
    resp = requests.post(config.api_url, headers=headers, json=payload, timeout=config.timeout)
    if resp.status_code == 400 and "response_format" in payload:
        # 部分模型不支援 response_format，移除後重試
        payload.pop("response_format")
        resp = requests.post(config.api_url, headers=headers, json=payload, timeout=config.timeout)
    resp.raise_for_status()
    data = resp.json()
    try:
        message = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"{config.provider} 回傳格式異常: {data}") from exc
    return extract_json(message)
