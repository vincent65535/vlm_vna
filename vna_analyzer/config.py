"""設定載入：API key、模型與預設 prompt。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv(override=True)

DEFAULT_MODEL = "google/gemini-3.8-flash"
DEFAULT_TIMEOUT = 120
OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"
NVIDIA_API_URL = "https://integrate.api.nvidia.com/v1/chat/completions"

DEFAULT_PROMPT = (
    "你是一個 VNA（向量網路分析儀）量測報告解析助手。請仔細閱讀圖片內容，並以 JSON 格式回傳：\n"
    "1) 畫面右上角顯示的 marker 量測數據（名稱、數值、單位）。每個 name 必須包含所屬 trace；例如 S11 的 marker 1 必須命名為 S11_marker1、S22 的 marker 2 必須命名為 S22_marker2。禁止只回傳 marker1 或 marker2；\n"
    "2) 每一條 trace 在完整掃頻範圍的最小值與最大值。必須根據整條曲線與座標軸判讀，"
    "不可只使用 marker 值；無法可靠讀取則回傳 null；\n"
    "3) VNA 顯示的掃頻起點與終點（Start、Stop）及單位；\n"
    "4) 圖面上顯示的 Pass/Fail 標記（若圖面無標記則回 null）。\n"
    "單位必須忠實保留圖面文字：圖上寫 SWR 就回傳 SWR，寫 VSWR 就回傳 VSWR，不要依範例改名。\n"
    '回傳格式範例：{"measurements": [{"name": "S11_marker1", "value": 1.08, "unit": "SWR"}], '
    '"trace_extrema": [{"name": "S11", "min": 1.00, "max": 1.12, "unit": "SWR"}], '
    '"frequency": {"start": 10, "stop": 8, "start_unit": "MHz", "stop_unit": "GHz"}, '
    '"vlm_pass_fail": "PASS"}。'
)


def get_model(provider: str = "openrouter") -> str:
    if provider == "nvidia":
        return "moonshotai/kimi-k3"
    return os.getenv("OPENROUTER_MODEL", "").strip() or DEFAULT_MODEL


@dataclass
class Config:
    api_key: str
    model: str = field(default_factory=get_model)
    timeout: int = DEFAULT_TIMEOUT
    provider: str = "openrouter"

    @property
    def api_url(self) -> str:
        return NVIDIA_API_URL if self.provider == "nvidia" else OPENROUTER_API_URL


def get_api_key(provider: str = "openrouter") -> str:
    key_name = "NVIDIA_API_KEY" if provider == "nvidia" else "OPENROUTER_API_KEY"
    return os.getenv(key_name, "").strip()
