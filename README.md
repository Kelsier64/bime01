# Aquarium Lab 魚缸行為監測

用 Raspberry Pi Zero 2 W + Pi Camera 3 從魚缸正面拍攝，持續追蹤 1–2 尾金魚的 2D 位置，記錄游速、活動比例、游動高度與停留位置，並在相機斷線或活動量明顯下降時發出警示。控制台透過 USB / SSH 通道在筆電上開啟。

沒有硬體時可用「假相機」：它會畫出金魚畫面，走跟實機完全相同的偵測、統計與警示流程，用來開發和展示。

## 架構

```
相機 (PiCamera / FakeTank)
  └─ Monitor（伺服器啟動就在背景常駐，不需要開網頁）
       ├─ vision      顏色或背景差異偵測 → 多目標追蹤（像素座標）
       ├─ calibration 四角校正 → 魚缸公分座標（x 從左壁起算，y 為離缸底高度）
       ├─ recorder    每秒一筆位置與游速；每分鐘彙總（游速、活動、高度、位置熱圖）
       ├─ alerts      相機斷線、活動量偏低 → 事件紀錄 + 快照
       └─ feeder      目前為手動餵食紀錄；接上餵食器後換成實體驅動
  └─ FastAPI：/ws/live 推送狀態與畫面，/api/* 查詢歷史與操作
```

| 檔案 | 內容 |
| --- | --- |
| [aquarium/monitor.py](aquarium/monitor.py) | 常駐管線、相機重連、看門狗、資料保存期限 |
| [aquarium/vision.py](aquarium/vision.py) | 偵測與追蹤 |
| [aquarium/calibration.py](aquarium/calibration.py) | 像素 → 公分透視轉換 |
| [aquarium/recorder.py](aquarium/recorder.py)、[aquarium/store.py](aquarium/store.py) | 秒級與分鐘級統計、SQLite |
| [aquarium/alerts.py](aquarium/alerts.py) | 警示規則 |
| [aquarium/sources.py](aquarium/sources.py) | Pi Camera 3（`rpicam-vid` MJPEG） |
| [aquarium/fake.py](aquarium/fake.py) | 假相機與示範用歷史資料 |
| [aquarium/devices.py](aquarium/devices.py) | 餵食器、燈光、溫度計介面 |
| [aquarium/settings.py](aquarium/settings.py) | 可在控制台修改的設定（存成 `settings.json`） |

資料放在 `.data/<來源>/`：`aquarium.sqlite3`、`settings.json`、`snapshots/`。假相機與實機的資料分開存放，不會混在一起。

## 本機啟動（假相機）

需要 Python 3.11+、uv、Node.js 20.19+ 或 22.12+。

```powershell
npm.cmd --prefix frontend ci
npm.cmd --prefix frontend run build
uv sync --locked
uv run --locked python -m uvicorn aquarium.main:app --host 127.0.0.1 --port 8000
```

開啟 <http://127.0.0.1:8000>。第一次啟動會補上 3 天的示範歷史，讓趨勢圖與「活動量偏低」警示有比較基準。右側「模擬情境」可以關燈、讓魚變遲緩、拔除相機 45 秒。假相機的活動量比較區間預設為 5 分鐘，方便展示。

改前端時可用 `npm.cmd --prefix frontend run dev`，它會把 `/api` 和 `/ws` 轉到 8000 埠。macOS / Linux 用 `npm`。

測試：`uv run --locked python -m unittest discover -s tests -v`

## 部署到 Raspberry Pi Zero 2 W

Pi 使用系統的 OpenCV 與 NumPy（`python3-opencv`、`python3-numpy`，Raspberry Pi OS Bookworm 已有 `rpicam-apps`）。

1. 在開發電腦建置前端，將 `aquarium/`、`frontend/dist/`、`deploy/`、`pyproject.toml`、`uv.lock` 傳到 Pi 的 `~/bime01`。
2. 在 Pi 上安裝：

   ```sh
   sudo apt install python3-opencv python3-numpy
   cd ~/bime01
   uv venv --system-site-packages --python /usr/bin/python3 --no-python-downloads
   uv sync --locked --no-default-groups
   rpicam-hello --list-cameras
   ```

   Pi 沒有網路時，在開發電腦先下載套件（以 32 位元 Bookworm、Python 3.11 為例），把 `requirements-pi.txt` 和 `wheelhouse/` 一起傳過去，改用 `uv pip sync --offline --no-index --find-links wheelhouse --require-hashes requirements-pi.txt` 安裝：

   ```powershell
   uv export --locked --no-default-groups --format requirements-txt --no-emit-project --output-file requirements-pi.txt
   python -m pip download --require-hashes --only-binary=:all: --platform manylinux_2_31_armv7l --platform manylinux_2_17_armv7l --platform manylinux2014_armv7l --python-version 3.11 --abi cp311 --implementation cp --dest wheelhouse -r requirements-pi.txt
   ```

3. 設成開機自動執行（長期記錄需要）：編輯 [deploy/aquarium.service](deploy/aquarium.service) 的使用者與路徑後

   ```sh
   sudo cp deploy/aquarium.service /etc/systemd/system/
   sudo systemctl daemon-reload && sudo systemctl enable --now aquarium
   journalctl -u aquarium -f
   ```

4. 在筆電執行 `ssh -N -L 8000:127.0.0.1:8000 kelsier@raspberrypi.local`，再開 <http://127.0.0.1:8000>。

**時鐘**：Pi Zero 沒有電池時鐘。只接 USB 又沒有網路時，重開機後時間可能錯誤，歷史資料的時間軸就會錯。重開機後先確認 `date`，必要時從筆電同步：`ssh kelsier@raspberrypi.local "sudo date -u -s '$(date -u +%Y-%m-%dT%H:%M:%SZ)'"`。

**對焦**：Camera 3 預設連續自動對焦。魚缸距離固定時，可用 `Environment=AQUARIUM_RPICAM_ARGS=--autofocus-mode manual --lens-position 3` 固定焦距（數值依實際距離調整）。

## 第一次使用

1. 把鏡頭固定在魚缸正面，讓水體盡量填滿畫面，背景越單純越好。
2. 按「校正魚缸範圍」，依序點選水體的左上、右上、右下、左下角，輸入魚缸內寬與水深（cm）後儲存。沒校正前只會記錄魚數，不計算游速與位置。鏡頭移動過就要重新校正。
3. 在「設定」填入魚數。金魚用「顏色」辨識；深色魚或紅外線拍攝改用「背景差異」（需固定鏡頭約 6 秒建立背景）。
4. 「光線不足門檻」決定何時視為關燈。設定視窗會顯示目前畫面亮度，關燈後看一次亮度，把門檻設在開燈與關燈亮度之間。

## 統計與警示的定義

- **游速**：每尾魚每秒最後位置的位移（cm/s），只在連續兩秒都追蹤到同一尾魚時計算。
- **活動比例**：游速大於 1 cm/s 的魚秒比例。
- **平均高度**：魚離缸底的高度，餵食後通常會上升。
- **停留位置分布**：魚缸正面切成 12 × 6 格，累計每格的魚秒數。
- **關燈時段**：畫面平均亮度低於門檻時暫停追蹤，不計入任何統計。
- **餵食反應**：比較餵食紀錄前後各 N 分鐘（預設 5）的游速、高度與活動比例。
- **相機斷線**：超過設定秒數（預設 30）沒有收到畫面。相機會自動重連，恢復後警示解除。
- **活動量偏低**：最近 60 分鐘有光時段的平均游速，低於過去 3 天平均的 50%。需要至少 3 小時的基準資料，恢復到門檻再加 15% 才解除。

秒級資料保留 7 天，分鐘彙總永久保留，快照保留最新 500 張。

## 硬體擴充

[aquarium/devices.py](aquarium/devices.py) 定義了 `Feeder`、`Light`、`Thermometer` 介面：

- **餵食器**：實作 `Feeder`（`kind = "device"`，`feed()` 在裝置確認後回傳，失敗時拋出 `DeviceError`），傳給 `create_app(feeder=...)`。控制台按鈕會變成「立即餵食」，事件與餵食反應分析沿用。
- **燈光、水溫**：目前只有介面，接上硬體時再接到控制台。
- **ESP32-CAM / 3D**：第一版只做正面 2D。新增相機只要實作 [aquarium/sources.py](aquarium/sources.py) 的 `FrameSource`（例如讀 ESP32-CAM 的 MJPEG 串流）。舊的 Codex 3D 模擬版本留在本機分支 `codex/aquarium-3d`。

## API

| 介面 | 用途 |
| --- | --- |
| `WS /ws/live` | 狀態 JSON，接著是對應的 JPEG 畫格（`?video=false` 只收狀態） |
| `GET /api/state` | 目前狀態 |
| `GET /api/frame.jpg` | 最新畫格 |
| `GET /api/history?range=1h\|24h\|7d\|30d` | 趨勢、摘要、熱圖、事件 |
| `GET /api/feeding` | 最近一次餵食的前後比較 |
| `POST /api/feed` | 記錄餵食（或驅動餵食器） |
| `GET/PUT /api/settings` | 設定與校正 |
| `GET /api/events` | 事件紀錄 |
| `POST /api/demo` | 假相機情境：`dark`、`sluggish`、`offline`、`reset` |

伺服器只監聽本機，請透過 SSH 通道連線。寫入類 API 會拒絕跨來源請求。互動文件在 `/docs`。
