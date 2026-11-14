"""TiMi SCALP PRO ML MetaFilter v0.1 — optional read-only final approval.

Author: Suat DOĞU. No trading actions. Source: TiMi v1.8.4 server signal.
This component NEVER invents LONG or SHORT; unapproved ML always returns BEKLE.
Live route only scores a PRETRAINED, separately reviewed model. No training
is performed on incoming web requests. Optional dependencies for inference:
LightGBM. The default deployment needs none.
"""
from __future__ import annotations
import asyncio
import json
import os
from pathlib import Path
import time
from fastapi import HTTPException
from timi_ml_meta_features import FEATURE_KEYS, SCHEMA, extract_features, eligible_archive_record

MODEL_SCHEMA = "timi-metafilter-lightgbm-v1"
YES = {"1", "true", "yes", "on"}


def meta_gate(source, model_ready, probability=None, threshold=0.7):
    """Conservative approval: never change source side, never promote BEKLE."""
    if source not in ("LONG", "SHORT", "BEKLE"):
        return "BEKLE", "Geçersiz kaynak kararı"
    if source == "BEKLE":
        return "BEKLE", "Mevcut TiMi motoru BEKLE diyor"
    if not model_ready:
        return "BEKLE", "Bağımsız doğrulama onaylı ML modeli henüz yok"
    if probability is None or not 0 <= probability <= 1:
        return "BEKLE", "Geçerli ML olasılığı üretilemedi"
    if probability < threshold:
        return "BEKLE", "ML onay eşiği karşılanmadı"
    return source, "Mevcut TiMi sinyal yönü ML filtresinden geçti"


class MetaFilter:
    def __init__(self, *, get_candles, cached_pro, now_ms, symbols, root):
        self.get_candles = get_candles
        self.cached_pro = cached_pro
        self.now_ms = now_ms
        self.symbols = tuple(symbols)
        self.root = Path(root)
        self.enabled = os.getenv("TIMI_META_ENABLED", "0").lower() in YES
        self.model_file = os.getenv("TIMI_META_MODEL_PATH", "timi_meta_model.txt")
        self.meta_file = os.getenv("TIMI_META_METADATA_PATH", "timi_meta_model.json")
        self.loaded = False
        self.verified = False
        self.reason = "Model kurulmadı"
        self.booster = None
        self.metadata = None
        self._cache = {}
        self._lock = asyncio.Lock()
        self._load_once()

    def _load_once(self):
        # Training has to occur OUTSIDE the live FastAPI service.
        if not self.enabled:
            self.reason = "ML onay/veto devre dışı (varsayılan güvenli ayar)"
            return
        try:
            meta_path = self.root / self.meta_file
            model_path = self.root / self.model_file
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            if meta.get("schema") != MODEL_SCHEMA or meta.get("feature_schema") != SCHEMA:
                raise ValueError("Model/feature schema mismatch")
            if meta.get("feature_keys") != list(FEATURE_KEYS):
                raise ValueError("Model feature order mismatch")
            if meta.get("research_only", True) or meta.get("live_promotion_allowed") is not True:
                raise ValueError("Research-only model: live promotion not approved")
            if not (0.5 <= float(meta.get("threshold", 0.7)) <= 0.99):
                raise ValueError("Invalid approval threshold")
            # This explicit environment gate is not set by the training script.
            if os.getenv("TIMI_META_MANUAL_APPROVAL", "0").lower() not in YES:
                raise ValueError("Explicit manual model-approval gate is off")
            import lightgbm as lgb  # optional: only needed for verified, approved model
            booster = lgb.Booster(model_file=str(model_path))
            if booster.num_feature() != len(FEATURE_KEYS):
                raise ValueError("Model has wrong number of features")
            self.booster = booster
            self.metadata = meta
            self.loaded = self.verified = True
            self.reason = "Validated model loaded"
        except Exception as exc:
            self.verified = self.loaded = False
            self.booster = None
            self.reason = "Model onaylı değil veya yüklenemedi: " + type(exc).__name__

    async def evaluate(self, symbol):
        if symbol not in self.symbols:
            raise HTTPException(status_code=400, detail="Desteklenmeyen parite")
        # Server's actual original decision, never an invented model-only signal.
        pro = await self.cached_pro(symbol)
        source = (pro.get("signal") or {}).get("decision", "BEKLE")
        response = {
            "symbol": symbol, "source": "TiMi SCALP PRO /api/analysis-pro",
            "source_decision": source, "ml_decision": "BEKLE", "ml_status": "not_approved",
            "ml_model_loaded": self.loaded, "ml_live_approval_enabled": self.verified,
            "ml_probability": None, "ml_probability_is_calibrated": False,
            "indicators_26_in_feature_schema": True, "indicator_count": 26,
            "feature_count": len(FEATURE_KEYS),
            "orders_enabled": False, "live_model_training_enabled": False,
            "decision_evaluated_at_ms": pro.get("decision_evaluated_at_ms"),
            "reason": self.reason,
            "note": "No new LONG/SHORT direction. Spot SHORT is directional/exit analysis, not short execution."
        }
        if source not in ("LONG", "SHORT"):
            response["reason"] = "TiMi kural tabanlı karar BEKLE"
            return response
        if not self.verified:
            response["reason"] = self.reason
            return response
        # Only when the model is already approved, build features without blocking
        # the event loop. 26 indicators are computed from CLOSED 1m candles.
        try:
            candles = await self.get_candles(symbol, "1m", 260)
            from timi_ml_v5_archive import make_record
            captured = self.now_ms()
            record = await asyncio.to_thread(make_record, symbol, candles, pro, captured)
            if not eligible_archive_record(record):
                raise ValueError("Unhealthy / delayed snapshot")
            vector = extract_features(record)
            import numpy as np
            prob = float((await asyncio.to_thread(
                self.booster.predict,
                np.asarray([vector], dtype=float), num_threads=1))[0])
            if not 0 <= prob <= 1:
                raise ValueError("Nonprobabilistic model response")
            # Research calibration must be validated separately and serialized.
            calibration = self.metadata.get("calibration")
            if not calibration or calibration.get("type") != "isotonic":
                raise ValueError("Missing calibration")
            import bisect
            x = calibration.get("x") or []
            y = calibration.get("y") or []
            if len(x) != len(y) or len(x) < 2 or not all(a <= b for a, b in zip(x, x[1:])):
                raise ValueError("Invalid calibrator")
            i = max(0, min(len(x) - 2, bisect.bisect_right(x, prob) - 1))
            x1, x2 = float(x[i]), float(x[i+1])
            y1, y2 = float(y[i]), float(y[i+1])
            cp = max(0, min(1, y1 + (y2-y1) * ((prob-x1)/(x2-x1) if x2>x1 else 0)))
            verdict, reason = meta_gate(source, True, cp, float(self.metadata["threshold"]))
            response.update(ml_decision=verdict, ml_probability=round(cp, 5),
                            ml_probability_is_calibrated=True, ml_status="approved_model",
                            feature_snapshot_at_ms=record["feature_available_at_ms"],
                            reason=reason)
        except Exception as exc:
            response.update(ml_decision="BEKLE", ml_status="data_or_model_unavailable",
                            reason="ML verisi geçersiz veya güncel değil: " + type(exc).__name__)
        return response


def install_metafilter(app, *, get_candles, cached_pro, now_ms, symbols, root):
    """Optional installer. Add after `app = FastAPI(...)` in existing main.py."""
    engine = MetaFilter(get_candles=get_candles, cached_pro=cached_pro,
                        now_ms=now_ms, symbols=symbols, root=root)

    @app.get("/api/ml-meta/{symbol}")
    async def ml_meta(symbol: str):
        return await engine.evaluate(symbol.upper())

    return engine
