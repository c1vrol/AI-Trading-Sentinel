"""
Telemetría ligera de llamadas a proveedores de IA (tokens aproximados / reales).
Archivo diario: data/ai_usage_daily.json — estimated_cost_usd solo si defines tarifa (p. ej. Grok).
"""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone

logger = logging.getLogger(__name__)
_lock = threading.Lock()


def _path() -> str:
    return os.path.join(os.path.dirname(__file__), "..", "data", "ai_usage_daily.json")


def _alert_sent_flag_path(day: str) -> str:
    return os.path.join(os.path.dirname(__file__), "..", "data", f"ai_usage_alert_{day}.sent")


def _alert_soft_flag_path(day: str) -> str:
    return os.path.join(os.path.dirname(__file__), "..", "data", f"ai_usage_alert_soft_{day}.sent")


def _alert_critical_flag_path(day: str) -> str:
    return os.path.join(os.path.dirname(__file__), "..", "data", f"ai_usage_alert_critical_{day}.sent")


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def grok_estimated_cost_usd(tokens: int) -> float:
    """USD aproximados solo si GROK_ESTIMATE_USD_PER_1M_TOKENS > 0 (mezcla in+out)."""
    try:
        rate = float(os.getenv("GROK_ESTIMATE_USD_PER_1M_TOKENS", "0") or 0)
    except (TypeError, ValueError):
        rate = 0.0
    return max(0, int(tokens or 0)) * max(0.0, rate) / 1_000_000.0


def today_tokens_and_cost() -> tuple[int, float]:
    """Totales del día UTC (tokens sumados por proveedor; coste estimado acumulado)."""
    day = _today()
    path = _path()
    with _lock:
        if not os.path.exists(path):
            return 0, 0.0
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            logger.debug("ai_usage_daily read: %s", e)
            return 0, 0.0
        block = data.get(day)
        if not isinstance(block, dict):
            return 0, 0.0
        totals = block.get("totals")
        if isinstance(totals, dict):
            tok = int(totals.get("tokens", 0) or 0)
            cost = float(totals.get("estimated_cost_usd", 0) or 0)
            return tok, cost
        bp = block.get("by_provider") or {}
        tok = sum(int(v.get("tokens", 0) or 0) for v in bp.values() if isinstance(v, dict))
        cost = sum(
            float(v.get("estimated_cost_usd", 0) or 0) for v in bp.values() if isinstance(v, dict)
        )
        return tok, cost


def usage_alert_sent_for_today() -> bool:
    """Compat: crítico ya enviado (misma semántica que antes del umbral dual)."""
    return usage_critical_alert_sent_for_today()


def mark_usage_alert_sent() -> None:
    mark_usage_critical_alert_sent()


def usage_soft_alert_sent_for_today() -> bool:
    return os.path.exists(_alert_soft_flag_path(_today()))


def mark_usage_soft_alert_sent() -> None:
    path = _alert_soft_flag_path(_today())
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(_today())
    except OSError as e:
        logger.warning("usage soft alert flag: %s", e)


def usage_critical_alert_sent_for_today() -> bool:
    return os.path.exists(_alert_critical_flag_path(_today())) or os.path.exists(
        _alert_sent_flag_path(_today())
    )


def mark_usage_critical_alert_sent() -> None:
    path = _alert_critical_flag_path(_today())
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(_today())
    except OSError as e:
        logger.warning("usage critical alert flag: %s", e)


def _budget_tokens() -> int:
    try:
        return max(0, int(os.getenv("AI_USAGE_TOKEN_ALERT_THRESHOLD", "0").strip() or "0"))
    except ValueError:
        return 0


def token_alert_limits() -> tuple[int, int, int]:
    """(budget, soft, critical) en tokens; budget 0 = desactivado."""
    budget = _budget_tokens()
    if budget <= 0:
        return 0, 0, 0
    try:
        sp = float(os.getenv("AI_USAGE_TOKEN_SOFT_PCT", "0.65") or 0.65)
    except (TypeError, ValueError):
        sp = 0.65
    try:
        cp = float(os.getenv("AI_USAGE_TOKEN_CRITICAL_PCT", "0.90") or 0.9)
    except (TypeError, ValueError):
        cp = 0.9
    sp = min(0.95, max(0.05, sp))
    cp = min(0.99, max(sp + 0.05, cp))
    soft = int(budget * sp)
    critical = int(budget * cp)
    return budget, soft, critical


def today_usage_digest() -> dict:
    """Totales del día + por proveedor + capas de polish (para logs / embeds)."""
    day = _today()
    path = _path()
    out: dict = {
        "day": day,
        "totals": {"calls": 0, "tokens": 0, "estimated_cost_usd": 0.0},
        "by_provider": {},
        "polish_layers": {},
    }
    with _lock:
        if not os.path.exists(path):
            return out
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            logger.debug("ai_usage_daily read digest: %s", e)
            return out
        block = data.get(day)
        if not isinstance(block, dict):
            return out
        tot = block.get("totals")
        if isinstance(tot, dict):
            out["totals"] = {
                "calls": int(tot.get("calls", 0) or 0),
                "tokens": int(tot.get("tokens", 0) or 0),
                "estimated_cost_usd": float(tot.get("estimated_cost_usd", 0) or 0),
            }
        bp = block.get("by_provider") or {}
        if isinstance(bp, dict):
            out["by_provider"] = {
                k: {
                    "calls": int(v.get("calls", 0) or 0),
                    "tokens": int(v.get("tokens", 0) or 0),
                }
                for k, v in bp.items()
                if isinstance(v, dict)
            }
        pl = block.get("polish_layers") or {}
        if isinstance(pl, dict):
            for k, v in pl.items():
                try:
                    out["polish_layers"][str(k)] = int(v)
                except (TypeError, ValueError):
                    continue
    return out


def increment_polish_layer(provider: str) -> None:
    """Una pasada de polish aplicada (scan): cuenta por proveedor para % Groq vs Grok vs Gemini."""
    if os.getenv("AI_USAGE_METRICS", "1").strip().lower() in ("0", "false", "off"):
        return
    p = str(provider).lower().strip()
    if p not in ("groq", "grok", "gemini"):
        return
    day = _today()
    path = _path()
    with _lock:
        data: dict = {}
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
            except Exception as e:
                logger.debug("ai_usage_daily read polish: %s", e)
        if day not in data or not isinstance(data[day], dict):
            data[day] = {"by_provider": {}, "by_purpose": {}}
        pl = data[day].setdefault("polish_layers", {})
        pl[p] = int(pl.get(p, 0) or 0) + 1
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.warning("ai_usage_daily polish_layers save: %s", e)


def record_ai_usage(
    provider: str,
    purpose: str,
    *,
    tokens: int | None = None,
    estimated: bool = False,
    estimated_cost_usd: float | None = None,
) -> None:
    if os.getenv("AI_USAGE_METRICS", "1").strip().lower() in ("0", "false", "off"):
        return
    day = _today()
    path = _path()
    with _lock:
        data: dict = {}
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
            except Exception as e:
                logger.debug("ai_usage_daily read: %s", e)
        if day not in data or not isinstance(data[day], dict):
            data[day] = {"by_provider": {}, "by_purpose": {}}

        bp = data[day].setdefault("by_provider", {})
        pp = data[day].setdefault("by_purpose", {})
        prov = str(provider).lower()[:24]
        purp = str(purpose).lower()[:48]
        tok = int(tokens or 0)
        cost = float(estimated_cost_usd or 0.0)

        if prov not in bp:
            bp[prov] = {"calls": 0, "tokens": 0, "tokens_estimated": 0, "estimated_cost_usd": 0.0}
        bp[prov]["calls"] = int(bp[prov].get("calls", 0)) + 1
        if tok:
            bp[prov]["tokens"] = int(bp[prov].get("tokens", 0)) + tok
            if estimated:
                bp[prov]["tokens_estimated"] = int(bp[prov].get("tokens_estimated", 0)) + tok
        if cost:
            bp[prov]["estimated_cost_usd"] = float(bp[prov].get("estimated_cost_usd", 0) or 0) + cost

        if purp not in pp:
            pp[purp] = {"calls": 0, "tokens": 0, "estimated_cost_usd": 0.0}
        pp[purp]["calls"] = int(pp[purp].get("calls", 0)) + 1
        if tok:
            pp[purp]["tokens"] = int(pp[purp].get("tokens", 0)) + tok
        if cost:
            pp[purp]["estimated_cost_usd"] = float(pp[purp].get("estimated_cost_usd", 0) or 0) + cost

        tot = data[day].setdefault(
            "totals", {"calls": 0, "tokens": 0, "tokens_estimated": 0, "estimated_cost_usd": 0.0}
        )
        tot["calls"] = int(tot.get("calls", 0)) + 1
        if tok:
            tot["tokens"] = int(tot.get("tokens", 0)) + tok
            if estimated:
                tot["tokens_estimated"] = int(tot.get("tokens_estimated", 0)) + tok
        if cost:
            tot["estimated_cost_usd"] = float(tot.get("estimated_cost_usd", 0) or 0) + cost

        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.warning("ai_usage_daily save: %s", e)

    extra = " (est.)" if estimated else ""
    if cost:
        logger.info(
            "AI usage | %s | %s | tokens=%s%s | ~$%.6f",
            provider,
            purpose,
            tok,
            extra,
            cost,
        )
    else:
        logger.info(
            "AI usage | %s | %s | tokens=%s%s",
            provider,
            purpose,
            tok,
            extra,
        )
