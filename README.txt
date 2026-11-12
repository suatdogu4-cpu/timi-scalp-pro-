TiMi SCALP PRO v1.0 — Suat DOĞU

1) Upload main.py, index.html, requirements.txt to the same GitHub repository.
2) Render > Web Service > Connect repository.
3) Build Command: pip install -r requirements.txt
4) Start Command: uvicorn main:app --host 0.0.0.0 --port $PORT
5) Open https://YOUR-SERVICE.onrender.com/ in Chrome.
6) Test https://YOUR-SERVICE.onrender.com/api/health
7) No exchange API key is needed. No real orders are possible.

If serving index.html from GitHub Pages instead of Render:
- Enter your Render https://...onrender.com URL in Menu > Gateway.
- Set Render environment variable TIMI_ORIGINS to the exact GitHub Pages origin,
  e.g. https://username.github.io (not including repository path).
- WebSocket endpoint must be reachable from your browser/network.

Important limits:
- Paper balance is shared among all visitors and resets when the service restarts.
  DO NOT expose paper trading to multiple users; production requires auth and per-user DB.
- Binance stream accessibility depends on server region and network restrictions.
- UI and feed freshness are checked; a live WebSocket connection alone does not guarantee live data.
- First 20 levels are partial depth, not a full synchronized L2 book.
- All 20 strategy engines are NOT implemented in v1.0; only market-data foundations.
- This version does not compute liquidation maps, genuine order-flow CVD, or arbitrage execution.
