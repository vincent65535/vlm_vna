# VNA 量測圖 VLM 解析器

用 OpenRouter 的 VLM（視覺語言模型）解析 VNA（向量網路分析儀）螢幕截圖或 PDF，
提取右上角量測數據、線圖最大值/最小值、Pass/Fail 標記，並依 `spec.json` 規格自動比對判定合規。

## 功能

- 支援圖片檔：PNG / JPG / JPEG / WebP / BMP
- 支援 PDF（自動將每頁轉為圖片後送給 VLM）
- 單一檔案或整個資料夾批次處理
- API 回傳 HTTP 402（費用／付款問題）時立即停止後續呼叫，保存本次結果；剩餘檔案註明未檢驗並留待重試。
- VLM 提取結果與 `spec.json` 規格比對，程式端判定逐項及整體 Pass / Fail
- 輸出每個檔案的 JSON 明細 + 一份 Excel 彙整報告

## 安裝

```bat
cd D:\works\vlm_vna
venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
```

編輯 `.env`，填入你的 OpenRouter API key（申請：https://openrouter.ai/keys）。

也可在同一份 `.env` 設定 OpenRouter 模型：

```dotenv
OPENROUTER_MODEL=google/gemini-3.8-flash
```

模型選擇順序為：命令列 `--model` → `.env`／環境變數 `OPENROUTER_MODEL` → 預設 `google/gemini-3.8-flash`。未設定或值為空白時使用預設模型；既有 `.env` 載入規則為檔案值優先於同名環境變數。此設定適用於工作目錄及指定路徑兩種模式，不影響 NVIDIA 的預設模型。

## 使用方式

### 工作目錄批次檢驗

不帶路徑時，預設以 `C:\public` 作為工作目錄，也可以用 `--workdir` 指定：

```bat
python -m vna_analyzer
python -m vna_analyzer --workdir C:\public
```

目錄配置：

```text
C:\public\
  待檢驗\
    衰減器\
      MVE2210-05\001.png
      MVE2210-10\001.png
      spec\
        spec-MVE2210-05.json
        spec-MVE2210-10.json
  已完成\
    <批次時間>\
      report.xlsx
      衰減器\<型號>\<原始檔案>
      JSON\衰減器\<型號>\<原始檔名含副檔名>.json
```

- 支援多個產品類別、型號及型號內的子目錄；所有 `spec` 子目錄都不作為檢驗輸入。
- 啟動時固定本次檔案清單及各型號規格；執行中新加入的檔案留待下次。
- 每個型號只使用同類別 `spec\spec-<型號>.json`，缺少或損壞時列為「無法判定」，不呼叫 VLM。
- 完成本次全部檔案後，先保存 JSON 及一份 Excel，再將合格、不合格的原始檔移至該批次目錄。無法判定者保留在「待檢驗」，下次會重試。
- JSON 與原檔保留類別、型號及子目錄；JSON 名稱包含原始副檔名，因此 `001.png` 和 `001.pdf` 也不會覆蓋。
- 保存報告失敗時不搬動原檔。歸檔失敗時會嘗試還原已移動的檔案，並修正報告連結與記錄歸檔錯誤。
- 原檔在處理期間變更或消失時不歸檔，列為「無法判定」。請在執行前完成檔案複製，避免檢驗期間修改原檔。
- 批次時間及檢驗時間使用台灣時間（UTC+8）。

### 指定單檔或資料夾（保留原檔）

基本用法（解析單一檔案）：

```bat
python -m vna_analyzer path\to\vna_screenshot.png
```

批次處理資料夾內所有圖片與 PDF：

```bat
python -m vna_analyzer path\to\report_folder
```

### 常用參數

| 參數 | 說明 |
|---|---|
| `--workdir <目錄>` | 工作目錄批次模式，預設 `C:\public`；不可與位置參數 `path` 同時使用 |
| `--prompt "..."` | 自訂解析 prompt（預設為內建 VNA 提取 prompt） |
| `--model <模型>` | 覆蓋設定檔模型；OpenRouter 未設定時預設 `google/gemini-3.8-flash` |
| `--spec <檔案>` | 規格檔路徑（預設 `./spec.json`） |
| `--output <資料夾>` | 輸出資料夾（預設 `./output`） |
| `--excel <檔案>` | Excel 彙整報告路徑（預設 `<output>/report.xlsx`） |
| `--no-excel` | 不產生 Excel 報告 |
| `--timeout <秒>` | API 逾時秒數（預設 120） |

`--spec`、`--output`、`--excel`、`--no-excel` 僅適用於指定單檔／資料夾模式。工作目錄模式固定依型號選 spec，並在本次「已完成」批次目錄產生 Excel。

### 自訂 prompt 範例

```bat
python -m vna_analyzer screenshot.png --prompt "幫我把這圖裡的右上角數據提取出來，並找出線圖的最大值和最小值，判斷 Pass 或 Fail，以 JSON 回傳。"
```

## 規格檔格式（spec.json）

```json
{
  "measurements": {
    "S11": { "min": -30, "max": -10, "unit": "dB" },
    "S21": { "min": -3, "max": 0, "unit": "dB" }
  }
}
```

- `min` / `max` 可省略其一（只做單邊限制）；兩者皆省略代表僅記錄不限。
- 名稱比對會忽略大小寫與符號（例如 VLM 回傳 `S-11` 仍會對應到 `S11`）。
- VLM 回傳的量測名稱在規格中找不到時，判定為 `NO_SPEC`（不影響其他項目）。
- 每個量測規格必須有有效的 `unit`；上下限必須是有限數值，且下限不可高於上限。
- 此 VNA 檢驗流程將 `SWR` 與 `VSWR` 視為同義單位（忽略大小寫及前後空白），曲線與 Marker 都適用。比對時使用同義規則，JSON 與 Excel 保留辨識回傳的名稱；其他不同單位仍列為單位不符。
- 每個 spec 量測項目都必須辨識到整條曲線的最小值與最大值，Marker 不可取代整條曲線。圖面 Pass/Fail 僅記錄於 JSON，程式依 spec 判定。
- 可加入 `frequency_range: {"start_ghz": 0.01, "stop_ghz": 8.0}`，要求實際掃頻起點不高於 0.01 GHz、終點不低於 8 GHz。
- 必要數據缺漏、單位不符或辨識失敗為「無法判定」；即使同時有超規也不歸檔，原因仍保留超規數據。完整資料超規為「不合格」，全部符合才為「合格」。

## 輸出

- `output\<檔名>.json`：每個檔案的完整解析結果（含 VLM 原始回傳）。
- 指定路徑模式的 Excel 預設為 `output\report_<時間>.xlsx`，原檔不會移動。JSON 依輸入子目錄保存，避免不同子目錄同名檔互相覆蓋。
- 工作目錄模式輸出位置請見上方目錄配置。

Excel 包含兩張工作表：

1. **檔案總表**：每個檔案一列。欄位為「檢驗結果、產品類別、型號、檔名、不合格／無法判定原因、使用的 spec、檢驗時間、處理耗時（秒）、檔案連結」。
2. **檢驗明細**：每個檢驗項目一列。欄位為「檔案整體結果、產品類別、型號、檔名、資料類型、檢驗項目、項目判定、規格下限、規格上限、實測最小值、實測最大值、Marker 值、單位、備註」。掃頻起點、終點分列，實際掃頻值放在數值欄「Marker 值」。

第一欄固定為中文結果：合格（綠色）、不合格（紅色）、無法判定（黃色）。明細的額外非規格項目顯示「無規格」（灰色）。兩張表開啟篩選並凍結標題及前四欄；數值與時間使用 Excel 原生格式，檔案連結指向歸檔後或仍待檢驗的原檔。

報告中的檔案連結及 spec 路徑預設將 `C:\public` 對應至 `\\192.168.1.101\public`，供其他電腦開啟。儲存、讀檔及歸檔仍使用本機路徑。若工作目錄或分享位置不同，可在 `.env` 設定（路徑不需加引號）：

```dotenv
REPORT_LOCAL_ROOT=C:\public
REPORT_SHARE_ROOT=\\192.168.1.101\public
```

只有位於 `REPORT_LOCAL_ROOT` 下的路徑會轉換；其他路徑保留原樣。設定只影響新產生的報告。

遇到 HTTP 402 時，已完成的結果仍會保存並按原規則歸檔；發生錯誤的檔案與尚未檢驗的檔案保留原位。Excel 仍列出本次完整清單，未檢驗者顯示「無法判定」，原因註明費用問題停止；未檢驗者不填檢驗時間或耗時。指定路徑模式同樣停止後續 API 呼叫。

結束碼：`0` 為全部合格或無待檢驗檔案；`1` 為含不合格／無法判定；`2` 為 API 費用問題、設定、保存或歸檔失敗。

## 測試

```bat
pip install pytest
python -m pytest tests -q
```

測試使用暫存目錄與模擬 VLM，不呼叫付費 API，也不搬動 `C:\public` 的原檔。
