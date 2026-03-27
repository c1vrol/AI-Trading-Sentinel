"""
Orquestación gratuita-first: polish según POLISH_PREFERENCE_ORDER; Grok solo si GROK_ENABLED y modo scan.
Daily bias / HFT: no pasan por aquí (solo Gemini en el resto del bot).
"""
from __future__ import annotations

import json
import os
from collections.abc import Awaitable, Callable

from core import groq_client
from core import grok_client
from core.ai_engine import PROMPT_STRUCTURE_ONLY_ES
from core.ai_usage_metrics import increment_polish_layer
from core.grok_client import grok_enabled_flag
from core.groq_client import fallback_strategy_aggressive

# Etiquetas fijas alineadas con Gemini desk; salida EN para Groq/Grok.
SYSTEM_DESK_POLISH = (
    "Eres un Desk Editor senior. Tu tarea es pulir y mejorar el INPUT recibido manteniendo un tono "
    "institucional y profesional.\n"
    "Reglas estrictas:\n"
    "- Máximo 110 palabras en inglés (EN).\n"
    "- Usa exactamente estas etiquetas al inicio de cada línea:\n"
    "  Bias: \n"
    "  Levels: (solo si aparecen en INPUT o snapshot)\n"
    "  Risk: \n"
    "  Disclaimer: (not financial advice)\n"
    "- Mantén todos los hechos originales, no agregues precios ni niveles nuevos.\n"
    "- Sé más claro, directo y útil sin añadir relleno.\n"
    "- Si el texto original es débil o genérico, mejóralo manteniendo neutralidad.\n"
    + PROMPT_STRUCTURE_ONLY_ES
)

SYSTEM_DESK_FALLBACK = (
    "Eres un Risk Desk Analyst. Genera un análisis corto basado ÚNICAMENTE en el JSON de FACTS proporcionado.\n"
    "Reglas estrictas:\n"
    "- Máximo 130 palabras en inglés (EN).\n"
    "- Usa exactamente estas etiquetas:\n"
    "  Bias: \n"
    "  Levels: (solo si existen en FACTS)\n"
    "  Risk: \n"
    "  Disclaimer: (not financial advice)\n"
    "- No inventes ningún dato. Si falta información relevante, indícalo brevemente.\n"
    "- Sé profesional y objetivo.\n"
    + PROMPT_STRUCTURE_ONLY_ES
)

SYSTEM_ALERT_SECOND = (
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


def prefer_groq_for_polish() -> bool:
    return os.getenv("PREFER_GROQ_FOR_POLISH", "true").strip().lower() not in ("0", "false", "no", "off")


def parse_polish_preference_order() -> list[str]:
    raw = os.getenv("POLISH_PREFERENCE_ORDER", "groq,grok").strip().lower()
    seen: set[str] = set()
    order: list[str] = []
    for part in raw.split(","):
        p = part.strip()
        if p not in ("groq", "grok", "gemini") or p in seen:
            continue
        seen.add(p)
        order.append(p)
    return order or ["groq", "grok"]


def effective_polish_order() -> list[str]:
    o = parse_polish_preference_order()
    if not prefer_groq_for_polish():
        o = [x for x in o if x != "groq"]
    return o


def scan_polish_tier() -> str:
    return os.getenv("SCAN_POLISH_TIER", "free").strip().lower()


def normalize_grok_scan_mode(raw: str | None = None) -> str:
    return grok_client.normalize_grok_scan_mode(raw)


def polish_gemini_bridge_after_groq() -> bool:
    """Si gemini no está en POLISH_PREFERENCE_ORDER, intenta Gemini tras fallo Groq (429/agresivo por defecto)."""
    if "gemini" in effective_polish_order():
        return False
    raw = os.getenv("POLISH_GEMINI_BRIDGE_AFTER_GROQ", "").strip().lower()
    if raw in ("0", "false", "no", "off"):
        return False
    if raw in ("1", "true", "yes", "on"):
        return True
    return fallback_strategy_aggressive()


def alert_second_read_model() -> str:
    m = os.getenv("ALERT_SECOND_READ_MODEL", "groq").strip().lower()
    if m in ("", "none", "off"):
        return "none"
    if m in ("groq", "grok", "gemini"):
        return m
    return "groq"


def _alert_thresholds_met(sentiment_data: dict, change_pct: float) -> bool:
    try:
        conf = float(sentiment_data.get("confidence", 0.0))
    except (TypeError, ValueError):
        conf = 0.0
    min_c = float(os.getenv("ALERT_SECOND_READ_MIN_CONFIDENCE", "0.72"))
    min_m = float(os.getenv("ALERT_SECOND_READ_MIN_MOVE_ABS_PCT", "1.0"))
    need_both = os.getenv("ALERT_SECOND_READ_REQUIRE_BOTH", "0").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    move_ok = abs(float(change_pct)) >= min_m
    conf_ok = conf >= min_c
    return (conf_ok and move_ok) if need_both else (conf_ok or move_ok)


def should_run_alert_second_read(sentiment_data: dict, change_pct: float) -> bool:
    if os.getenv("ALERT_SECOND_READ_ENABLED", "1").strip().lower() in ("0", "false", "off"):
        return False
    model = alert_second_read_model()
    if model == "none":
        return False
    if model == "gemini":
        return False
    if model == "groq" and not groq_client.groq_configured():
        return False
    if model == "grok" and (not grok_enabled_flag() or not grok_client.grok_configured()):
        return False
    return _alert_thresholds_met(sentiment_data, change_pct)


def _alert_second_user_payload(
    symbol: str,
    sentiment_data: dict,
    gemini_insight: str | None,
    market_snapshot: dict,
    change_pct: float,
    timeframe: str,
) -> str:
    excerpt = json.dumps(market_snapshot, ensure_ascii=True)[:3200]
    sent = json.dumps(sentiment_data, ensure_ascii=True)[:1200]
    return (
        f"Symbol: {symbol}\n"
        f"Move: {round(float(change_pct), 4)}%\n"
        f"Timeframe: {timeframe}\n"
        f"Sentiment (JSON): {sent}\n"
        f"Gemini Insight: {(gemini_insight or '').strip()[:500]}\n"
        f"Snapshot Excerpt: {excerpt}"
    )


async def volatility_alert_second_read(
    symbol: str,
    sentiment_data: dict,
    gemini_insight: str | None,
    market_snapshot: dict,
    change_pct: float,
    timeframe: str = "5m",
) -> str:
    model = alert_second_read_model()
    user = _alert_second_user_payload(
        symbol,
        sentiment_data,
        gemini_insight,
        market_snapshot,
        change_pct,
        timeframe,
    )[:12000]

    if model == "groq" and groq_client.groq_configured():
        out = await groq_client.groq_chat(
            SYSTEM_ALERT_SECOND,
            user,
            max_tokens=320,
            temperature=0.35,
            quality="low_cost",
            purpose="alert_second_read",
        )
        return out.strip()

    if model == "grok" and grok_enabled_flag() and grok_client.grok_configured():
        return (
            await grok_client.grok_volatility_second_read(
                symbol,
                sentiment_data,
                gemini_insight,
                market_snapshot,
                change_pct,
                timeframe=timeframe,
            )
        ).strip()

    return ""


async def polish_scan_text(
    gemini_text: str,
    snapshot: dict | None,
    coin: str,
    *,
    gemini_polish_fn: Callable[[str], Awaitable[str]] | None = None,
) -> tuple[str, str | None]:
    """
    Recorre effective_polish_order() (env POLISH_PREFERENCE_ORDER, filtrado por PREFER_GROQ_FOR_POLISH).
    Grok: si el texto sigue siendo el borrador Gemini → siempre (si modo permite); si ya pasó Groq → solo tier premium.
    gemini_polish_fn: opcional, segunda pasada Gemini sobre el borrador actual.
    """
    text = gemini_text.strip()
    if not text:
        return gemini_text, None

    mode = normalize_grok_scan_mode()
    tier = scan_polish_tier()
    grok_may_polish = mode in ("polish_only", "polish_and_fallback") and grok_enabled_flag() and grok_client.grok_configured()

    start_text = text

    def build_user(current: str) -> str:
        parts = [f"Asset: {coin.upper()}\nINPUT:\n{current}"]
        if snapshot:
            parts.append(
                "\nSNAPSHOT_EXCERPT:\n" + json.dumps(snapshot, ensure_ascii=True)[:2400]
            )
        return "\n".join(parts)

    applied: list[str] = []

    for step in effective_polish_order():
        if step == "groq" and groq_client.groq_configured():
            pq = await groq_client.groq_chat(
                SYSTEM_DESK_POLISH,
                build_user(text),
                max_tokens=420,
                temperature=0.38,
                quality="low_cost" if tier == "free" else "high_quality",
                purpose="scan_polish",
            )
            if pq.strip():
                text = pq.strip()
                applied.append("groq")
                increment_polish_layer("groq")
            elif (
                polish_gemini_bridge_after_groq()
                and gemini_polish_fn is not None
            ):
                try:
                    gp = await gemini_polish_fn(text)
                except Exception:
                    gp = ""
                if gp and str(gp).strip():
                    text = str(gp).strip()
                    applied.append("gemini")
                    increment_polish_layer("gemini")
        elif step == "grok" and grok_may_polish:
            if text == start_text or tier == "premium":
                g = await grok_client.polish_with_grok(text, snapshot=snapshot, coin=coin)
                if g.strip():
                    text = g.strip()
                    applied.append("grok")
                    increment_polish_layer("grok")
        elif step == "gemini" and gemini_polish_fn is not None:
            gp = await gemini_polish_fn(text)
            if gp and str(gp).strip():
                text = str(gp).strip()
                applied.append("gemini")
                increment_polish_layer("gemini")

    if not applied:
        return gemini_text, None
    return text, "+".join(applied)


async def generate_scan_fallback(
    coin: str,
    snapshot: dict,
    *,
    gemini_retry_fn: Callable[[str, dict], Awaitable[str]] | None = None,
) -> tuple[str, str | None]:
    """Tras fallo Gemini: Groq (puede fallar rápido si FALLBACK_STRATEGY=aggressive); opción Gemini; Grok si modo."""
    mode = normalize_grok_scan_mode()
    grok_fb = mode in ("fallback_only", "polish_and_fallback", "always_grok") and grok_enabled_flag() and grok_client.grok_configured()

    facts = json.dumps({"asset": coin.upper(), "facts": snapshot}, ensure_ascii=True)[:8000]
    u = f"FACTS:\n{facts}\nFollow system instructions.\n\n{PROMPT_STRUCTURE_ONLY_ES}"

    if groq_client.groq_configured():
        o = await groq_client.groq_chat(
            SYSTEM_DESK_FALLBACK,
            u,
            max_tokens=520,
            temperature=0.4,
            purpose="scan_fallback",
        )
        if o.strip():
            return o.strip(), "groq"

    use_gem = gemini_retry_fn is not None and (
        fallback_strategy_aggressive()
        or os.getenv("SCAN_FALLBACK_USE_GEMINI", "0").strip().lower() in ("1", "true", "yes", "on")
    )
    if use_gem:
        try:
            gm = await gemini_retry_fn(coin, snapshot)
            if gm and str(gm).strip():
                return str(gm).strip(), "gemini"
        except Exception:
            pass

    if grok_fb:
        o = await grok_client.generate_with_grok_fallback(coin, snapshot)
        if o.strip():
            return o.strip(), "grok"

    return "", None


async def generate_always_grok_or_free_first(
    coin: str,
    snapshot: dict,
    *,
    gemini_retry_fn: Callable[[str, dict], Awaitable[str]] | None = None,
) -> tuple[str, str | None]:
    """always_grok: Grok primero si habilitado; si no, cadena Groq → Gemini (opc.) → Grok."""
    if grok_enabled_flag() and grok_client.grok_configured():
        o = await grok_client.generate_with_grok_fallback(coin, snapshot)
        if o.strip():
            return o.strip(), "grok"
    if groq_client.groq_configured():
        return await generate_scan_fallback(coin, snapshot, gemini_retry_fn=gemini_retry_fn)
    return "", None
