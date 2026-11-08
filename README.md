# Aquarium Lab 智慧養殖監控

第一階段使用 8 尾模擬斑馬魚，從 Python 產生魚群軌跡與水質遙測。FastAPI 提供即時資料、設備控制和 SQLite 歷史查詢，Three.js 負責繪製互動式 3D 魚缸。斑馬魚的群游特性作為行為模型的設計參考。模擬速度、排程與透光度告警門檻是展示參數，實缸使用前需以觀測資料校正。 [斑馬魚群游研究](https://zfin.org/ZDB-PUB-070827-8)

## 本機啟動

需要 Python 3.11 以上、uv 和 Node.js 20.19+ 或 22.12+。

```powershell
npm.cmd --prefix frontend ci
npm.cmd --prefix frontend run build
uv sync --locked
uv run --locked uvicorn aquarium.main:app --host 127.0.0.1 --port 8000
```

- [控制台](http://127.0.0.1:8000)
- [互動 API 文件](http://127.0.0.1:8000/docs)

macOS 和 Linux 使用 `npm`。前端改動後重跑 build。預設資料庫為 `.data/aquarium.sqlite3`，可用 `AQUARIUM_DB` 指定路徑。請以單一 Uvicorn worker 執行，以共享即時魚況、設備狀態與排程。

## 展示流程

1. 等待約 15 秒，建立餵食前的觀測區間。可切換立體、側面和上方視角。
2. 按「立即餵食」，觀察魚依序游向投餌區。右下角比較餵食前後各 15 秒的平均游速、魚群平均間距與投餌區平均魚數，並顯示有效樣本數。
3. 按「追蹤遺失」，兩尾魚會在最後已知位置變灰約 7 秒。分析排除該段缺測，也不跨缺測計算游速。
4. 按「降低透光」，讀值降至 60% 以下時會顯示清理提醒。按「恢復透光」可讓讀值回升。
5. 可手動切換燈光或恢復自動。預設模擬餵食間隔為 180 秒，手動餵食會重新計時，兩次餵食至少間隔 30 秒。模擬照明每 80 秒循環，亮 65 秒、暗 15 秒。
6. 「重跑」以相同亂數種子重設魚群和模擬情境，並保留資料庫的歷史。餵食反應以當次運行為範圍，歷史趨勢可以跨重跑查詢。

歷史趨勢提供 1 分鐘、15 分鐘和 24 小時視窗。低速比例以低於 3 cm/s 的有效速度樣本計算。停留區域沿魚缸寬度分成左、中、右三區，按有效位置樣本累計。魚群平均間距使用同一時點的兩兩距離，投餌區則以投餌點為圓心、15 cm 為半徑計算。這些指標衡量可觀測的活動和餵食反應，實際攝食量可在影像辨識階段另行估算。

## 硬體接入

### 相機與水質遙測

設定硬體模式後，模擬資料源會停用。相機追蹤程式與感測器閘道各自用 HTTP 上傳資料，控制台和 SQLite 分析流程沿用。

```powershell
$env:AQUARIUM_MODE = "hardware"
$env:AQUARIUM_INGEST_TOKEN = [guid]::NewGuid().ToString("N")
uv run --locked uvicorn aquarium.main:app --host 127.0.0.1 --port 8000
```

| 介面 | 用途 |
| --- | --- |
| `POST /api/ingest/observations` | 上傳已融合的魚群 3D 追蹤畫格 |
| `POST /api/ingest/telemetry` | 上傳水溫與透光度讀值 |
| `WS /ws/live` | 向控制台推送最新觀測、遙測和控制狀態 |
| `GET /api/history` | 查詢同一資料來源的歷史走勢和事件 |

兩個上傳端點皆使用 `Authorization: Bearer <token>`，成功回傳 HTTP 204。每個串流的 `timestamp_ms` 使用 Unix 毫秒時間，須遞增且接近伺服器時間。同一串流由單一上游負責排序。以下是相機畫格與遙測的 JSON 結構，送出前用當下的時間戳替換範例值。

```json
{
  "timestamp_ms": 1794100000000,
  "tracks": [
    {"id": "fish-1", "status": "tracked", "position": {"x": 3.5, "y": 18, "z": -5}},
    {"id": "fish-2", "status": "lost"}
  ]
}
```

```json
{
  "timestamp_ms": 1794100000000,
  "temperature_c": 25.2,
  "transmittance_pct": 92
}
```

相機端先對齊側面和上方鏡頭的時間，完成校正、跨視角配對與 3D 定位，再送入完整畫格。座標使用 cm，從魚缸左到右為 `x`（−40～40），由缸底向上為 `y`（0～40），由後方向前為 `z`（−18～18）。持續追蹤同一個體時保留 ID，暫時遮擋時傳 `lost`。遙測每筆代表最新完整快照，當下無讀值的欄位可用 `null`。模擬與硬體的歷史依資料來源分開查詢。

### 設備驅動

在 [aquarium/contracts.py](aquarium/contracts.py) 實作 `Actuator.execute(command)`，再用 `create_app(mode="hardware", ingest_token=..., actuator=...)` 注入驅動。控制台會沿用原本的餵食與燈光按鈕。

- 餵食命令為 `FeedCommand`，燈光命令為 `LightCommand`。以命令 ID 做裝置端去重，確認完成後再回報成功。無法確認時拋出 `DeviceUnavailable`。
- 目前硬體模式可直接接收觀測。餵食與燈光會等實體驅動注入才啟用，硬體自動排程需另行設定。水溫控制器與打氣機可依同一命令模式增加專用型別。
- 伺服器預設只監聽本機。跨裝置整合時，把 API 放在受保護的網路或反向代理後，並為操作端加上存取控制。
