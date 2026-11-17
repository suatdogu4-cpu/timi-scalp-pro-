"""TiMi Blockchain OSINT v0.1 — public, read-only Bitcoin/Ethereum research gateway.

Developer: Suat DOĞU. No private keys, seed phrases, wallet signing, exchange orders,
transaction broadcasting, identity inference, or unverified ownership claims.
"""
from __future__ import annotations

import asyncio
import copy
import logging
import os
import re
import time
from collections import OrderedDict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent
APP_VERSION = "0.1.0"
LOG = logging.getLogger("timi.osint")
logging.basicConfig(level=logging.INFO)
# HTTPX otherwise logs complete URLs, including Blockscout API keys in query strings.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
BTC_SOURCE = "https://mempool.space/api"
EVM_CHAINS = {
    "ethereum": {"id": "1", "name": "Ethereum", "coin": "ETH", "api": "https://eth.blockscout.com/api/v2", "explorer": "https://eth.blockscout.com"},
    "base": {"id": "8453", "name": "Base", "coin": "ETH", "api": "https://base.blockscout.com/api/v2", "explorer": "https://base.blockscout.com"},
}
BTC_ADDR = re.compile(r"^(?:[13][a-km-zA-HJ-NP-Z1-9]{25,34}|bc1[ac-hj-np-z02-9]{11,87})$")
EVM_ADDR = re.compile(r"^0x[a-fA-F0-9]{40}$")
BTC_TX = re.compile(r"^[a-fA-F0-9]{64}$")
EVM_TX = re.compile(r"^0x[a-fA-F0-9]{64}$")
CACHE_TTL = int(os.environ.get("OSINT_CACHE_SECONDS", "35"))
CACHE_TTL = max(5, min(300, CACHE_TTL))
USER_AGENT = "TiMi-Blockchain-OSINT/0.1 (read-only; public data)"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def decimal_units(value: Any, places: int) -> str | None:
    if value is None or not isinstance(places, int) or not (0 <= places <= 36):
        return None
    try:
        with localcontext() as ctx:
            ctx.prec = 90
            d = Decimal(str(value)) / (Decimal(10) ** places)
            return format(d.normalize(), "f") if d else "0"
    except (ValueError, InvalidOperation, TypeError):
        return None


def integer(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def address_of(value: Any) -> str | None:
    if isinstance(value, dict):
        return value.get("hash") if isinstance(value.get("hash"), str) else None
    return value if isinstance(value, str) else None


def utc_from_unix(seconds: Any) -> str | None:
    try:
        return datetime.fromtimestamp(int(seconds), tz=timezone.utc).isoformat(timespec="seconds")
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def normalize_chain(chain: str) -> str:
    key = chain.lower().strip()
    if key not in ("bitcoin", *EVM_CHAINS):
        raise HTTPException(400, "Bu sürüm yalnızca Bitcoin, Ethereum ve Base ağlarını destekler.")
    return key


def validate_address(chain: str, address: str) -> str:
    if len(address) > 120:
        raise HTTPException(422, "Adres fazla uzun.")
    if chain == "bitcoin":
        if not BTC_ADDR.fullmatch(address):
            raise HTTPException(422, "Bitcoin ana ağ adresi biçimi geçersiz.")
        return address
    if not EVM_ADDR.fullmatch(address):
        raise HTTPException(422, "EVM adresi 0x ile başlayan 40 onaltılık karakter içermelidir.")
    return address


def validate_hash(chain: str, tx_hash: str) -> str:
    regex = BTC_TX if chain == "bitcoin" else EVM_TX
    if not regex.fullmatch(tx_hash):
        raise HTTPException(422, "İşlem kimliği biçimi geçersiz.")
    return tx_hash


def explorer_address(chain: str, address: str) -> str:
    if chain == "bitcoin":
        return f"https://mempool.space/address/{address}"
    return f"{EVM_CHAINS[chain]['explorer']}/address/{address}"


def explorer_tx(chain: str, hash_value: str) -> str:
    if chain == "bitcoin":
        return f"https://mempool.space/tx/{hash_value}"
    return f"{EVM_CHAINS[chain]['explorer']}/tx/{hash_value}"


# TiMi'nin mevcut lifespan / piyasa HTTP istemcisini değiştirmez.
# OSINT istekleri yalnızca OSINT uç noktaları üzerinden ve sınırlandırılmış şekilde çalışır.
router = APIRouter()
_OSINT_CACHE: OrderedDict = OrderedDict()
_OSINT_SEMAPHORE = asyncio.Semaphore(5)


def source_args(chain: str, endpoint: str) -> tuple[str, dict[str, str]]:
    if chain == "bitcoin":
        return f"{BTC_SOURCE}/{endpoint.lstrip('/')}", {}
    cfg = EVM_CHAINS[chain]
    api_key = os.getenv("BLOCKSCOUT_API_KEY", "").strip()
    if api_key:
        return f"https://api.blockscout.com/{cfg['id']}/api/v2/{endpoint.lstrip('/')}", {"apikey": api_key}
    return f"{cfg['api']}/{endpoint.lstrip('/')}", {}


async def get_json(request: Request, chain: str, endpoint: str, params: dict | None = None) -> Any:
    """Fixed host allowlist. All path values are already validated, never user-supplied URLs."""
    url, key_params = source_args(chain, endpoint)
    query = {**key_params, **(params or {})}
    cache_key = (chain, endpoint, tuple(sorted((str(k), str(v)) for k, v in query.items())))
    cache = _OSINT_CACHE
    now = time.monotonic()
    hit = cache.get(cache_key)
    if hit and now - hit[0] < CACHE_TTL:
        cache.move_to_end(cache_key)
        return copy.deepcopy(hit[1])
    try:
        async with _OSINT_SEMAPHORE:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(12.0, connect=5.0),
                headers={"User-Agent": USER_AGENT},
                follow_redirects=False,
            ) as client:
                response = await client.get(url, params=query)
        if response.status_code == 404:
            raise HTTPException(404, "Kaynakta bu adres veya işlem bulunamadı.")
        if response.status_code in (401, 403):
            raise HTTPException(503, "Veri kaynağı erişimi reddetti; Blockscout API anahtarı gerekebilir.")
        if response.status_code == 429:
            raise HTTPException(503, "Kaynağın istek kotası doldu. Kısa süre sonra yeniden deneyin.")
        if response.status_code >= 400:
            LOG.warning("Provider HTTP error chain=%s status=%s", chain, response.status_code)
            raise HTTPException(502, "Zincir veri kaynağı geçerli yanıt vermedi.")
        body = response.json()
        if not isinstance(body, (dict, list)):
            raise ValueError("unexpected provider response")
        cache[cache_key] = (now, copy.deepcopy(body))
        cache.move_to_end(cache_key)
        while len(cache) > 250:
            cache.popitem(last=False)
        return body
    except HTTPException:
        raise
    except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
        LOG.warning("Provider connection error chain=%s error=%s", chain, type(exc).__name__)
        raise HTTPException(503, "Zincir veri kaynağına bağlanılamadı veya zaman aşımına uğradı.") from exc
    except (ValueError, httpx.DecodingError) as exc:
        raise HTTPException(502, "Veri kaynağından bozuk yanıt alındı.") from exc


def btc_tx_to_record(tx: dict, address: str) -> dict:
    vin = tx.get("vin") or []
    vout = tx.get("vout") or []
    in_sum = sum(integer((i.get("prevout") or {}).get("value")) or 0
                 for i in vin if (i.get("prevout") or {}).get("scriptpubkey_address") == address)
    out_sum = sum(integer(o.get("value")) or 0 for o in vout
                  if o.get("scriptpubkey_address") == address)
    direction = "karma" if in_sum and out_sum else "çıkış" if in_sum else "giriş" if out_sum else "ilişkili"
    gross = in_sum if in_sum else out_sum
    status = tx.get("status") or {}
    txid = tx.get("txid", "")
    return {
        "hash": txid, "timestamp": utc_from_unix(status.get("block_time")),
        "confirmed": bool(status.get("confirmed")), "direction": direction,
        "amount": decimal_units(gross, 8), "unit": "BTC", "asset": "BTC",
        "type": "utxo", "explorer_url": explorer_tx("bitcoin", txid),
        "note": "Brüt adres girdi/çıktısı; karşı tarafa net ödeme veya sahiplik kanıtı değildir.",
        "block": status.get("block_height"),
    }


def evm_tx_to_record(chain: str, tx: dict, address: str) -> dict:
    sender = address_of(tx.get("from"))
    recipient = address_of(tx.get("to"))
    failed = str(tx.get("result") or tx.get("status") or "").lower() in ("error", "failed", "reverted")
    value = decimal_units(tx.get("value"), 18)
    direction = "öz işlem" if sender and recipient and sender.lower() == address.lower() == recipient.lower() else (
        "çıkış" if sender and sender.lower() == address.lower() else "giriş" if recipient and recipient.lower() == address.lower() else "ilişkili")
    hash_value = tx.get("hash", "")
    return {
        "hash": hash_value, "timestamp": tx.get("timestamp"),
        "confirmed": str(tx.get("status") or "").lower() not in ("pending", "awaiting_internal_transactions"),
        "failed": failed, "direction": direction, "amount": "0" if failed else value,
        "unit": EVM_CHAINS[chain]["coin"], "asset": EVM_CHAINS[chain]["coin"],
        "type": "native", "from": sender, "to": recipient,
        "explorer_url": explorer_tx(chain, hash_value), "block": tx.get("block"),
        "note": "Başarısız işlem: değer aktarılmadı." if failed else "İşlem değeri; ERC-20 olayları ayrı listelenir.",
    }


def token_transfer_to_record(chain: str, tr: dict, address: str) -> dict:
    token = tr.get("token") or {}
    total = tr.get("total") or {}
    if isinstance(total, dict):
        raw_value = total.get("value")
        dec = integer(total.get("decimals"))
    else:
        raw_value, dec = total, integer(token.get("decimals"))
    if dec is None:
        dec = integer(token.get("decimals"))
    amount = decimal_units(raw_value, dec) if dec is not None else None
    sender, recipient = address_of(tr.get("from")), address_of(tr.get("to"))
    direction = "öz işlem" if sender and recipient and sender.lower() == recipient.lower() == address.lower() else (
        "çıkış" if sender and sender.lower() == address.lower() else "giriş" if recipient and recipient.lower() == address.lower() else "ilişkili")
    txid = tr.get("transaction_hash") or tr.get("tx_hash") or ""
    return {
        "hash": txid, "timestamp": tr.get("timestamp"), "confirmed": True,
        "direction": direction, "amount": amount, "raw_amount": str(raw_value) if raw_value is not None else None,
        "unit": token.get("symbol") or "TOKEN", "asset": token.get("address_hash") or token.get("symbol"),
        "type": token.get("type") or "token", "from": sender, "to": recipient,
        "explorer_url": explorer_tx(chain, txid), "block": tr.get("block_hash"),
        "note": "Token olay kaydı; tokenın güvenilirliği veya ekonomik değeri doğrulanmış değildir.",
    }


async def wallet_info(request: Request, chain: str, address: str) -> dict:
    if chain == "bitcoin":
        results = await asyncio.gather(
            get_json(request, chain, f"address/{address}"),
            get_json(request, chain, f"address/{address}/txs"), return_exceptions=True)
        errors = [str(x.detail) for x in results if isinstance(x, HTTPException)]
        if all(isinstance(x, Exception) for x in results):
            raise HTTPException(503, "; ".join(errors) or "Kaynak kullanılamıyor")
        detail = results[0] if isinstance(results[0], dict) else {}
        txs = results[1] if isinstance(results[1], list) else []
        if not isinstance(results[0], dict) or not isinstance(detail.get("chain_stats"), dict):
            errors.append("Bitcoin hesap özeti eksik veya beklenmeyen biçimde.")
        if not isinstance(results[1], list):
            errors.append("Bitcoin işlem sayfası alınamadı.")
        chain_stats = detail.get("chain_stats") or {}
        mempool_stats = detail.get("mempool_stats") or {}
        funded, spent = integer(chain_stats.get("funded_txo_sum")), integer(chain_stats.get("spent_txo_sum"))
        return {
            "chain": chain, "address": address, "fetched_at": utc_now(), "source": "mempool.space Esplora",
            "source_url": "https://mempool.space/docs/api/rest", "explorer_url": explorer_address(chain, address),
            "status": "partial" if errors else "ok", "warnings": errors,
            "native_unit": "BTC", "balance_native": decimal_units(funded-spent, 8) if funded is not None and spent is not None else None,
            "balance_note": "Yalnızca onaylı UTXO bakiyesi; mempool etkisi dahil değil.",
            "received_native": decimal_units(funded, 8), "spent_native": decimal_units(spent, 8),
            "totals_note": "Birikimli UTXO fonlanan/harcanan çıktılar; net kişi-kişi para akışı değildir.",
            "tx_count": integer(chain_stats.get("tx_count")), "pending_tx_count": integer(mempool_stats.get("tx_count")),
            "transactions": [btc_tx_to_record(x, address) for x in txs if isinstance(x, dict)][:60],
            "token_transfers": [],
            "pagination": {"transactions_incomplete": (integer(chain_stats.get("tx_count")) or 0) > len(txs), "tokens_incomplete": False},
        }

    results = await asyncio.gather(
        get_json(request, chain, f"addresses/{address}"),
        get_json(request, chain, f"addresses/{address}/transactions"),
        get_json(request, chain, f"addresses/{address}/token-transfers"), return_exceptions=True)
    warnings = [str(x.detail) for x in results if isinstance(x, HTTPException)]
    if all(isinstance(x, Exception) for x in results):
        raise HTTPException(503, "; ".join(warnings) or "Kaynak kullanılamıyor")
    detail = results[0] if isinstance(results[0], dict) else {}
    tx_body = results[1] if isinstance(results[1], dict) else {}
    token_body = results[2] if isinstance(results[2], dict) else {}
    if "coin_balance" not in detail:
        warnings.append("EVM bakiye verisi eksik.")
    if not isinstance(tx_body.get("items"), list):
        warnings.append("Normal işlem sayfası alınamadı.")
    if not isinstance(token_body.get("items"), list):
        warnings.append("Token olay sayfası alınamadı.")
    tx_rows = tx_body.get("items") if isinstance(tx_body.get("items"), list) else []
    token_rows = token_body.get("items") if isinstance(token_body.get("items"), list) else []
    tx_count = integer(detail.get("transactions_count"))
    return {
        "chain": chain, "address": address, "fetched_at": utc_now(),
        "source": "Blockscout API v2", "source_url": "https://docs.blockscout.com/devs/apis",
        "explorer_url": explorer_address(chain, address), "status": "partial" if warnings else "ok", "warnings": warnings,
        "native_unit": "ETH", "balance_native": decimal_units(detail.get("coin_balance"), 18),
        "balance_note": "Explorer'ın bildirdiği güncel yerel varlık bakiyesi; token bakiyeleri dahil değil.",
        "received_native": None, "spent_native": None,
        "totals_note": "Bu sürümde tam geçmiş toplamı hesaplanmıyor.",
        "tx_count": tx_count, "pending_tx_count": None,
        "transactions": [evm_tx_to_record(chain, t, address) for t in tx_rows if isinstance(t, dict)][:50],
        "token_transfers": [token_transfer_to_record(chain, t, address) for t in token_rows if isinstance(t, dict)][:50],
        "pagination": {
            "transactions_incomplete": bool(tx_body.get("next_page_params")) or (tx_count is not None and tx_count > len(tx_rows)),
            "tokens_incomplete": bool(token_body.get("next_page_params")),
        },
    }


class GraphRequest(BaseModel):
    chain: str
    address: str = Field(min_length=15, max_length=120)
    limit: int = Field(default=8, ge=1, le=12)


def make_graph(data: dict, limit: int) -> dict:
    chain, address = data["chain"], data["address"]
    nodes = OrderedDict()
    edges: list[dict] = []
    focus_id = address.lower() if chain != "bitcoin" else address

    def add_node(key: str | None, kind: str = "address") -> str | None:
        if not key or not isinstance(key, str):
            return None
        id_ = key.lower() if chain != "bitcoin" and kind == "address" else key
        if len(nodes) >= 65 and id_ not in nodes:
            return None
        if id_ not in nodes:
            nodes[id_] = {"id": id_, "label": id_[:7] + "…" + id_[-5:] if len(id_) > 18 else id_,
                          "kind": "focus" if id_ == focus_id else kind, "full": key}
        return id_

    add_node(address)
    if chain == "bitcoin":
        txs = data.get("_graph_raw_txs") or []
        for tx in txs[:limit]:
            txid = tx.get("txid")
            tx_node = add_node(f"tx:{txid}", "transaction") if txid else None
            if not tx_node:
                continue
            ins = [v.get("prevout") or {} for v in tx.get("vin") or []]
            outs = tx.get("vout") or []
            ins = [x for x in ins if isinstance(x, dict) and x.get("scriptpubkey_address")]
            outs = [x for x in outs if isinstance(x, dict) and x.get("scriptpubkey_address")]
            # Keep the investigated address represented even if beyond first few legs.
            selected_in = [x for x in ins if x.get("scriptpubkey_address") == address][:2]
            selected_in += [x for x in ins if x.get("scriptpubkey_address") != address][:2]
            selected_out = [x for x in outs if x.get("scriptpubkey_address") == address][:2]
            selected_out += [x for x in outs if x.get("scriptpubkey_address") != address][:3]
            for x in selected_in:
                src = add_node(x.get("scriptpubkey_address"))
                if src:
                    edges.append({"source": src, "target": tx_node, "label": f"input {decimal_units(x.get('value'), 8) or '?'} BTC", "type": "utxo-input"})
            for x in selected_out:
                dst = add_node(x.get("scriptpubkey_address"))
                if dst:
                    edges.append({"source": tx_node, "target": dst, "label": f"output {decimal_units(x.get('value'), 8) or '?'} BTC", "type": "utxo-output"})
        note = "Bitcoin grafı girdileri işlem düğümüne, çıktıları işlem düğümünden adreslere bağlar. Adresten adrese doğrudan sahiplik veya ödeme atfetmez. Yalnızca örnek işlem ve bacaklar çizilir."
    else:
        items = (data.get("transactions") or [])[:limit] + (data.get("token_transfers") or [])[:limit]
        for t in items:
            if t.get("failed") or t.get("direction") == "öz işlem":
                continue
            a, b = add_node(t.get("from")), add_node(t.get("to"))
            if a and b and (a == focus_id or b == focus_id):
                amount = t.get("amount")
                label = f"{amount} {t.get('unit')}" if amount is not None else "token (tutar doğrulanamadı)"
                if t.get("type") == "native" and amount == "0":
                    label = "kontrat çağrısı / 0 ETH"
                edges.append({"source": a, "target": b, "label": label, "type": t.get("type"), "tx_hash": t.get("hash")})
        note = "EVM grafı yalnızca örnek normal işlemleri ve token olaylarını gösterir. Kontratın iç transferleri ve tüm geçmiş dahil değildir; bağlantı ortak mülkiyet kanıtı değildir."
    return {"chain": chain, "address": address, "fetched_at": utc_now(), "nodes": list(nodes.values()),
            "edges": edges[:100], "limited": True, "note": note, "source": data.get("source")}


@router.get("/osint/panel", include_in_schema=False)
async def osint_panel():
    return FileResponse(ROOT / "osint_panel.html", headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


@router.get("/api/osint/health")
async def health():
    return {"ok": True, "version": APP_VERSION, "time_utc": utc_now(),
            "supported_chains": ["bitcoin", "ethereum", "base"],
            "key_configured": bool(os.getenv("BLOCKSCOUT_API_KEY")),
            "note": "Gateway çalışıyor; harici veri kaynakları ayrıca sorgulanmalıdır."}


@router.get("/api/osint/chains")
async def chains():
    return {"supported": ["bitcoin", "ethereum", "base"],
            "planned_not_enabled": ["solana", "tron", "bnb", "arbitrum"],
            "read_only": True}


@router.get("/api/osint/wallet/{chain}/{address}")
async def wallet(request: Request, chain: str, address: str):
    chain = normalize_chain(chain)
    address = validate_address(chain, address)
    return await wallet_info(request, chain, address)


@router.get("/api/osint/tx/{chain}/{tx_hash}")
async def transaction(request: Request, chain: str, tx_hash: str):
    chain = normalize_chain(chain)
    tx_hash = validate_hash(chain, tx_hash)
    ep = f"tx/{tx_hash}" if chain == "bitcoin" else f"transactions/{tx_hash}"
    raw = await get_json(request, chain, ep)
    if not isinstance(raw, dict):
        raise HTTPException(502, "Beklenmeyen işlem yanıtı.")
    return {"chain": chain, "hash": tx_hash, "fetched_at": utc_now(),
            "explorer_url": explorer_tx(chain, tx_hash), "source": "mempool.space" if chain == "bitcoin" else "Blockscout",
            "data": raw, "note": "Ham işlem verisi kaynak yanıtıdır; gerçek kimlik bilgisi içermez."}


@router.post("/api/osint/graph")
async def graph(request: Request, payload: GraphRequest):
    chain = normalize_chain(payload.chain)
    address = validate_address(chain, payload.address)
    data = await wallet_info(request, chain, address)
    if chain == "bitcoin":
        try:
            raw = await get_json(request, chain, f"address/{address}/txs")
            data["_graph_raw_txs"] = raw if isinstance(raw, list) else []
        except HTTPException:
            data["_graph_raw_txs"] = []
    return make_graph(data, payload.limit)


def install_timi_osint(app: FastAPI) -> None:
    """TiMi SCALP PRO mevcut FastAPI uygulamasına salt-okunur OSINT ekler."""
    if getattr(app.state, "timi_osint_installed", False):
        return
    app.include_router(router)
    app.state.timi_osint_installed = True
