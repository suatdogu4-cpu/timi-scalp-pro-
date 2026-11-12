
"""TiMi SCALP PRO v1.0.1 — Binance Spot relay and paper simulator.

Developer: Suat DOĞU
Public market data only. No real orders or exchange credentials.
"""
import asyncio
import json
import logging
import os
import time
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import websockets
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

logging.basicConfig(level=logging.INFO)
LOG = logging.getLogger("timi")

ROOT = Path(__file__).resolve().parent
VERSION = "1.0.1"

SYMBOLS = (
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT",
    "XRPUSDT", "DOGEUSDT", "ADAUSDT", "AVAXUSDT"
)

# Binance public market-data endpoint.
# This endpoint does not support private trading operations.
STREAM = "wss://data-stream.binance.vision/stream?streams="
REST = "https://data-api.binance.vision/api/v3"

FEE = 0.001
SLIPPAGE = 0.0002


class Market:
    def __init__(self, symbol):
        self.symbol = symbol
        self.price = None
        self.change = None
        self.volume = None
        self.high = None
        self.low = None
        self.bid = None
        self.ask = None
        self.bids = []
        self.asks = []
        self.trades = deque(maxlen=70)
        self.buy_qty = 0.0
        self.sell_qty = 0.0
        self.last_trade_ms = None
        self.last_book_ms = None
        self.last_ticker_ms = None
        self.source = "Binance Spot"
        self.sequence = 0

    def public(self):
        return {
            "symbol": self.symbol,
            "price": self.price,
            "change": self.change,
            "volume": self.volume,
            "high": self.high,
            "low": self.low,
            "bid": self.bid,
            "ask": self.ask,
            "bids": self.bids,
            "asks": self.asks,
            "trades": list(self.trades),
            "buy_qty": round(self.buy_qty, 5),
            "sell_qty": round(self.sell_qty, 5),
            "last_trade_ms": self.last_trade_ms,
            "last_book_ms": self.last_book_ms,
            "last_ticker_ms": self.last_ticker_ms,
            "source": self.source,
            "sequence": self.sequence,
        }


markets = {s: Market(s) for s in SYMBOLS}
clients = []

connection_status = {
    "connected": False,
    "last_message_ms": None,
    "error": None,
    "endpoint": "data-stream.binance.vision",
}

state_lock = asyncio.Lock()

paper_cash = 10000.0
paper_positions = {s: 0.0 for s in SYMBOLS}
paper_history = deque(maxlen=200)


def now_ms():
    return int(time.time() * 1000)


async def bootstrap():
    """Load initial 24-hour statistics through REST."""
    async with httpx.AsyncClient(timeout=12) as client:
        for symbol in SYMBOLS:
            try:
                response = await client.get(
                    f"{REST}/ticker/24hr",
                    params={"symbol": symbol},
                )
                response.raise_for_status()
                v = response.json()
                m = markets[symbol]

                m.price = float(v["lastPrice"])
                m.change = float(v["priceChangePercent"])
                m.volume = float(v["quoteVolume"])
                m.high = float(v["highPrice"])
                m.low = float(v["lowPrice"])
                m.last_ticker_ms = now_ms()

            except Exception as exc:
                LOG.warning(
                    "Bootstrap %s failed: %s",
                    symbol,
                    exc,
                )


async def broadcast(symbol, event):
    if not clients:
        return

    data = json.dumps(event, separators=(",", ":"))

    for client in tuple(clients):
        if client["symbol"] != symbol:
            continue

        queue = client["queue"]

        try:
            queue.put_nowait(data)
        except asyncio.QueueFull:
            try:
                queue.get_nowait()
                queue.put_nowait(data)
            except (asyncio.QueueEmpty, asyncio.QueueFull):
                pass


async def relay():
    """Connect to Binance public Spot WebSocket streams."""
    names = []

    for symbol in SYMBOLS:
        key = symbol.lower()
        names.extend((
            f"{key}@trade",
            f"{key}@depth20@100ms",
            f"{key}@ticker",
        ))

    url = STREAM + "/".join(names)
    delay = 1

    while True:
        try:
            LOG.info(
                "Connecting to Binance public stream: %s",
                connection_status["endpoint"],
            )

            async with websockets.connect(
                url,
                ping_interval=20,
                ping_timeout=20,
                max_size=4_000_000,
                open_timeout=15,
            ) as ws:

                connection_status.update(
                    connected=True,
                    error=None,
                )

                LOG.info("Binance public WebSocket connected")
                delay = 1

                async for raw in ws:
                    msg = json.loads(raw)
                    stream = msg.get("stream", "")
                    v = msg.get("data", {})

                    symbol = v.get("s", "").upper()

                    # Partial depth stream payloads may not
                    # contain a symbol. Recover it from stream.
                    if symbol not in markets:
                        symbol = stream.split("@")[0].upper()

                    if symbol not in markets:
                        continue

                    m = markets[symbol]
                    now = now_ms()

                    connection_status["last_message_ms"] = now

                    if "@trade" in stream:
                        price = float(v["p"])
                        qty = float(v["q"])

                        side = "sell" if v["m"] else "buy"

                        m.price = price
                        m.last_trade_ms = now

                        trade = {
                            "price": price,
                            "qty": qty,
                            "side": side,
                            "ts": v.get("T", now),
                            "id": v.get("t"),
                        }

                        m.trades.appendleft(trade)

                        if side == "buy":
                            m.buy_qty += qty
                        else:
                            m.sell_qty += qty

                        event = {
                            "type": "trade",
                            "symbol": symbol,
                            "trade": trade,
                            "price": price,
                            "ts": now,
                        }

                    elif "@depth20" in stream:
                        m.bids = [
                            [float(p), float(q)]
                            for p, q in v.get("bids", [])
                        ]

                        m.asks = [
                            [float(p), float(q)]
                            for p, q in v.get("asks", [])
                        ]

                        m.bid = (
                            m.bids[0][0] if m.bids else None
                        )
                        m.ask = (
                            m.asks[0][0] if m.asks else None
                        )

                        m.last_book_ms = now

                        event = {
                            "type": "depth",
                            "symbol": symbol,
                            "bids": m.bids,
                            "asks": m.asks,
                            "bid": m.bid,
                            "ask": m.ask,
                            "ts": now,
                        }

                    elif "@ticker" in stream:
                        m.change = float(v["P"])
                        m.volume = float(v["q"])
                        m.high = float(v["h"])
                        m.low = float(v["l"])
                        m.last_ticker_ms = now

                        event = {
                            "type": "ticker",
                            "symbol": symbol,
                            "price": m.price,
                            "change": m.change,
                            "volume": m.volume,
                            "high": m.high,
                            "low": m.low,
                            "ts": now,
                        }

                    else:
                        continue

                    m.sequence += 1
                    await broadcast(symbol, event)

        except asyncio.CancelledError:
            raise

        except Exception as exc:
            connection_status.update(
                connected=False,
                error=str(exc),
            )

            LOG.warning(
                "Binance public stream disconnected: %s; "
                "retry in %ss",
                exc,
                delay,
            )

            await asyncio.sleep(delay)
            delay = min(30, delay * 2)

        finally:
            connection_status["connected"] = False


@asynccontextmanager
async def lifespan(app):
    await bootstrap()
    task = asyncio.create_task(relay())

    try:
        yield
    finally:
        task.cancel()
        await asyncio.gather(
            task,
            return_exceptions=True,
        )


app = FastAPI(
    title="TiMi SCALP PRO",
    version=VERSION,
    lifespan=lifespan,
)

origins = [
    x.strip()
    for x in os.getenv("TIMI_ORIGINS", "").split(",")
    if x.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


@app.get("/")
async def index():
    return FileResponse(ROOT / "index.html")


@app.get("/api/health")
async def health():
    age = None

    if connection_status["last_message_ms"]:
        age = (
            now_ms() -
            connection_status["last_message_ms"]
        )

    return {
        "app": "TiMi SCALP PRO",
        "version": VERSION,
        "upstream_connected": connection_status["connected"],
        "last_message_age_ms": age,
        "upstream_endpoint": connection_status["endpoint"],
        "upstream_error": connection_status["error"],
        "symbols": SYMBOLS,
        "real_orders_enabled": False,
    }


@app.get("/api/markets")
async def market_list():
    return {
        "markets": [
            {
                k: v
                for k, v in m.public().items()
                if k not in ("bids", "asks", "trades")
            }
            for m in markets.values()
        ]
    }


@app.get("/api/snapshot/{symbol}")
async def snapshot(symbol: str):
    symbol = symbol.upper()

    if symbol not in markets:
        raise HTTPException(
            404,
            "Unsupported symbol",
        )

    return markets[symbol].public()


@app.get("/api/klines/{symbol}")
async def klines(
    symbol: str,
    interval: str = "1m",
    limit: int = 150,
):
    symbol = symbol.upper()

    intervals = (
        "1m", "3m", "5m", "15m",
        "30m", "1h", "4h", "1d",
    )

    if symbol not in markets or interval not in intervals:
        raise HTTPException(
            400,
            "Invalid symbol or interval",
        )

    async with httpx.AsyncClient(timeout=12) as client:
        try:
            r = await client.get(
                f"{REST}/klines",
                params={
                    "symbol": symbol,
                    "interval": interval,
                    "limit": max(20, min(limit, 300)),
                },
            )

            r.raise_for_status()

            current = now_ms()

            candles = [
                {
                    "t": x[0],
                    "o": float(x[1]),
                    "h": float(x[2]),
                    "l": float(x[3]),
                    "c": float(x[4]),
                    "v": float(x[5]),
                    "closed": x[6] < current,
                }
                for x in r.json()
            ]

            return {
                "symbol": symbol,
                "interval": interval,
                "source": "Binance Spot REST",
                "candles": candles,
            }

        except httpx.HTTPError:
            raise HTTPException(
                503,
                "Binance candle service unavailable",
            )


@app.websocket("/ws/{symbol}")
async def live(ws: WebSocket, symbol: str):
    symbol = symbol.upper()

    if symbol not in markets:
        await ws.close(code=1008)
        return

    await ws.accept()

    item = {
        "symbol": symbol,
        "queue": asyncio.Queue(maxsize=120),
    }

    clients.append(item)

    try:
        await ws.send_json({
            "type": "snapshot",
            "data": markets[symbol].public(),
        })

        while True:
            try:
                data = await asyncio.wait_for(
                    item["queue"].get(),
                    timeout=10,
                )

                await ws.send_text(data)

            except asyncio.TimeoutError:
                await ws.send_json({
                    "type": "heartbeat",
                    "ts": now_ms(),
                    "upstream_connected": (
                        connection_status["connected"]
                    ),
                })

    except (
        WebSocketDisconnect,
        RuntimeError,
    ):
        pass

    finally:
        if item in clients:
            clients.remove(item)


class PaperOrder(BaseModel):
    symbol: str
    side: str
    quantity: float = Field(gt=0, le=100000)


@app.get("/api/paper")
async def paper():
    return {
        "cash": round(paper_cash, 2),
        "positions": paper_positions,
        "history": list(paper_history),
        "fee_rate": FEE,
        "slippage_rate": SLIPPAGE,
        "mode": "simulation_only",
    }


@app.post("/api/paper/order")
async def paper_order(order: PaperOrder):
    global paper_cash

    symbol = order.symbol.upper()

    if (
        symbol not in markets
        or order.side not in ("buy", "sell")
    ):
        raise HTTPException(
            400,
            "Invalid symbol or side",
        )

    m = markets[symbol]
    now = now_ms()

    if (
        not connection_status["connected"]
        or m.price is None
        or not m.last_trade_ms
        or now - m.last_trade_ms > 5000
    ):
        raise HTTPException(
            409,
            "No fresh trade price; paper execution blocked",
        )

    async with state_lock:
        px = m.price * (
            1 + SLIPPAGE
            if order.side == "buy"
            else 1 - SLIPPAGE
        )

        notional = px * order.quantity
        fee = notional * FEE

        if order.side == "buy":
            if notional + fee > paper_cash:
                raise HTTPException(
                    409,
                    "Insufficient paper cash",
                )

            paper_cash -= notional + fee
            paper_positions[symbol] += order.quantity

        else:
            if (
                order.quantity >
                paper_positions[symbol] + 1e-10
            ):
                raise HTTPException(
                    409,
                    "Insufficient paper position; "
                    "spot short disabled",
                )

            paper_cash += notional - fee

            paper_positions[symbol] = max(
                0,
                paper_positions[symbol] - order.quantity,
            )

        record = {
            "symbol": symbol,
            "side": order.side,
            "quantity": order.quantity,
            "price": round(px, 8),
            "fee": round(fee, 6),
            "ts": now,
            "simulated": True,
        }

        paper_history.appendleft(record)

        return {
            "order": record,
            "cash": round(paper_cash, 2),
            "position": paper_positions[symbol],
        }


@app.post("/api/paper/reset")
async def reset_paper():
    global paper_cash

    async with state_lock:
        paper_cash = 10000.0

        for symbol in SYMBOLS:
            paper_positions[symbol] = 0.0

        paper_history.clear()

    return {
        "ok": True,
        "mode": "simulation_only",
    }


# SECURITY:
# No exchange API keys.
# No order-signing functionality.
# No real exchange order endpoints.
# All trades remain simulated.
