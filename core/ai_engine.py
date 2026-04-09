import json
import os
import re
import asyncio
import logging
import time
import random
import hashlib
from collections import deque
from datetime import datetime, timezone

try:
    from google import genai
    from google.genai import types
except ImportError:
    genai = None
    types = None

logger = logging.getLogger(__name__)

try:
    from core.ai_usage_metrics import record_ai_usage as _record_ai_usage
except ImportError:
    _record_ai_usage = None  # type: ignore[misc, assignment]

from core import groq_client

_NEWS_CAP = 1800
_MAX_TP_SL_MOVE_FRAC = 0.28

# Cierre estándar para salidas estructuradas (Gemini + alineación con desk Groq).
PROMPT_STRUCTURE_ONLY_ES = (
    "Responde solo con la estructura solicitada, sin introducciones ni explicaciones extras."
)

PROMPT_STRUCTURE_ONLY_EN = (
    "Respond only with the requested structure, with no extra introduction or explanation."
)

# Sentinel AI — instrucción base para embeds (p. ej. daily bias: solo esto en system_instruction).
SYSTEM_PROMPT = """
You are Sentinel AI Embed Copywriter — a senior institutional desk writer for a premium crypto analytics Discord bot.

Tiers:
- Market Preview (free/trial) → educational, high-level
- Core Analytics ($19.99/mo) → deeper, more structured
- Quantum Strategy ($49.99/mo or $299.99 lifetime) → highest nuance and professional depth

Global rules (strict):
- Maximum 140-155 words per embed body.
- Short paragraphs, **bold** section titles, bullets for readability.
- Tone: Professional institutional but accessible. Avoid dense academic language.
- Legal: NEVER give buy/sell orders or personalized advice. Use hypothetical language, stress-test references, market conditions, and educational framing only.
- Language:
  - daily_macro_bias and major_signals → Spanish
  - quantum_signal and ai_deep_dive → English

Always follow the exact schema for the given embed_type. Prioritize clarity, scannability, and high perceived value.
""".strip()


def _json_facts_for_prompt(obj: dict, max_len: int = 4200) -> str:
    """JSON legible para prompts; compacta o trunca si excede max_len."""
    raw = json.dumps(obj, ensure_ascii=False, indent=2)
    if len(raw) <= max_len:
        return raw
    compact = json.dumps(obj, ensure_ascii=False)
    if len(compact) <= max_len:
        return compact
    return compact[: max_len - 3] + "..."

_MONTHS_EN = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)


def quantum_scan_output_lang(bot_lang: str = "en") -> str:
    """
    Quantum tier (/ai_scan, HFT validation prompts) uses English only for a consistent
    premium desk product. QUANTUM_SCAN_LANG and BOT_LANGUAGE are ignored for scan output.
    """
    _ = bot_lang
    _ = os.getenv("QUANTUM_SCAN_LANG", "")
    return "en"


def _gemini_rpd_state_path() -> str:
    return os.path.join(os.path.dirname(__file__), "..", "data", "gemini_rpd_state.json")


def _key_fp16(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


class GeminiKeyScheduler:
    """
    Gestión por llave según cuotas típicas Gemini (ajustables por env):
    RPM, RPD, TPM estimado. Reserva atómica antes de la llamada; rollback si falla (excepto 429 ya manejado).
    """

    def __init__(self, keys: list[str]):
        self.keys = [k for k in keys if k]
        self.rpm = int(os.getenv("GEMINI_RPM_PER_KEY", "5"))
        self.rpd = int(os.getenv("GEMINI_RPD_PER_KEY", "20"))
        self.tpm_limit = int(os.getenv("GEMINI_TPM_PER_KEY", "250000"))
        self.max_wait = float(os.getenv("GEMINI_ACQUIRE_MAX_WAIT_SEC", "120"))
        self._lock = asyncio.Lock()
        self._minute_ts: dict[str, deque] = {k: deque() for k in self.keys}
        self._token_window: dict[str, deque] = {k: deque() for k in self.keys}
        self._day_count: dict[str, int] = {k: 0 for k in self.keys}
        self._day_date: str = ""
        self._load_rpd_state()

    def _utc_day(self) -> str:
        return datetime.now(timezone.utc).date().isoformat()

    def _roll_day(self):
        d = self._utc_day()
        if d != self._day_date:
            self._day_date = d
            for k in self.keys:
                self._day_count[k] = 0
            self.persist_rpd_to_disk()

    def _load_rpd_state(self):
        path = _gemini_rpd_state_path()
        if not os.path.exists(path):
            self._day_date = self._utc_day()
            return
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            logger.warning("gemini_rpd_state load: %s", e)
            self._day_date = self._utc_day()
            return
        today = self._utc_day()
        if data.get("utc_date") != today:
            self._day_date = today
            return
        self._day_date = today
        by_fp = data.get("rpd_by_key_fp", {})
        for k in self.keys:
            fp = _key_fp16(k)
            if fp in by_fp:
                v = int(by_fp[fp])
                self._day_count[k] = max(0, min(self.rpd, v))

    def persist_rpd_to_disk(self):
        """Persiste RPD consumido hoy (UTC) por huella de clave — sin guardar la clave en claro."""
        path = _gemini_rpd_state_path()
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            by_fp = {_key_fp16(k): int(self._day_count.get(k, 0)) for k in self.keys}
            payload = {
                "utc_date": self._utc_day(),
                "rpd_by_key_fp": by_fp,
                "successful_calls_today": sum(self._day_count.values()),
            }
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.warning("gemini_rpd_state save: %s", e)

    def _prune_minute(self, key: str, now: float):
        dq = self._minute_ts[key]
        while dq and now - dq[0] >= 60.0:
            dq.popleft()
        tq = self._token_window[key]
        while tq and now - tq[0][0] >= 60.0:
            tq.popleft()

    def _token_sum_minute(self, key: str) -> int:
        return sum(t for _, t in self._token_window[key])

    @staticmethod
    def _est_tokens(prompt: str, sys_blob: str) -> int:
        return min(48_000, max(400, (len(prompt) + len(sys_blob)) // 3 + 800))

    def _pick_and_reserve(self, pool: list[str], prompt: str, sys_blob: str) -> str | None:
        now = time.time()
        self._roll_day()
        est = self._est_tokens(prompt, sys_blob)
        tpm_cap = int(self.tpm_limit * 0.92)
        candidates: list[tuple[int, str]] = []
        for k in pool:
            if k not in self._minute_ts:
                continue
            self._prune_minute(k, now)
            if self._day_count[k] >= self.rpd:
                continue
            if len(self._minute_ts[k]) >= self.rpm:
                continue
            if self._token_sum_minute(k) + est > tpm_cap:
                continue
            candidates.append((self._day_count[k], k))
        if not candidates:
            return None
        candidates.sort(key=lambda x: (x[0], x[1]))
        key = candidates[0][1]
        self._minute_ts[key].append(now)
        self._token_window[key].append((now, est))
        self._day_count[key] += 1
        return key

    def rollback_reserve(self, key: str):
        if key not in self._minute_ts:
            return
        dq = self._minute_ts[key]
        if dq:
            dq.pop()
        tq = self._token_window[key]
        if tq:
            tq.pop()
        self._day_count[key] = max(0, self._day_count[key] - 1)

    async def acquire(self, pool: list[str], prompt: str, sys_blob: str) -> str:
        deadline = time.monotonic() + self.max_wait
        while time.monotonic() < deadline:
            async with self._lock:
                key = self._pick_and_reserve(pool, prompt, sys_blob)
                if key:
                    return key
            await asyncio.sleep(0.2 + random.random() * 0.2)
        logger.error("GeminiKeyScheduler: no capacity in pool within %.1fs", self.max_wait)
        raise RuntimeError("gemini_capacity_timeout")


class AIEngine:
    SYSTEM_BASE = (
        "Eres un Senior Market Risk Analyst con estilo institucional (bancos y hedge funds). "
        "Tu rol es proporcionar análisis educativo y commentary basado exclusivamente en los datos proporcionados. "
        "IMPORTANTE: Esto NO es consejo financiero, de inversión, fiscal ni recomendación de trading. "
        "No sugieras abrir, cerrar o modificar posiciones. El usuario asume todo el riesgo. "
        "Sé siempre claro, conciso y profesional. "
        "Si faltan datos o hay errores en los facts, menciónalo brevemente sin inventar información. "
        "Nunca des garantías de resultados ni uses lenguaje promocional. "
        + PROMPT_STRUCTURE_ONLY_ES
    )

    MISSION_NAMES = ("MONITOR", "USER_VIP", "ADMIN_FORCE", "SYSTEM_LOOP")

    _DEFAULT_MISSION_POOLS: dict[str, list[str]] = {
        "MONITOR": [
            "AIzaSyA5xc847AXAdXE60MNtchC03P2bmdFm-m0",
            "AIzaSyBMP3MiUuF6rciJ5-dKvYQm_a45PXmwNak",
        ],
        "USER_VIP": [
            "AIzaSyDaEkcuOqYyP2XXwJXmAvPEmeNKJ015d7s",
        ],
        "ADMIN_FORCE": [
            "AIzaSyBN_7jgeWG-7K3_EpYAiBDhRKB5Y-7sW8s",
        ],
        "SYSTEM_LOOP": [
            "AIzaSyBx_IadhKwisUWVY82-Hl3bokKVQ2QhDx4",
        ],
    }

    @classmethod
    def _default_keys_flat(cls) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for pool in cls._DEFAULT_MISSION_POOLS.values():
            for k in pool:
                if k not in seen:
                    seen.add(k)
                    out.append(k)
        return out

    def __init__(self, api_key: str = ""):
        self.cooldowns: dict[str, float] = {}
        self.clients: dict[str, object] = {}

        # Pools aislados por misión (sin “cascada” entre monitoreo / VIP / admin / tareas)
        self.missions = {m: list(p) for m, p in self._DEFAULT_MISSION_POOLS.items()}
        already = set(self._default_keys_flat())

        extras: list[str] = []
        env_multi = os.getenv("GEMINI_API_KEYS", "").strip()
        if env_multi:
            for part in env_multi.split(","):
                k = part.strip()
                if len(k) > 12:
                    extras.append(k)
        env_one = (api_key or "").strip() or os.getenv("GEMINI_API_KEY", "").strip()
        if env_one and env_one not in extras:
            extras.insert(0, env_one)

        new_only = [k for k in extras if k not in already]
        for i, k in enumerate(new_only):
            mission = self.MISSION_NAMES[i % len(self.MISSION_NAMES)]
            self.missions[mission].append(k)

        all_keys: list[str] = []
        for m in self.MISSION_NAMES:
            for k in self.missions[m]:
                if k not in all_keys:
                    all_keys.append(k)

        if genai and all_keys:
            for k in all_keys:
                try:
                    self.clients[k] = genai.Client(api_key=k)
                except Exception as e:
                    logger.error("Failed to init Gemini client: %s", e)

        for m in self.MISSION_NAMES:
            self.missions[m] = [k for k in self.missions[m] if k in self.clients]

        sched_keys: list[str] = []
        for m in self.MISSION_NAMES:
            for k in self.missions[m]:
                if k not in sched_keys:
                    sched_keys.append(k)

        self._scheduler = GeminiKeyScheduler(sched_keys) if sched_keys else None

        if not self.clients:
            logger.error("AIEngine: no valid Gemini clients (check keys and google-genai install).")

        self.api_call_count = 0
        for m in self.MISSION_NAMES:
            n = len(self.missions[m])
            logger.info("AIEngine pool %-12s → %d key(s)", m, n)
        logger.info(
            "Scheduler RPM=%s RPD=%s TPM~=%s (pick: least-used in-pool, no cross-mission spill)",
            os.getenv("GEMINI_RPM_PER_KEY", "5"),
            os.getenv("GEMINI_RPD_PER_KEY", "20"),
            os.getenv("GEMINI_TPM_PER_KEY", "250000"),
        )

    def _merge_system(self, system_instruction: str) -> str | None:
        extra = (system_instruction or "").strip()
        if not extra:
            return self.SYSTEM_BASE
        return f"{self.SYSTEM_BASE} {extra}"

    def _pool_for_mission(self, mission: str) -> list[str]:
        """Solo claves reservadas a esta misión; nunca se reutilizan claves de otra cola."""
        return [k for k in self.missions.get(mission, []) if k in self.clients]

    def _mark_cooldown(self, key: str, duration: float = 60):
        logger.warning(
            "API Key %s... enters cooldown for %.1fs (429/Rate Limit).",
            key[:10],
            duration,
        )
        self.cooldowns[key] = time.time() + duration

    def _filter_cooldown(self, pool: list[str]) -> list[str]:
        now = time.time()
        return [k for k in pool if self.cooldowns.get(k, 0) <= now]

    async def generate_response(
        self,
        prompt: str,
        system_instruction: str = "",
        mission: str = "USER_VIP",
        response_mime_type: str = "text/plain",
    ):
        if not self.clients or not self._scheduler:
            raise RuntimeError("gemini_not_ready")

        sys_blob = self._merge_system(system_instruction) or ""
        pool_base = self._pool_for_mission(mission)
        if not pool_base:
            raise RuntimeError(f"no_keys_for_mission:{mission}")

        max_retries = max(6, len(pool_base) * 3)

        for attempt in range(max_retries):
            pool = self._filter_cooldown(pool_base)
            if not pool:
                # OPTIMIZACIÓN: Si todas las llaves están bloqueadas (429) y tenemos Groq listo,
                # no esperamos los 9 reintentos. Lanzamos el error ya para que el fallback actúe.
                if attempt > 0 and groq_client.groq_configured():
                    logger.warning(f"All Gemini keys in cooldown for {mission}. Fast-falling back to Groq.")
                    raise RuntimeError("gemini_exhausted_retries")
                
                await asyncio.sleep(1.0)
                pool = pool_base

            key = await self._scheduler.acquire(pool, prompt, sys_blob)
            client = self.clients[key]

            try:
                response = await asyncio.to_thread(
                    client.models.generate_content,
                    model="gemini-2.0-flash",
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        system_instruction=sys_blob,
                        response_mime_type=response_mime_type,
                    ),
                )
                self.api_call_count += 1
                self._scheduler.persist_rpd_to_disk()
                if _record_ai_usage:
                    est = GeminiKeyScheduler._est_tokens(prompt, sys_blob)
                    _record_ai_usage(
                        "gemini",
                        str(mission),
                        tokens=int(est),
                        estimated=True,
                        estimated_cost_usd=0.0,
                    )
                return response
            except Exception as e:
                error_str = str(e).lower()
                is_429 = (
                    "429" in error_str
                    or "resourceexhausted" in error_str
                    or "too many requests" in error_str
                )
                
                async with self._scheduler._lock:
                    self._scheduler.rollback_reserve(key)
                    self._scheduler.persist_rpd_to_disk()

                if is_429:
                    # Detección de retry_delay del error
                    retry_wait = 0.0
                    try:
                        # Algunos SDKs de Google exponen el retry_delay en los atributos
                        if hasattr(e, 'retry_delay'):
                            retry_wait = float(getattr(e, 'retry_delay'))
                        elif 'retrydelay' in error_str:
                            # Parsear "retryDelay: 26s" del string si no hay atributo
                            match = re.search(r"retrydelay(?::|\s*)(\d+)", error_str)
                            if match:
                                retry_wait = float(match.group(1))
                    except:
                        pass
                    
                    # Backoff Exponencial si no se detectó delay específico o como extra
                    if retry_wait <= 0:
                        retry_wait = (2 ** attempt) + random.uniform(0.5, 1.5)
                    
                    # Limitar espera máxima (60s) para no bloquear la tarea perpetuamente
                    retry_wait = min(60.0, retry_wait)
                    
                    self._mark_cooldown(key, duration=retry_wait + 5.0)
                    
                    if attempt < max_retries - 1:
                        logger.info(
                            "Mission %s: 429 on key %s...; actual retry_wait=%.1fs", 
                            mission, key[:8], retry_wait
                        )
                        await asyncio.sleep(retry_wait)
                        continue
                        
                # Si falló y es el último intento o no es un 429 recuperable
                logger.error("Mission %s error [%s...]: %s", mission, key[:8], e)
                raise RuntimeError("gemini_exhausted_retries from exception") from e

        raise RuntimeError("gemini_exhausted_retries")

    @staticmethod
    def _lang_line(lang: str) -> str:
        l = (lang or "en").strip()
        return f"Write prose in: {l}."

    @staticmethod
    def _tp_sl_plausible(price: float, tp: float, sl: float, condition: str) -> bool:
        try:
            price = float(price)
            tp = float(tp)
            sl = float(sl)
        except (TypeError, ValueError):
            return False
        if price <= 0 or tp <= 0 or sl <= 0:
            return False
            
        reward = abs(tp - price)
        risk = abs(price - sl)
        if risk == 0 or reward < (1.5 * risk):
            return False
            
        cap = price * _MAX_TP_SL_MOVE_FRAC
        cond = (condition or "").lower()
        if "oversold" in cond:
            if not (sl < price < tp):
                return False
            return (tp - price) <= cap and (price - sl) <= cap
        if "overbought" in cond:
            if not (tp < price < sl):
                return False
            return (price - tp) <= cap and (sl - price) <= cap
        return abs(tp - price) <= cap and abs(sl - price) <= cap

    @staticmethod
    def _parse_quantum_signal_text(raw: str) -> dict | None:
        """Parse line-oriented quantum_signal output (no JSON). Keys are case-insensitive."""
        text = (raw or "").strip()
        if text.startswith("```"):
            lines = text.split("\n")
            if lines and lines[0].strip().startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines).strip()

        def _first_float(val: str) -> float | None:
            s = (val or "").replace(",", "")
            m = re.search(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?", s)
            if not m:
                return None
            try:
                return float(m.group(0))
            except ValueError:
                return None

        rows: dict[str, str] = {}
        for line in text.splitlines():
            s = line.strip().replace("**", "")
            if not s or s.startswith("#"):
                continue
            s = re.sub(r"^(?:\d+[\).\]]\s*)+", "", s)
            if ":" not in s:
                continue
            k, v = s.split(":", 1)
            key = k.strip().upper().replace(" ", "_")
            val = v.strip()
            if not key:
                continue
            alias = {
                "DESK_READ": "SENTIMENT_LABEL",
                "SENTIMENT": "SENTIMENT_LABEL",
                "LABEL": "SENTIMENT_LABEL",
            }.get(key, key)
            rows[alias] = val
        if not rows:
            return None
        vs = (rows.get("VALID") or "").strip().lower()
        if vs in ("no", "false", "0", "n"):
            valid = False
        elif vs in ("true", "yes", "1", "y"):
            valid = True
        else:
            valid = False
        tp = _first_float(rows.get("TP", ""))
        sl = _first_float(rows.get("SL", ""))
        if tp is None or sl is None:
            return None
        sentiment_label = (rows.get("SENTIMENT_LABEL") or rows.get("SENTIMENT") or "").strip()
        reason = (rows.get("REASON") or "").strip()[:240]
        cm = (rows.get("CONFIDENCE") or "Moderate").strip().lower()
        if cm == "high":
            conf_raw = "High"
        elif cm == "low":
            conf_raw = "Low"
        else:
            conf_raw = "Moderate"
        return {
            "valid": valid,
            "tp": tp,
            "sl": sl,
            "reason": reason,
            "sentiment_label": sentiment_label[:120],
            "confidence": conf_raw,
        }

    async def analyze_sentiment(
        self,
        text: str,
        lang: str = "en",
        mission: str = "MONITOR",
        market_context: dict | None = None,
    ) -> dict:
        excerpt = (text or "")[:_NEWS_CAP].replace("\r", " ")
        payload = {
            "news_excerpt": excerpt,
            "market_facts": market_context or {},
        }
        sys_instr = (
            f"{self._lang_line(lang)} Return one JSON object only, no markdown. "
            + PROMPT_STRUCTURE_ONLY_ES
        )
        prompt = (
            "TASK: Assess whether the NEWS_EXCERPT skews bullish, bearish, or neutral "
            "relative to MARKET_FACTS (if facts are empty or error, lower confidence). "
            "Do not invent prices, levels, or events not in the input.\n"
            f"INPUT:{json.dumps(payload, ensure_ascii=True)}\n"
            'SCHEMA:{"sentiment":"BULLISH|BEARISH|NEUTRAL","confidence":0.0,"thesis":"max 140 chars",'
            '"disclaimer":"Not advice; model output only."}\n'
            + PROMPT_STRUCTURE_ONLY_ES
        )

        try:
            gen_response = await self.generate_response(
                prompt=prompt,
                system_instruction=sys_instr,
                mission=mission,
                response_mime_type="application/json",
            )
            data = json.loads(str(gen_response.text))
            return {
                "sentiment": str(data.get("sentiment", "NEUTRAL")).upper(),
                "confidence": float(data.get("confidence", 0.0)),
                "support": 0.0,
                "resistance": 0.0,
                "thesis": str(data.get("thesis", ""))[:280],
                "disclaimer": str(data.get("disclaimer", "Not financial advice."))[:200],
            }
        except Exception as rt_e:
            is_exhausted = "exhausted_retries" in str(rt_e).lower()
            is_429 = "429" in str(rt_e) or "resourceexhausted" in str(rt_e).lower()
            
            if (is_exhausted or is_429) and groq_client.groq_configured():
                logger.warning(f"Gemini exhausted for {mission}. Falling back to Groq for analyze_sentiment.")
                try:
                    groq_resp = await groq_client.groq_chat(
                        system=sys_instr,
                        user=prompt,
                        max_tokens=420,
                        temperature=0.35,
                        purpose="sentiment_fallback",
                        response_format={"type": "json_object"}
                    )
                    
                    # Groq might wrap the json in ```json ... ``` markdown
                    clean_resp = groq_resp.strip()
                    if clean_resp.startswith("```"):
                        lines = clean_resp.split("\n")
                        if lines[0].startswith("```"):
                            lines = lines[1:]
                        if lines and lines[-1].startswith("```"):
                            lines = lines[:-1]
                        clean_resp = "\n".join(lines).strip()
                    
                    data = json.loads(clean_resp)
                    return {
                        "sentiment": str(data.get("sentiment", "NEUTRAL")).upper(),
                        "confidence": float(data.get("confidence", 0.0)),
                        "support": 0.0,
                        "resistance": 0.0,
                        "thesis": str(data.get("thesis", ""))[:280],
                        "disclaimer": str(data.get("disclaimer", "Not financial advice."))[:200],
                    }
                except Exception as groq_e:
                    logger.error("analyze_sentiment (Groq fallback failed): %s", groq_e)
            else:
                logger.error("analyze_sentiment: %s", rt_e)
        except Exception as e:
            logger.error("analyze_sentiment: %s", e)
            
        return {
            "sentiment": "NEUTRAL",
            "confidence": 0.0,
            "support": 0.0,
            "resistance": 0.0,
            "thesis": "Cross-check live tape; multiple factors at play.",
            "disclaimer": "",
        }

    async def analyze_market_batch(self, market_data: dict, lang: str = "en") -> dict:
        sys_instr = f"{self._lang_line(lang)} JSON only, no markdown. {PROMPT_STRUCTURE_ONLY_ES}"
        slim = json.dumps(market_data, ensure_ascii=True)[:7500]
        prompt = (
            "TASK: For each symbol in MARKET_BATCH, one-line desk bias from supplied numbers only.\n"
            f"MARKET_BATCH:{slim}\n"
            "OUTPUT: JSON object whose keys are symbol strings; each value is "
            '{"bias":"bullish|bearish|neutral","confidence":0.0-1,"note":"max 90 chars"}. '
            "If data for a symbol is missing, omit key or set neutral with low confidence.\n"
            + PROMPT_STRUCTURE_ONLY_ES
        )
        try:
            gen_response = await self.generate_response(
                prompt=prompt,
                system_instruction=sys_instr,
                mission="MONITOR",
                response_mime_type="application/json",
            )
            return json.loads(gen_response.text)
        except Exception as rt_e:
            is_exhausted = "exhausted_retries" in str(rt_e).lower()
            is_429 = "429" in str(rt_e) or "resourceexhausted" in str(rt_e).lower()
            
            if (is_exhausted or is_429) and groq_client.groq_configured():
                logger.warning(f"Gemini exhausted for MONITOR. Falling back to Groq for analyze_market_batch.")
                try:
                    groq_resp = await groq_client.groq_chat(
                        system=sys_instr,
                        user=prompt,
                        max_tokens=250,
                        temperature=0.35,
                        purpose="market_batch_fallback",
                        response_format={"type": "json_object"}
                    )
                    
                    clean_resp = groq_resp.strip()
                    if clean_resp.startswith("```"):
                        lines = clean_resp.split("\n")
                        if lines[0].startswith("```"):
                            lines = lines[1:]
                        if lines and lines[-1].startswith("```"):
                            lines = lines[:-1]
                        clean_resp = "\n".join(lines).strip()
                    return json.loads(clean_resp)
                except Exception as groq_e:
                    logger.error("analyze_market_batch (Groq fallback failed): %s", groq_e)
            else:
                logger.error("analyze_market_batch: %s", rt_e)
            return {}
        except Exception as e:
            logger.error("analyze_market_batch: %s", e)
            return {}

    async def get_emergency_insight(
        self,
        symbol: str,
        price_change: float,
        timeframe: str = "5m",
        lang: str = "en",
        market_snapshot: dict | None = None,
    ) -> str:
        facts = {
            "symbol": symbol,
            "candle_change_pct": round(price_change, 4),
            "timeframe": timeframe,
            "snapshot": market_snapshot or {},
        }
        sys_instr = (
            f"{self._lang_line(lang)} Max 24 words. No trade instructions. {PROMPT_STRUCTURE_ONLY_ES}"
        )
        prompt = (
            "TASK: One-sentence risk-desk style explanation of the move using only FACTS. "
            "If snapshot is empty, acknowledge limited data.\n"
            f"FACTS:{json.dumps(facts, ensure_ascii=True)[:1200]}\n"
            + PROMPT_STRUCTURE_ONLY_ES
        )
        try:
            gen_response = await self.generate_response(
                prompt=prompt, system_instruction=sys_instr, mission="MONITOR"
            )
            return gen_response.text.strip()
        except Exception as rt_e:
            is_exhausted = "exhausted_retries" in str(rt_e).lower()
            is_429 = "429" in str(rt_e) or "resourceexhausted" in str(rt_e).lower()
            
            if (is_exhausted or is_429) and groq_client.groq_configured():
                logger.warning(f"Gemini exhausted for MONITOR. Falling back to Groq for get_emergency_insight.")
                try:
                    groq_resp = await groq_client.groq_chat(
                        system=sys_instr,
                        user=prompt,
                        max_tokens=60,
                        temperature=0.4,
                        purpose="emergency_fallback"
                    )
                    return groq_resp.strip()
                except Exception as groq_e:
                    logger.error("get_emergency_insight (Groq fallback failed): %s", groq_e)
            else:
                logger.error("get_emergency_insight: %s", rt_e)
            return (
                "Volatility print in line with tape; confirm against your own levels and risk rules."
            )
        except Exception as e:
            logger.error("get_emergency_insight: %s", e)
            return (
                "Volatility print in line with tape; confirm against your own levels and risk rules."
            )

    async def evaluate_hft_signal(
        self,
        symbol: str,
        timeframe: str,
        rsi: float,
        price: float,
        condition: str,
        lang: str = "en",
        market_context: dict | None = None,
    ) -> dict:
        ctx = {
            "pair": symbol,
            "tf": timeframe,
            "signal_price": round(float(price), 8),
            "rsi14": round(float(rsi), 2),
            "local_condition": condition,
            "snapshot": market_context or {},
        }
        sys_instr = (
            f"{SYSTEM_PROMPT}\n\n"
            "embed_type: quantum_signal\n\n"
            "Quantum Signal — strict output contract:\n"
            "- English only. Exactly 6 non-empty lines, in this order. No blank lines, no JSON, no markdown fences, no preamble or postscript.\n"
            "- Each line must start with the key, a colon, a single space, then the value (one line per key).\n"
            "- Keys (copy exactly): VALID, TP, SL, SENTIMENT_LABEL, CONFIDENCE, REASON\n\n"
            "Line meanings:\n"
            "1) VALID: yes or no\n"
            "2) TP: one numeric hypothetical stress-test reference level (pair currency only)\n"
            "3) SL: one numeric hypothetical stress-test reference level (pair currency only)\n"
            "4) SENTIMENT_LABEL: desk skew in English; anchor vocabulary: Bearish, Bullish, Neutral, Mean-reversion (combine as needed, e.g. \"Bullish mean-reversion skew\").\n"
            "5) CONFIDENCE: exactly High, Moderate, or Low\n"
            "6) REASON: single line, max 240 characters, clear institutional tone\n\n"
            "Primary RSI rule: rsi14 in DATA is the 15m last-closed RSI — the same figure the product shows in Local Tape. "
            "If SNAPSHOT includes other RSI prints (e.g. 1h), cite them only as secondary context with an explicit label "
            "(e.g. '1h context: …'); never present them as replacing the primary gate.\n\n"
            "Hypothetical language only. Never trading orders. TP/SL are hypothetical stress-test reference bands, not instructions.\n"
            + PROMPT_STRUCTURE_ONLY_EN
        )
        prompt = (
            "embed_type: quantum_signal\n"
            "TASK: Hypothetical desk consistency check from RSI setup + SNAPSHOT (educational only).\n"
            f"DATA:{json.dumps(ctx, ensure_ascii=True)[:2200]}\n\n"
            "OUTPUT — paste exactly 6 lines, in this order, nothing else:\n"
            "VALID: yes or no\n"
            "TP: <number>\n"
            "SL: <number>\n"
            "SENTIMENT_LABEL: <English desk skew; use Bearish / Bullish / Neutral / Mean-reversion as appropriate>\n"
            "CONFIDENCE: High | Moderate | Low\n"
            "REASON: <one line, max 240 characters>\n\n"
            "REASON must anchor the desk read to rsi14 (15m, closed) in DATA — that is the primary RSI members see in Local Tape. "
            "Other RSI values in SNAPSHOT may be mentioned only as labeled context (e.g. '1h: …'), not as the main gate.\n\n"
            "TP/SL: hypothetical stress-test anchors only, max ~25% from signal_price in DATA. "
            "Oversold / long-leaning → sl < price < tp. Overbought / short-leaning → tp < price < sl.\n"
            "CRITICAL MATHEMATICS: You must enforce a minimum Risk/Reward ratio of 1:2. "
            "The potential reward (distance to TP) must be at least twice the potential risk (distance to SL).\n"
            + PROMPT_STRUCTURE_ONLY_EN
        )
        try:
            gen_response = await self.generate_response(
                prompt=prompt,
                system_instruction=sys_instr,
                mission="MONITOR",
            )
            data = self._parse_quantum_signal_text(str(gen_response.text or ""))
            if not data:
                raise ValueError("quantum_signal parse failed")
            valid = bool(data.get("valid"))
            tp = float(data.get("tp", 0.0))
            sl = float(data.get("sl", 0.0))
            reason = str(data.get("reason", ""))[:240]
            sentiment_label = str(data.get("sentiment_label", "")).strip()[:120]
            conf_raw = str(data.get("confidence", "Moderate"))
            if valid and not self._tp_sl_plausible(price, tp, sl, condition):
                return {
                    "valid": False,
                    "tp": tp,
                    "sl": sl,
                    "reason": "TP/SL failed sanity check vs price/setup.",
                    "sentiment_label": sentiment_label,
                    "confidence": conf_raw,
                }
            return {
                "valid": valid,
                "tp": tp,
                "sl": sl,
                "reason": reason,
                "sentiment_label": sentiment_label,
                "confidence": conf_raw,
            }
        except Exception as e:
            logger.error("evaluate_hft_signal: %s", e)
            return {
                "valid": False,
                "reason": "Validation deferred—review local RSI and tape.",
            }

    @staticmethod
    def build_quantum_scan_prompt(coin: str, snapshot: dict, lang: str = "en") -> tuple[str, str]:
        lc = (lang or "en").strip().lower()
        if lc.startswith("es"):
            sys_instr = (
                f"Idioma: {lang}. Responde en español. "
                "Máximo 80 palabras. Usa exactamente estas etiquetas:\n"
                "Bias: \n"
                "Levels: (solo rangos o niveles presentes en FACTS)\n"
                "Risk: \n"
                "Disclaimer: \n\n"
                "Sé directo y profesional. Enfócate en el contexto actual del mercado."
            )
            task = (
                "TASK: Escribe un commentary institucional corto usando ÚNICAMENTE los FACTS proporcionados. "
                "No inventes precios, niveles ni indicadores que no estén en los facts. "
                "Si hay un campo 'error', menciónalo brevemente."
            )
        else:
            sys_instr = (
                f"Language: {lang}. Respond in English. "
                "Max 80 words. Use exactly these labels:\n"
                "Bias: \n"
                "Levels: (only ranges or levels present in FACTS)\n"
                "Risk: \n"
                "Disclaimer: \n\n"
                "Be direct and professional. Focus on current market context."
            )
            task = (
                "TASK: Write a short institutional commentary using ONLY the FACTS provided. "
                "Do not invent prices, levels, or indicators not present in the facts. "
                "If there is an 'error' field, mention it briefly."
            )
        body = json.dumps({"asset": coin.upper(), "facts": snapshot}, ensure_ascii=False)[:3400]
        prompt = f"{task}\n\nFACTS:\n{body}\n\n{PROMPT_STRUCTURE_ONLY_ES}"
        return prompt, sys_instr

    @staticmethod
    def build_quantum_scan_prompt_minimal(coin: str, snapshot: dict, lang: str = "en") -> tuple[str, str]:
        lc = (lang or "en").strip().lower()
        if lc.startswith("es"):
            sys_instr = (
                f"Idioma: {lang}. Responde en español. Máximo 55 palabras. "
                "Líneas: Bias: | Levels: (solo si están en FACTS) | Risk: | Disclaimer: . "
                "Solo hechos; sin órdenes de trading."
            )
            task = "Comentario mínimo solo desde FACTS; si hay 'error' en facts, indícalo brevemente."
        else:
            sys_instr = (
                f"Language: {lang}. Respond in English. Max 55 words. "
                "Lines: Bias: | Levels: (only if in FACTS) | Risk: | Disclaimer: . "
                "Facts only; no trade orders."
            )
            task = "Minimal commentary from FACTS only; if facts contain 'error', note briefly."
        body = json.dumps({"asset": coin.upper(), "facts": snapshot}, ensure_ascii=False)[:2800]
        prompt = f"{task}\n\nFACTS:\n{body}\n\n{PROMPT_STRUCTURE_ONLY_ES}"
        return prompt, sys_instr

    async def generate_quantum_scan_minimal(
        self,
        coin: str,
        snapshot: dict,
        lang: str = "en",
    ) -> str:
        prompt, sys_instr = AIEngine.build_quantum_scan_prompt_minimal(coin, snapshot, lang)
        response = await self.generate_response(
            prompt=prompt,
            system_instruction=sys_instr,
            mission="USER_VIP",
        )
        return str(response.text or "").strip()

    async def polish_quantum_scan_desk(
        self,
        coin: str,
        text: str,
        snapshot: dict | None,
        lang: str = "en",
    ) -> str:
        """Segunda pasada Gemini sobre el borrador (USER_VIP pool; polish_layers en JSON cuenta capas)."""
        sys_instr = (
            f"{self._lang_line(lang)} Desk editor: polish INPUT (and snapshot if any). Max 110 words. "
            "Use exactly these line prefixes: Bias: | Levels: (only if in INPUT or snapshot) | "
            "Risk: | Disclaimer: (not financial advice). "
            "Keep all original facts; do not add new prices or levels. "
            "Clearer and more useful; stay neutral. " + PROMPT_STRUCTURE_ONLY_ES
        )
        parts = [f"Asset: {coin.upper()}\nINPUT:\n{(text or '').strip()}"]
        if snapshot:
            parts.append(
                "\nSNAPSHOT_EXCERPT:\n" + json.dumps(snapshot, ensure_ascii=True)[:2400]
            )
        prompt = "\n".join(parts)
        response = await self.generate_response(
            prompt=prompt,
            system_instruction=sys_instr,
            mission="USER_VIP",
        )
        return str(response.text or "").strip()

    @staticmethod
    def format_desk_date_english(dt: datetime) -> str:
        """Current calendar date for headers; English month names (locale-independent)."""
        return f"{_MONTHS_EN[dt.month - 1]} {dt.day}, {dt.year}"

    @staticmethod
    def build_daily_bias_prompt(snapshot: dict, report_date: str) -> tuple[str, str]:
        """
        Genera Daily Macro Bias en español.
        Retorna (user_prompt, SYSTEM_PROMPT).
        """
        rd = (report_date or "").strip()
        facts_json = json.dumps(snapshot, ensure_ascii=False, indent=2)
        if len(facts_json) > 4200:
            facts_json = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))[:4200]

        user_prompt = f"""
embed_type: daily_macro_bias
REPORT_DATE: {rd}

FACTS:
{facts_json}

Genera el Daily Macro Bias en español siguiendo esta estructura EXACTA:

**Sesgo:** [emoji] **Etiqueta corta y clara**

**Factores clave a vigilar:**
- Viñeta concreta 1
- Viñeta concreta 2
- Viñeta concreta 3

**Implicación para BTC/ETH:**
Párrafo breve y condicional (qué fortalecería o debilitaría el sesgo).

**Veredicto Sentinel:**
1-2 frases claras y útiles.

Reglas estrictas:
- Máximo 150 palabras en total.
- Sé directo, profesional y fácil de leer.
- Base todo estrictamente en los FACTS. No inventes nada.
- Usa lenguaje accesible pero institucional.

Responde ÚNICAMENTE con el texto estructurado.
""".strip()
        return user_prompt, SYSTEM_PROMPT

    async def polish_deep_dive_triplet(
        self,
        trio: list[dict],
        market_pack: dict | None = None,
    ) -> list[dict]:
        """
        Refina el texto de 3 bloques (inglés, ai_deep_dive). Mantiene id / emoji / title; solo reescribe content.
        """
        if not trio:
            return trio
        slim = [
            {
                "id": b.get("id"),
                "emoji": b.get("emoji", "▫️"),
                "title": b.get("title", "Note"),
                "content": (b.get("content") or "")[:1400],
            }
            for b in trio
        ]
        sys_instr = (
            f"{SYSTEM_PROMPT}\n\n"
            "embed_type: ai_deep_dive. English only. Return one JSON object only, no markdown fences.\n"
            "Rewrite each block's \"content\" only. Copy \"id\", \"emoji\", and \"title\" from INPUT_BLOCKS byte-for-byte unchanged.\n\n"
            "Strict layout per block (all sections required, in this order):\n"
            "1) First line: one **bold** hook (max ~12 words).\n"
            "2) **BTC:** then at least 2 bullet lines starting with \"- \" (concrete watch items from INPUT/MARKET_FACTS only).\n"
            "3) **ETH:** then at least 2 bullet lines starting with \"- \".\n"
            "4) **Stress-test / invalidation:** one or two sentences, hypothetical desk language only.\n\n"
            "Scannability: inside hook, bullets, and stress-test lines (not the literal labels **BTC:** / **ETH:**), "
            "wrap 3–6 concrete desk concepts or market shorthand phrases drawn from INPUT in **bold** "
            "(short spans, typically 2–5 words each — e.g. thematic labels members should spot on a skim).\n\n"
            "Hard limits: max 165 words per block; max 2 sentences per bullet cluster intro; no filler "
            "(avoid \"it is important to note\", \"in conclusion\", \"remember that\").\n"
            "No buy/sell/sizing or personalized advice. Do not add prices or levels absent from INPUT or MARKET_FACTS.\n"
            + PROMPT_STRUCTURE_ONLY_EN
        )
        mp = json.dumps(market_pack or {}, ensure_ascii=False)[:2800]
        user = (
            "INPUT_BLOCKS:\n"
            + json.dumps(slim, ensure_ascii=False, indent=2)[:5200]
            + "\n\nMARKET_FACTS_JSON (optional; do not invent beyond this):\n"
            + mp
            + "\n\nSCHEMA: {\"blocks\": [ objects matching INPUT_BLOCKS with improved \"content\" only ]}\n"
            "Same block count, order, ids, emoji, and titles as INPUT_BLOCKS. "
            "Each \"content\" must follow the section order and minimum bullets defined in system instructions.\n"
            + PROMPT_STRUCTURE_ONLY_EN
        )
        try:
            gen_response = await self.generate_response(
                prompt=user,
                system_instruction=sys_instr,
                mission="SYSTEM_LOOP",
                response_mime_type="application/json",
            )
            data = json.loads(str(gen_response.text))
        except Exception as e:
            logger.error("polish_deep_dive_triplet: %s", e)
            return trio
        blocks = data.get("blocks")
        if not isinstance(blocks, list) or len(blocks) != len(trio):
            return trio
        by_id: dict = {}
        for x in blocks:
            if not isinstance(x, dict):
                continue
            bid = x.get("id")
            c = (x.get("content") or "").strip()
            if bid is not None and c:
                by_id[bid] = c[:3500]
        out: list[dict] = []
        for b in trio:
            nb = dict(b)
            cid = b.get("id")
            if cid in by_id:
                nb["content"] = by_id[cid]
            out.append(nb)
        return out

    @staticmethod
    def build_admin_macro_prompt(btc_snap: dict, eth_snap: dict, lang: str = "en") -> tuple[str, str]:
        sys_instr = (
            f"Language: {lang}. Max 52 words. Not advice. End with one relevant emoji. "
            + PROMPT_STRUCTURE_ONLY_ES
        )
        pack = json.dumps({"BTC": btc_snap, "ETH": eth_snap}, ensure_ascii=True)[:3500]
        prompt = (
            "TASK: Emergency global crypto macro blurb using ONLY FACTS. If data errors, state briefly.\n"
            f"FACTS:{pack}\n" + PROMPT_STRUCTURE_ONLY_ES
        )
        return prompt, sys_instr
