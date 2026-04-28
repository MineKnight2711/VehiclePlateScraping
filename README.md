# Autobis Traffic Fine Scraper Backend

Backend fallback API cho tính năng tra cứu phạt nguội trong app Flutter.
App vẫn gọi eHub/Autobis trước; backend này chỉ dùng khi eHub trả về `data`
rỗng hoặc không có dữ liệu.

Provider order:

1. Fresh local cache.
2. `api.phatnguoi.vn` public endpoint.
3. Optional HCMC CSGT endpoint when `HCMC_CAPTCHA_SOLVER_URL` is configured.
4. CSGT national page flow.
5. Stale cache if every live provider is blocked.

## Local run

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
playwright install chromium
copy .env.example .env
python run_server.py
```

Check nhanh:

```powershell
curl http://127.0.0.1:8000/health
```

## Deploy backend ra URL public (Render/Railway/Fly)

Mục tiêu là có URL dạng:

- `https://xxx.onrender.com`
- `https://xxx.up.railway.app`
- `https://xxx.fly.dev`

Backend đã hỗ trợ biến `PORT` do platform cấp sẵn, nên chỉ cần start command:

```bash
python run_server.py
```

### Render (khuyên dùng nhanh nhất)

Repo đã có `render.yaml` ở root.

1. Push code lên GitHub.
2. Vào Render, chọn Blueprint deploy từ repo.
3. Render sẽ tạo service từ `render.yaml` (rootDir=`backend`).
4. Sau khi deploy xong, kiểm tra: `https://<service>.onrender.com/health`.

### Railway

1. Tạo service từ repo này.
2. Set `Root Directory` = `backend`.
3. Build command: `pip install -r requirements.txt`.
4. Start command: `python run_server.py`.
5. Verify `https://<service>.up.railway.app/health`.

### Fly.io

Repo đã có `backend/Dockerfile`.

```bash
cd backend
fly launch --no-deploy
fly deploy
```

Sau deploy, kiểm tra `https://<app-name>.fly.dev/health`.

## Env khuyến nghị cho free tier

Để giảm rủi ro timeout/tài nguyên, ưu tiên provider public:

```env
SCRAPER_ENABLE_PHATNGUOI=true
SCRAPER_ENABLE_CSGT=false
SCRAPER_ENABLE_HCMC=false
```

Nếu cần bật CSGT page flow thật (`SCRAPER_ENABLE_CSGT=true`), hãy đảm bảo môi
trường deploy có Playwright + Chromium phù hợp.

## Build Flutter với URL backend public

Dùng trực tiếp URL deploy khi build:

```bash
flutter build apk --release \
  --dart-define=TRAFFIC_SCRAPER_BASE_URL=https://xxx.onrender.com
```

Nếu muốn fallback nhiều backend:

```bash
flutter build apk --release \
  --dart-define=TRAFFIC_SCRAPER_BASE_URL=https://xxx.onrender.com \
  --dart-define=TRAFFIC_SCRAPER_FALLBACK_URLS=https://yyy.up.railway.app,https://zzz.fly.dev
```

## Endpoint

`POST /api/traffic-fines/check`

```json
{
  "license_plate": "30A12345",
  "vehicle_type": "car",
  "force_refresh": false
}
```

`force_refresh` là tuỳ chọn. Khi đặt `true`, backend sẽ bỏ qua cache và gọi live provider.

Response shape:

```json
{
  "error": 0,
  "message": "Tra cuu thanh cong",
  "data": [],
  "source": "csgt_scraper"
}
```
