# Aquarium Lab 魚缸行為監測

用 Raspberry Pi Zero 2 W + Pi Camera 3 從魚缸正面拍攝，持續追蹤 1–2 尾金魚的 2D 位置，記錄游速、活動比例、游動高度與停留位置，並在相機斷線或活動量明顯下降時發出警示。控制台透過 USB / SSH 通道在筆電上開啟。

影像來源可以在控制台切換，三種來源走完全相同的偵測、統計與警示流程：

- **Pi Camera 3**：在 Pi 上按「啟動相機」，開始後在背景持續記錄。
- **測試影片**：把錄好的影片當成相機即時播放（循環），用來在沒有魚缸時測試計算。可以放進 `.data/videos/` 或在控制台上傳。
- **假相機**：程式畫出的金魚畫面，可模擬關燈、魚變遲緩、相機斷線，用來展示警示。

## 架構

```
影像來源 (PiCamera / VideoFile / FakeTank) ← Hub 負責切換
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
| [aquarium/hub.py](aquarium/hub.py) | 可用來源清單、執行中切換、記住上次選擇 |
| [aquarium/devices.py](aquarium/devices.py) | 餵食器、燈光、溫度計介面 |
| [aquarium/settings.py](aquarium/settings.py) | 可在控制台修改的設定（存成 `settings.json`） |

每個來源的資料分開放在 `.data/sources/<來源>/`（`aquarium.sqlite3`、`settings.json`、`snapshots/`），各自有自己的校正，測試影片不會混進實機歷史。上次選的來源記在 `.data/active-source.txt`，重開機後沿用。

## 本機啟動（測試影片 / 假相機）

需要 Python 3.11+、uv、Node.js 20.19+ 或 22.12+。

```powershell
npm.cmd --prefix frontend ci
npm.cmd --prefix frontend run build
uv sync --locked
uv run --locked python -m uvicorn aquarium.main:app --host 127.0.0.1 --port 8000
```

開啟 <http://127.0.0.1:8000>，在「影像來源」選影片或假相機。

### 測試影片

`.data/videos/` 裡的影片（mp4、webm、mov、avi、mkv）都會出現在來源清單，播放時縮到 640 寬、最高約 10 fps，跟 Pi 相機的畫面規格接近。目前準備的兩支：

| 檔案 | 內容 |
| --- | --- |
| `goldfish-white.mp4` | 白底單尾金魚（YouTube `05biIPNBLWA`，21 秒），顏色辨識 |
| `zebrafish-3dzef.webm` | 原 MVP 的 3D-ZeF20 斑馬魚正面裁切片，背景差異辨識 |

金魚片可用 `uvx yt-dlp -f "bv*[height<=720][vcodec^=avc1]" -o .data/videos/goldfish-white.mp4 https://youtu.be/05biIPNBLWA` 下載。影片旁可放同名 `.json` 設定標籤、說明和預設設定（魚數、辨識方式、校正），例如：

```json
{"label": "影片：金魚", "note": "說明文字", "settings": {"fish_count": 1, "detection": "color"}}
```

沒有 `.json` 時預設 1 尾、顏色辨識、未校正（只算魚數，不算游速）；在控制台校正後就會開始計算。

### 假相機

第一次選假相機會補上 3 天的示範歷史，讓趨勢圖與「活動量偏低」警示有比較基準。右側「模擬情境」可以關燈、讓魚變遲緩、拔除相機 45 秒。假相機的活動量比較區間預設為 5 分鐘，方便展示。

改前端時可用 `npm.cmd --prefix frontend run dev`，它會把 `/api` 和 `/ws` 轉到 8000 埠。macOS / Linux 用 `npm`。

測試：`uv run --locked python -m unittest discover -s tests -v`

## 部署到 Raspberry Pi Zero 2 W

已在 Pi Zero 2 W（Raspbian Bookworm 32 位元、Python 3.11、系統 OpenCV 4.6、Camera 3 imx708）實測：相機約 10 fps，Python 約占一個核心的 90%（四核總量約 22%）、記憶體約 130 MB；Pi 上播放金魚測試片約 9 fps、斑馬魚片 7.5 fps。

### 平常更新

在開發電腦（Git Bash）執行，會建置前端、把程式和 `.data/videos/` 的測試影片傳到 Pi 的 `~/bime01`，再重新啟動：

```sh
bash tools/deploy_pi.sh
```

之後在筆電另開終端機執行下面的指令並保持連線，開 <http://127.0.0.1:8000>，在「影像來源」選「Pi Camera 3（實機）」按「啟動相機」，或選測試影片按「播放影片」。

```sh
ssh -N -L 8000:127.0.0.1:8000 kelsier@raspberrypi.local
```

### 第一次設定

1. **SSH 金鑰登入**（部署腳本需要）：在筆電執行 `ssh-keygen -t ed25519`，再把公鑰加到 Pi：

   ```powershell
   type $env:USERPROFILE\.ssh\id_ed25519.pub | ssh kelsier@raspberrypi.local "mkdir -p ~/.ssh && chmod 700 ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys"
   ```

2. **Python 環境**：Pi 使用系統的 OpenCV 與 NumPy（`sudo apt install python3-opencv python3-numpy`）。目前 Pi 上的 `~/bime01/.venv` 已有 FastAPI、uvicorn、pydantic、websockets；讓它看得到系統的 OpenCV：

   ```sh
   echo /usr/lib/python3/dist-packages > ~/bime01/.venv/lib/python3.11/site-packages/system-dist-packages.pth
   ```

   要重建環境時：有網路就 `uv venv --system-site-packages --python /usr/bin/python3 && uv sync --locked --no-default-groups`。Pi 只有 USB 沒有網路時，在開發電腦先下載套件，把 `requirements-pi.txt` 和 `wheelhouse/` 傳過去，在 Pi 用 `uv pip sync --offline --no-index --find-links wheelhouse --require-hashes requirements-pi.txt` 安裝：

   ```powershell
   uv export --locked --no-default-groups --format requirements-txt --no-emit-project --output-file requirements-pi.txt
   python -m pip download --require-hashes --only-binary=:all: --platform manylinux_2_31_armv7l --platform manylinux_2_17_armv7l --platform manylinux2014_armv7l --python-version 3.11 --abi cp311 --implementation cp --dest wheelhouse -r requirements-pi.txt
   ```

3. **開機自動執行**（長期記錄需要，否則 Pi 重開機後要重跑部署腳本）：

   ```sh
   sudo cp ~/bime01/deploy/aquarium.service /etc/systemd/system/
   sudo systemctl daemon-reload && sudo systemctl enable --now aquarium
   journalctl -u aquarium -f
   ```

   啟用後部署腳本會改用 `systemctl restart`。

**關機**：控制台「設定」最下面有「關閉 Raspberry Pi」。按下並確認後，等綠燈熄滅再拔電源；直接拔電可能損壞 SD 卡。關機後程式不會自己回來，重新插電開機後再執行一次 `tools/deploy_pi.sh`。

**時鐘**：Pi Zero 沒有電池時鐘，只接 USB 時也無法自動對時，重開機後時間常常會錯，歷史資料的時間軸就會跟著錯。控制台偵測到 Pi 與筆電相差超過 2 分鐘時會顯示「時鐘不準」，按「用這台電腦的時間校正」即可（用 `sudo date` 設定，需要免密碼 sudo，Raspberry Pi OS 預設就是）。

**對焦**：Camera 3 預設連續自動對焦。魚缸距離固定時，可在 service 加 `Environment=AQUARIUM_RPICAM_ARGS=--autofocus-mode manual --lens-position 3` 固定焦距（數值約為 1 / 距離公尺數）。

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
| `GET /api/sources`、`PUT /api/source` | 列出 / 切換影像來源（`{"id": "picamera"}`） |
| `POST /api/shutdown` | 關閉 Raspberry Pi（`sudo shutdown -h now`，需要免密碼 sudo） |
| `POST /api/clock` | 用控制台電腦的時間校正 Pi 系統時鐘（相差超過 30 秒才會調整） |
| `PUT /api/videos/{檔名}` | 上傳測試影片（請求內容就是影片檔） |
| `POST /api/demo` | 假相機情境：`dark`、`sluggish`、`offline`、`reset` |

伺服器只監聽本機，請透過 SSH 通道連線。寫入類 API 會拒絕跨來源請求。互動文件在 `/docs`。
