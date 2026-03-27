"""
xAI Grok (OpenAI-compatible). Opcional: GROK_ENABLED=false por defecto (stack gratis).

GrokManager: round-robin, 429 → siguiente clave, RPM/TPM a nivel equipo, backoff exponencial.
RPD suave: data/grok_rpd_state.json.

SCAN_GROK_MODE: disabled | polish_only | fallback_only | polish_and_fallback | always_grok
(vacío → disabled). Orquestación: core/ai_polish_manager.py (Groq primero).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import random
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Literal

import aiohttp

from core.ai_engine import PROMPT_STRUCTURE_ONLY_ES
from core.ai_usage_metrics import grok_estimated_cost_usd, record_ai_usage

logger = logging.getLogger(__name__)


def grok_enabled_flag() -> bool:
    """Grok desactivado por defecto (sin créditos / modo gratis)."""
    return os.getenv("GROK_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on")

GROK_DEFAULT_URL = "https://api.x.ai/v1/chat/completions"

QualityMode = Literal["low_cost", "high_quality"]

SYSTEM_TRADING_SYNTH = (
    "You are a senior crypto trading-desk lead. Rewrite the INPUT into a sharper institutional brief: "
    "clear bias (one word), key risk, risk-management angle (what would invalidate the read) — "
    "using ONLY numbers and claims already in the INPUT / snapshot excerpt. "
    "Do not invent prices, levels, or metrics. Max 130 words. English. Not financial advice. "
    + PROMPT_STRUCTURE_ONLY_ES
)

SYSTEM_FALLBACK = (
    "You are a senior crypto trading-desk analyst. From the JSON FACTS only, deliver: "
    "regime read, bias label, key risk, relevant levels (only if present in facts). "
    "Reason briefly, then one tight paragraph. Max 160 words. English. "
    "No invented data. Not financial advice. "
    + PROMPT_STRUCTURE_ONLY_ES
)

SYSTEM_ALERT_SECOND_READ = (
    "Eres Risk Desk Analyst especializado en movimientos rápidos de mercado. "
    "Analiza el pack de datos (sentimiento, insight de Gemini, movimiento % y snapshot) y genera una "
    "segunda opinión corta y útil.\n\n"
    "Reglas:\n"
    "- Máximo 85 palabras en inglés (EN).\n"
    "- Estructura obligatoria:\n"
    "  Bias: [una línea clara]\n"
    "  Risk: [enfocado en riesgos clave del movimiento actual]\n"
    "  Watch: [opcional - un punto de atención]\n"
    "  Disclaimer: (not financial advice)\n"
    "- Sé directo. Enfatiza gestión de riesgo. No des órdenes de trading.\n"
    + PROMPT_STRUCTURE_ONLY_ES
)


def _grok_rpd_state_path() -> str:
    return os.path.join(os.path.dirname(__file__), "..", "data", "grok_rpd_state.json")


def _key_fp16(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def load_grok_keys() -> list[str]:
    seen: set[str] = set()
    out: list[str] = []

    def push(k: str):
        k = k.strip()
        if len(k) < 12 or k in seen:
            return
        seen.add(k)
        out.append(k)

    raw = os.getenv("GROK_API_KEYS", "").strip()
    if raw:
        for part in raw.split(","):
            push(part)
    push(os.getenv("GROK_API_KEY", ""))
    for i in range(1, 16):
        push(os.getenv(f"GROK_API_KEY_{i}", ""))
    return out


def normalize_grok_scan_mode(raw: str | None = None) -> str:
    """
    disabled | polish_only | fallback_only | polish_and_fallback | always_grok
    """
    m = (raw if raw is not None else os.getenv("SCAN_GROK_MODE", "")).strip().lower()
    aliases = {
        "off": "disabled",
        "none": "disabled",
        "polish": "polish_only",
        "fallback": "fallback_only",
    }
    m = aliases.get(m, m)
    valid = frozenset(
        ("disabled", "polish_only", "fallback_only", "polish_and_fallback", "always_grok")
    )
    if not m:
        return "disabled"
    if m not in valid:
        logger.warning("SCAN_GROK_MODE=%r invalid; using disabled", m)
        return "disabled"
    return m


def scan_grok_mode() -> str:
    return normalize_grok_scan_mode(os.getenv("SCAN_GROK_MODE"))


def grok_configured() -> bool:
    return bool(load_grok_keys())


class GrokManager:
    """
    Rotación round-robin; límites de equipo RPM/TPM en ventana móvil de 60s;
    ante 429 pasa a la siguiente clave sin espera larga; backoff exponencial entre oleadas.
    """

    def __init__(self, keys: list[str]):
        self.keys = [k for k in keys if k]
        self._lock = asyncio.Lock()
        self._rr = 0
        self._team_req_ts: deque[float] = deque()
        self._team_token_entries: deque[tuple[float, int]] = deque()
        self._key_cooldown: dict[str, float] = {}
        self.team_rpm = int(os.getenv("GROK_TEAM_RPM_PER_MIN", "45"))
        self.team_tpm = int(os.getenv("GROK_TEAM_TPM_PER_MIN", "180000"))
        self._rpd_soft_cap = int(os.getenv("GROK_RPD_PER_KEY", "150"))
        self._day_count: dict[str, int] = {k: 0 for k in self.keys}
        self._day_date = ""
        self._load_rpd_state()

    def _utc_day(self) -> str:
        return datetime.now(timezone.utc).date().isoformat()

    def _roll_day(self):
        d = self._utc_day()
        if d != self._day_date:
            self._day_date = d
            for k in self.keys:
                self._day_count[k] = 0
            self._persist_rpd()

    def _load_rpd_state(self):
        self._day_date = self._utc_day()
        path = _grok_rpd_state_path()
        if not os.path.exists(path):
            return
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            logger.warning("grok_rpd_state load: %s", e)
            return
        if data.get("utc_date") != self._utc_day():
            return
        by_fp = data.get("rpd_by_key_fp", {})
        for k in self.keys:
            fp = _key_fp16(k)
            if fp in by_fp:
                self._day_count[k] = max(0, min(self._rpd_soft_cap, int(by_fp[fp])))

    def _persist_rpd(self):
        path = _grok_rpd_state_path()
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            payload = {
                "utc_date": self._utc_day(),
                "rpd_by_key_fp": {_key_fp16(k): int(self._day_count.get(k, 0)) for k in self.keys},
            }
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.warning("grok_rpd_state save: %s", e)

    @staticmethod
    def estimate_tokens(user: str, system_blob: str, max_out: int) -> int:
        base = (len(user) + len(system_blob)) // 4 + max_out + 200
        return min(64_000, max(300, base))

    def _prune_team_windows(self, now: float):
        while self._team_req_ts and now - self._team_req_ts[0] >= 60.0:
            self._team_req_ts.popleft()
        while self._team_token_entries and now - self._team_token_entries[0][0] >= 60.0:
            self._team_token_entries.popleft()

    def _team_tpm_sum(self) -> int:
        return sum(t for _, t in self._team_token_entries)

    def _ordered_keys(self) -> list[str]:
        if not self.keys:
            return []
        n = len(self.keys)
        i = self._rr % n
        return self.keys[i:] + self.keys[:i]

    async def _throttle_team(self, est_tokens: int):
        for _ in range(90):
            now = time.time()
            async with self._lock:
                self._prune_team_windows(now)
                rpm_ok = len(self._team_req_ts) < self.team_rpm
                tpm_ok = self._team_tpm_sum() + est_tokens <= int(self.team_tpm * 0.92)
                if rpm_ok and tpm_ok:
                    return
                if self._team_req_ts and len(self._team_req_ts) >= self.team_rpm:
                    wait = min(2.0, max(0.08, 60.0 - (now - self._team_req_ts[0])))
                else:
                    wait = 0.12
            await asyncio.sleep(wait + random.random() * 0.08)

    def _record_success(self, key: str, tokens_recorded: int):
        now = time.time()
        self._prune_team_windows(now)
        self._team_req_ts.append(now)
        self._team_token_entries.append((now, tokens_recorded))
        self._roll_day()
        self._day_count[key] = min(self._rpd_soft_cap, self._day_count.get(key, 0) + 1)
        self._persist_rpd()

    def _parse_usage_tokens(self, data: dict) -> int | None:
        u = data.get("usage")
        if isinstance(u, dict) and u.get("total_tokens") is not None:
            try:
                return int(u["total_tokens"])
            except (TypeError, ValueError):
                return None
        return None

    async def chat(
        self,
        system: str,
        user: str,
        *,
        max_tokens: int = 700,
        temperature: float = 0.35,
        quality: QualityMode = "high_quality",
        purpose: str = "grok_chat",
    ) -> str:
        if quality == "low_cost":
            max_tokens = min(max_tokens, int(os.getenv("GROK_LOW_COST_MAX_TOKENS", "400")))
            temperature = min(temperature, 0.3)

        if not self.keys:
            return ""

        url = os.getenv("GROK_API_BASE", GROK_DEFAULT_URL).strip() or GROK_DEFAULT_URL
        model = os.getenv("GROK_MODEL", "grok-2-latest").strip()
        est = self.estimate_tokens(user, system, max_tokens)

        max_waves = int(os.getenv("GROK_MAX_WAVES", "4"))
        payload_base: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user[:120_000]},
            ],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }

        for wave in range(max_waves):
            await self._throttle_team(est)
            keys_order = self._ordered_keys()
            now = time.time()
            any_tried = False

            for key in keys_order:
                if self._key_cooldown.get(key, 0) > now:
                    continue
                if self._day_count.get(key, 0) >= self._rpd_soft_cap:
                    continue
                any_tried = True
                headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
                timeout = aiohttp.ClientTimeout(total=90)
                try:
                    async with aiohttp.ClientSession(timeout=timeout) as session:
                        async with session.post(url, headers=headers, json=payload_base) as resp:
                            body = await resp.text()
                            if resp.status == 429:
                                self._key_cooldown[key] = time.time() + float(
                                    os.getenv("GROK_KEY_COOLDOWN_429_SEC", "45")
                                )
                                logger.info("Grok 429 → next key (wave %s)", wave)
                                continue
                            if resp.status != 200:
                                logger.warning("Grok HTTP %s: %s", resp.status, body[:350])
                                if resp.status in (401, 403):
                                    self._key_cooldown[key] = time.time() + 3600.0
                                continue
                            data = json.loads(body)
                            choices = data.get("choices") or []
                            if not choices:
                                continue
                            msg = choices[0].get("message") or {}
                            content = str(msg.get("content") or "").strip()
                            used = self._parse_usage_tokens(data) or est
                            async with self._lock:
                                self._record_success(key, int(used))
                                self._rr = (self._rr + 1) % len(self.keys)
                            record_ai_usage(
                                "grok",
                                purpose,
                                tokens=int(used),
                                estimated=used == est,
                                estimated_cost_usd=grok_estimated_cost_usd(int(used)),
                            )
                            return content
                except Exception as e:
                    logger.error("Grok request error: %s", e)
                    continue

            if not any_tried:
                # todas en cooldown o RPD soft
                backoff = min(
                    float(os.getenv("GROK_BACKOFF_CAP_SEC", "24")),
                    (2**wave) * (0.35 + random.random() * 0.35),
                )
                logger.warning("Grok: all keys cooling or capped; backoff %.2fs", backoff)
                await asyncio.sleep(backoff)
            else:
                backoff = min(
                    float(os.getenv("GROK_BACKOFF_CAP_SEC", "24")),
                    (2**wave) * (0.25 + random.random() * 0.25),
                )
                await asyncio.sleep(backoff)

        logger.error("GrokManager: exhausted after %s waves", max_waves)
        return ""


_manager: GrokManager | None = None


def get_grok_manager() -> GrokManager | None:
    global _manager
    if not grok_enabled_flag():
        _manager = None
        return None
    keys = load_grok_keys()
    if not keys:
        _manager = None
        return None
    if _manager is None or set(_manager.keys) != set(keys):
        _manager = GrokManager(keys)
        logger.info(
            "GrokManager: %d key(s) team RPM=%s TPM=%s (RPD soft/key=%s)",
            len(keys),
            os.getenv("GROK_TEAM_RPM_PER_MIN", "45"),
            os.getenv("GROK_TEAM_TPM_PER_MIN", "180000"),
            os.getenv("GROK_RPD_PER_KEY", "150"),
        )
    return _manager


async def grok_chat_completion(
    system: str,
    user: str,
    *,
    max_tokens: int = 700,
    temperature: float = 0.35,
    quality: QualityMode = "high_quality",
    purpose: str = "grok_chat",
) -> str:
    mgr = get_grok_manager()
    if not mgr:
        return ""
    return await mgr.chat(
        system,
        user,
        max_tokens=max_tokens,
        temperature=temperature,
        quality=quality,
        purpose=purpose,
    )


def _grok_quality_from_env(prefix: str) -> QualityMode:
    v = os.getenv(f"{prefix}_GROK_QUALITY", "").strip().lower()
    if v == "low_cost":
        return "low_cost"
    return "high_quality"


async def polish_with_grok(
    gemini_text: str,
    snapshot: dict | None = None,
    *,
    coin: str = "",
    quality: QualityMode | None = None,
) -> str:
    if not grok_enabled_flag():
        return ""
    mode = normalize_grok_scan_mode()
    if mode not in ("polish_only", "polish_and_fallback"):
        return ""
    if not grok_configured():
        return ""
    q = quality or _grok_quality_from_env("SCAN")
    parts = [f"Asset: {coin.upper()}\nINPUT:\n{gemini_text.strip()}"]
    if snapshot:
        parts.append(
            "\nSNAPSHOT_EXCERPT:\n"
            + json.dumps(snapshot, ensure_ascii=True)[:2800]
        )
    user = "\n".join(parts)
    return await grok_chat_completion(
        SYSTEM_TRADING_SYNTH,
        user,
        max_tokens=650,
        temperature=0.35,
        quality=q,
        purpose="scan_polish",
    )


async def generate_with_grok_fallback(
    coin: str,
    snapshot: dict,
    *,
    quality: QualityMode | None = None,
) -> str:
    if not grok_enabled_flag() or not grok_configured():
        return ""
    q = quality or _grok_quality_from_env("SCAN")
    facts = json.dumps({"asset": coin.upper(), "facts": snapshot}, ensure_ascii=True)[:8000]
    user = f"FACTS:\n{facts}\nFollow system instructions."
    return await grok_chat_completion(
        SYSTEM_FALLBACK,
        user,
        max_tokens=800,
        temperature=0.4,
        quality=q,
        purpose="scan_fallback",
    )


async def grok_volatility_second_read(
    symbol: str,
    sentiment_data: dict,
    gemini_insight: str | None,
    market_snapshot: dict,
    change_pct: float,
    timeframe: str = "5m",
    *,
    quality: QualityMode | None = None,
) -> str:
    if not grok_enabled_flag() or not grok_configured():
        return ""
    q = quality or _grok_quality_from_env("ALERT")
    excerpt = json.dumps(market_snapshot, ensure_ascii=True)[:3500]
    sent = json.dumps(sentiment_data, ensure_ascii=True)[:1200]
    user = (
        f"Symbol: {symbol}\n"
        f"Move: {round(float(change_pct), 4)}%\n"
        f"Timeframe: {timeframe}\n"
        f"Sentiment (JSON): {sent}\n"
        f"Gemini Insight: {(gemini_insight or '').strip()[:500]}\n"
        f"Snapshot Excerpt: {excerpt}"
    )[:12000]
    return await grok_chat_completion(
        SYSTEM_ALERT_SECOND_READ,
        user,
        max_tokens=420,
        temperature=0.35,
        quality=q,
        purpose="alert_second_read",
    )


# Alias retrocompat
async def polish_quantum_brief(gemini_text: str, coin: str) -> str:
    return await polish_with_grok(gemini_text, snapshot=None, coin=coin)


async def grok_fallback_quantum_scan(coin: str, snapshot: dict) -> str:
    return await generate_with_grok_fallback(coin, snapshot)
