# Aquarium Lab 智慧養殖監控

控制台使用魚缸正面視角呈現 2D 位置和軌跡。預設以 Python 模擬 8 尾斑馬魚及水質遙測，並提供設備控制和 SQLite 歷史查詢。也可以用公開魚缸影片回放實際影像的 2D 追蹤結果。斑馬魚的群游特性作為行為模型的設計參考。模擬速度、排程與透光度告警門檻是展示參數，實缸使用前需以觀測資料校正。 [斑馬魚群游研究](https://zfin.org/ZDB-PUB-070827-8)

原始 3D 版本保存在本機分支 `codex/aquarium-3d`（提交 `3089330`）。若要另外開啟 3D 版本，可執行 `git worktree add ../bime01-3d codex/aquarium-3d`。

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

執行整合測試用 `uv run --locked python -m unittest discover -s tests -v`。

## 展示流程

1. 等待約 15 秒，建立餵食前的觀測區間。平面圖顯示魚的水平位置、高度與最近兩秒的軌跡。
2. 按「立即餵食」，觀察魚依序游向投餌區。右下角比較餵食前後各 15 秒的平均游速、魚群平均間距與投餌區平均魚數，並顯示有效樣本數。
3. 按「追蹤遺失」，兩尾魚會在最後已知位置變灰約 7 秒。分析排除該段缺測，也不跨缺測計算游速。
4. 按「降低透光」，讀值降至 60% 以下時會顯示清理提醒。按「恢復透光」可讓讀值回升。
5. 可手動切換燈光或恢復自動。預設模擬餵食間隔為 180 秒，手動餵食會重新計時，兩次餵食至少間隔 30 秒。模擬照明每 80 秒循環，亮 65 秒、暗 15 秒。
6. 「重跑」以相同亂數種子重設魚群和模擬情境，並保留資料庫的歷史。餵食反應以當次運行為範圍，歷史趨勢可以跨重跑查詢。

歷史趨勢提供 1 分鐘、15 分鐘和 24 小時視窗。游速、間距和投餌區統計使用正面平面座標。低速比例以低於 3 cm/s 的有效速度樣本計算。停留區域沿魚缸寬度分成左、中、右三區，按有效位置樣本累計。魚群平均間距使用同一時點的兩兩距離，投餌區則以投餌點為圓心、15 cm 為半徑計算。更新前記錄的 3D 運動統計仍保留在資料庫，2D 趨勢從更新後開始累計。

## Raspberry Pi Zero 2 W

在開發電腦建置前端後，將 `aquarium/`、`tests/`、`frontend/dist/`、`pyproject.toml` 和 `uv.lock` 傳到 Pi 的 `~/bime01`。Pi 可直接用 `uv sync --locked` 安裝 Python 套件。Pi 只有 USB 區網連線時，在開發電腦準備離線套件。以下以 Raspberry Pi OS Bookworm 32 位元和 Python 3.11 為例。

```powershell
uv export --locked --format requirements-txt --no-emit-project --output-file requirements-pi.txt
python -m pip download --require-hashes --only-binary=:all: --platform manylinux_2_31_armv7l --platform manylinux_2_17_armv7l --platform manylinux2014_armv7l --python-version 3.11 --abi cp311 --implementation cp --dest wheelhouse -r requirements-pi.txt
```

把 `requirements-pi.txt` 和 `wheelhouse/` 一起傳到 `~/bime01`，在 Pi 上執行。

```sh
cd ~/bime01
uv venv --python /usr/bin/python3 --no-python-downloads
uv pip sync --offline --no-index --find-links wheelhouse --require-hashes --python .venv/bin/python requirements-pi.txt
.venv/bin/python -m unittest discover -s tests -v
rpicam-hello --list-cameras
PYTHONPATH=/usr/lib/python3/dist-packages AQUARIUM_CAMERA=1 .venv/bin/python -m uvicorn aquarium.main:app --host 127.0.0.1 --port 8000
```

在開發電腦另開終端機執行 `ssh -N -L 8000:127.0.0.1:8000 kelsier@raspberrypi.local`，保留連線後開啟 [控制台](http://127.0.0.1:8000)。設定 `AQUARIUM_CAMERA=1` 時，伺服器預設使用實體模式。展開「實體相機」，按「啟動即時影像」後，主畫面會顯示 Zero 2 W 鏡頭，並停止模擬魚群、感測器與設備排程。也可以用控制台上方的「關閉模擬」單獨切換資料來源。

即時影像用 `rpicam-vid` 擷取 640 × 360、目標 10 fps 的 JPEG 畫格。按停止、收合「實體相機」或離開頁面就會釋放相機，仍維持實體模式。按「拍攝一張」可取得 960 × 540 JPEG。需要展示時可手動按「開啟模擬」。

即時追蹤使用系統的 OpenCV 和 NumPy。將鏡頭固定在魚缸正面，讓魚缸填滿畫面，選擇追蹤 1 或 2 尾後按「啟動即時影像」。自動模式約 6 秒建立背景，再根據顏色或背景差異畫出魚隻 ID、位置與軌跡。可手動選「橘金色魚」立即用顏色辨識，或選「深色魚」用背景差異辨識。若鏡頭或照明改變，可重啟即時影像來重建背景。

`GET /api/state` 的 `camera_tracking` 提供即時追蹤狀態與像素座標。水溫、透光度以及使用公分的餵食分析仍由實體感測器與校正後的資料提供。

跨裝置上傳觀測前，確認 Pi 和上傳端的 UTC 時間一致。USB 離線環境要在重開機後確認 Pi 的時鐘。

## 單鏡頭影片測試

離線追蹤工具可處理固定鏡頭中一或兩尾顏色比背景深的魚，輸出原始畫面的像素座標。短暫遺失的軌跡會標為 `lost`，座標留空。命令列可調整觀測區、前景亮度差門檻和魚影大小。

從 MOTChallenge 的 ZebraFish-03 下載 `sequence` 影片和 `3DZeF20Lables.zip` 標註檔，先建立 `.data/replay` 目錄。下列參數將正面魚缸裁切為 560 × 250、15 fps 的 WebM。程式用前 6 秒建立背景，評估其餘 24 秒。預測點須落在人工標註的魚框或其向外 4 個原始畫面像素的範圍內。可見與遮擋標註分開計數。 [3D-ZeF20 資料來源](https://motchallenge.net/data/3D-ZeF20/)

```sh
uv run --no-project --with opencv-python-headless python tools/prepare_front_video.py ZebraFish-03-raw.mp4 .data/replay/front.webm --roi 100 200 1120 500
uv run --no-project --with opencv-python-headless python tools/track_fish_video.py .data/replay/front.webm .data/replay/tracks.csv
uv run --no-project python tools/evaluate_3d_zef.py .data/replay/tracks.csv 3DZeF20Lables.zip --source-stride 4 --transform 100 200 2 2
```

啟動控制台後，點選「播放測試影片」即可播放、暫停、拖曳時間軸和下載 CSV。回放和 CSV 座標都以裁切後影像的左上角為原點，單位是像素。在 Pi 上試播時，把準備好的兩個檔案放入 `~/bime01/.data/replay/`。

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
| `POST /api/ingest/observations` | 上傳魚缸正面的 2D 追蹤畫格 |
| `POST /api/ingest/telemetry` | 上傳水溫與透光度讀值 |
| `WS /ws/live` | 向控制台推送最新觀測、遙測和控制狀態 |
| `GET /api/history` | 查詢同一資料來源的歷史走勢和事件 |

啟用相機預覽可直接使用實體模式。若要使用兩個上傳端點，還需要設定 `AQUARIUM_INGEST_TOKEN`，並使用 `Authorization: Bearer <token>`，成功回傳 HTTP 204。每個串流的 `timestamp_ms` 使用 Unix 毫秒時間，須遞增且接近伺服器時間。同一串流由單一上游負責排序。以下是相機畫格與遙測的 JSON 結構，送出前用當下的時間戳替換範例值。

```json
{
  "timestamp_ms": 1794100000000,
  "tracks": [
    {"id": "fish-1", "status": "tracked", "position": {"x": 3.5, "y": 18}},
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

相機端將正面影像校正到魚缸平面後，再送入完整畫格。座標使用 cm，從魚缸左到右為 `x`（−40～40），由缸底向上為 `y`（0～40）。持續追蹤同一個體時保留 ID，暫時遮擋時傳 `lost`。遙測每筆代表最新完整快照，當下無讀值的欄位可用 `null`。模擬與硬體的歷史依資料來源分開查詢。

### 設備驅動

在 [aquarium/contracts.py](aquarium/contracts.py) 實作 `Actuator.execute(command)`，再用 `create_app(mode="hardware", ingest_token=..., actuator=...)` 注入驅動。控制台會沿用原本的餵食與燈光按鈕。

- 餵食命令為 `FeedCommand`，燈光命令為 `LightCommand`。以命令 ID 做裝置端去重，確認完成後再回報成功。無法確認時拋出 `DeviceUnavailable`。
- 目前硬體模式可直接接收觀測。餵食與燈光會等實體驅動注入才啟用，硬體自動排程需另行設定。水溫控制器與打氣機可依同一命令模式增加專用型別。
- 伺服器預設只監聽本機。跨裝置整合時，把 API 放在受保護的網路或反向代理後，並為操作端加上存取控制。
