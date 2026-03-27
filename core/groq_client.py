"""
Groq OpenAI-compatible API (Llama 3.3 70B, etc.). Round-robin, 429 → siguiente key, RPM/TPM en ventana 60s.

Free tier (referencia ~2026; ajustar GROQ_*): ~30 RPM, ~12K TPM, ~1k RPD/key.
FALLBACK_STRATEGY=aggressive: 429 en todas las keys intentadas (o sin key disponible) → sale pronto
para que el orquestador use Gemini/Grok sin varias olas de backoff.
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

from core.ai_usage_metrics import record_ai_usage

logger = logging.getLogger(__name__)

GROQ_DEFAULT_URL = "https://api.groq.com/openai/v1/chat/completions"
QualityMode = Literal["low_cost", "high_quality"]


def _rpd_path() -> str:
    return os.path.join(os.path.dirname(__file__), "..", "data", "groq_rpd_state.json")


def _key_fp16(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def load_groq_keys() -> list[str]:
    seen: set[str] = set()
    out: list[str] = []

    def push(k: str):
        k = k.strip()
        if len(k) < 8 or k in seen:
            return
        seen.add(k)
        out.append(k)

    raw = os.getenv("GROQ_API_KEYS", "").strip()
    if raw:
        for part in raw.split(","):
            push(part)
    push(os.getenv("GROQ_API_KEY", ""))
    for i in range(1, 16):
        push(os.getenv(f"GROQ_API_KEY_{i}", ""))
    return out


def groq_configured() -> bool:
    return bool(load_groq_keys())


def fallback_strategy_aggressive() -> bool:
    """aggressive | conservador: en agresivo, 429 en todas las claves intentadas → salir sin olas largas."""
    raw = os.getenv("FALLBACK_STRATEGY", "aggressive").strip().lower()
    if raw in ("conservative", "conservador", "slow"):
        return False
    return raw in ("aggressive", "agresivo", "fast", "1", "true", "yes", "")


class GroqManager:
    def __init__(self, keys: list[str]):
        self.keys = [k for k in keys if k]
        self._lock = asyncio.Lock()
        self._rr = 0
        self._team_req_ts: deque[float] = deque()
        self._team_token_entries: deque[tuple[float, int]] = deque()
        self._key_cooldown: dict[str, float] = {}
        self.team_rpm = int(os.getenv("GROQ_TEAM_RPM_PER_MIN", "60"))
        self.team_tpm = int(os.getenv("GROQ_TEAM_TPM_PER_MIN", "120000"))
        self._rpd_soft = int(os.getenv("GROQ_RPD_PER_KEY", "2000"))
        self._day_count: dict[str, int] = {k: 0 for k in self.keys}
        self._day_date = ""
        self._load_rpd()

    def _utc_day(self) -> str:
        return datetime.now(timezone.utc).date().isoformat()

    def _roll_day(self):
        d = self._utc_day()
        if d != self._day_date:
            self._day_date = d
            for k in self.keys:
                self._day_count[k] = 0
            self._persist_rpd()

    def _load_rpd(self):
        self._day_date = self._utc_day()
        path = _rpd_path()
        if not os.path.exists(path):
            return
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            logger.warning("groq_rpd_state load: %s", e)
            return
        if data.get("utc_date") != self._utc_day():
            return
        by_fp = data.get("rpd_by_key_fp", {})
        for k in self.keys:
            fp = _key_fp16(k)
            if fp in by_fp:
                self._day_count[k] = max(0, min(self._rpd_soft, int(by_fp[fp])))

    def _persist_rpd(self):
        path = _rpd_path()
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "utc_date": self._utc_day(),
                        "rpd_by_key_fp": {_key_fp16(k): int(self._day_count.get(k, 0)) for k in self.keys},
                    },
                    f,
                    indent=2,
                    ensure_ascii=False,
                )
        except Exception as e:
            logger.warning("groq_rpd_state save: %s", e)

    @staticmethod
    def estimate_tokens(user: str, system_blob: str, max_out: int) -> int:
        base = (len(user) + len(system_blob)) // 4 + max_out + 150
        return min(64_000, max(200, base))

    def _prune(self, now: float):
        while self._team_req_ts and now - self._team_req_ts[0] >= 60.0:
            self._team_req_ts.popleft()
        while self._team_token_entries and now - self._team_token_entries[0][0] >= 60.0:
            self._team_token_entries.popleft()

    def _tpm_sum(self) -> int:
        return sum(t for _, t in self._team_token_entries)

    def _ordered_keys(self) -> list[str]:
        if not self.keys:
            return []
        n = len(self.keys)
        i = self._rr % n
        return self.keys[i:] + self.keys[:i]

    async def _throttle(self, est: int):
        for _ in range(90):
            now = time.time()
            async with self._lock:
                self._prune(now)
                rpm_ok = len(self._team_req_ts) < self.team_rpm
                tpm_ok = self._tpm_sum() + est <= int(self.team_tpm * 0.92)
                if rpm_ok and tpm_ok:
                    return
                wait = (
                    min(2.0, max(0.08, 60.0 - (now - self._team_req_ts[0])))
                    if self._team_req_ts and len(self._team_req_ts) >= self.team_rpm
                    else 0.12
                )
            await asyncio.sleep(wait + random.random() * 0.06)

    def _record_ok(self, key: str, toks: int):
        now = time.time()
        self._prune(now)
        self._team_req_ts.append(now)
        self._team_token_entries.append((now, toks))
        self._roll_day()
        self._day_count[key] = min(self._rpd_soft, self._day_count.get(key, 0) + 1)
        self._persist_rpd()

    @staticmethod
    def _usage_tokens(data: dict) -> int | None:
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
        max_tokens: int = 512,
        temperature: float = 0.35,
        quality: QualityMode = "high_quality",
        purpose: str = "groq_chat",
    ) -> str:
        if quality == "low_cost":
            max_tokens = min(max_tokens, int(os.getenv("GROQ_LOW_COST_MAX_TOKENS", "320")))
            temperature = min(temperature, 0.32)

        if not self.keys:
            return ""

        url = os.getenv("GROQ_API_BASE", GROQ_DEFAULT_URL).strip() or GROQ_DEFAULT_URL
        model = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile").strip()
        est = self.estimate_tokens(user, system, max_tokens)
        payload: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user[:120_000]},
            ],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        max_waves = int(os.getenv("GROQ_MAX_WAVES", "4"))
        aggressive = fallback_strategy_aggressive()
        if aggressive:
            max_waves = min(
                max_waves,
                max(1, int(os.getenv("GROQ_AGGRESSIVE_MAX_WAVES", "1"))),
            )

        for wave in range(max_waves):
            await self._throttle(est)
            now = time.time()
            any_tried = False
            n_429 = 0
            n_other = 0
            for key in self._ordered_keys():
                if self._key_cooldown.get(key, 0) > now:
                    continue
                if self._day_count.get(key, 0) >= self._rpd_soft:
                    continue
                any_tried = True
                headers = {
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                }
                try:
                    async with aiohttp.ClientSession(
                        timeout=aiohttp.ClientTimeout(total=75)
                    ) as session:
                        async with session.post(url, headers=headers, json=payload) as resp:
                            body = await resp.text()
                            if resp.status == 429:
                                n_429 += 1
                                self._key_cooldown[key] = time.time() + float(
                                    os.getenv("GROQ_KEY_COOLDOWN_429_SEC", "30")
                                )
                                logger.info("Groq 429 → next key (wave %s)", wave)
                                continue
                            if resp.status != 200:
                                n_other += 1
                                logger.warning("Groq HTTP %s: %s", resp.status, body[:300])
                                if resp.status in (401, 403):
                                    self._key_cooldown[key] = time.time() + 3600.0
                                continue
                            data = json.loads(body)
                            choices = data.get("choices") or []
                            if not choices:
                                n_other += 1
                                continue
                            msg = choices[0].get("message") or {}
                            content = str(msg.get("content") or "").strip()
                            used = self._usage_tokens(data) or est
                            async with self._lock:
                                self._record_ok(key, int(used))
                                self._rr = (self._rr + 1) % len(self.keys)
                            record_ai_usage(
                                "groq",
                                purpose,
                                tokens=int(used),
                                estimated=used == est,
                                estimated_cost_usd=0.0,
                            )
                            return content
                except Exception as e:
                    n_other += 1
                    logger.error("Groq error: %s", e)
                    continue

            if aggressive and (
                (not any_tried)
                or (n_429 > 0 and n_other == 0)
            ):
                logger.info(
                    "Groq aggressive fallback: sin respuesta (429-only o sin claves disponibles); "
                    "delegando siguiente proveedor"
                )
                return ""

            backoff = min(
                float(os.getenv("GROQ_BACKOFF_CAP_SEC", "20")),
                (2**wave) * (0.25 + random.random() * 0.25),
            )
            if not any_tried:
                logger.warning("Groq: all keys cooling; backoff %.2fs", backoff)
            await asyncio.sleep(backoff)

        logger.error("GroqManager exhausted")
        return ""


_groq_mgr: GroqManager | None = None


def get_groq_manager() -> GroqManager | None:
    global _groq_mgr
    keys = load_groq_keys()
    if not keys:
        _groq_mgr = None
        return None
    if _groq_mgr is None or set(_groq_mgr.keys) != set(keys):
        _groq_mgr = GroqManager(keys)
        logger.info(
            "GroqManager: %d key(s) RPM~=%s TPM~=%s",
            len(keys),
            os.getenv("GROQ_TEAM_RPM_PER_MIN", "60"),
            os.getenv("GROQ_TEAM_TPM_PER_MIN", "120000"),
        )
    return _groq_mgr


async def groq_chat(
    system: str,
    user: str,
    *,
    max_tokens: int = 512,
    temperature: float = 0.35,
    quality: QualityMode = "high_quality",
    purpose: str = "groq_chat",
) -> str:
    mgr = get_groq_manager()
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
