import discord
from discord.ext import commands, tasks
from discord import app_commands
from google import genai
import logging
import asyncio
import datetime
import random
import os
import json
import re
import time
import io
import urllib.parse
from PIL import Image, ImageDraw, ImageFont, ImageOps, ImageFilter
import aiohttp
import psutil
from typing import Literal

from core.ai_engine import AIEngine, quantum_scan_output_lang
from core.ai_polish_manager import (
    generate_always_grok_or_free_first,
    generate_scan_fallback,
    normalize_grok_scan_mode,
    polish_scan_text,
)

_log = logging.getLogger(__name__)

# ==========================================
# 🗺️ MAPA DE CANALES Y COLORES
# ==========================================
TRIAL_ALERTS_ID = 1486410079837094079
FREE_ANALYSIS_ID = 1486410103627190443
MAJOR_SIGNALS_ID = 1486410173193912501
DAILY_BIAS_ID = 1486410192093712384
VOLUME_SPIKES_ID = 1486410214604275822
QUANTUM_SIGNALS_ID = 1486410255780024371
AI_DEEP_DIVE_ID = 1486410290819236061
UPGRADE_CHANNEL_ID = 1486410002880139335
UPGRADE_MESSAGE_ID = 1486774005942976816
UPGRADE_LOG_ID = 1486792994672738435
PROFIT_WINS_ID = 1486414055152423063
WELCOME_CHANNEL_ID = 1486409907728154765  # Target for Welcome Cards
# ==========================================
# 🛡️ MAPA DE REACTION ROLES Y LOGS
# ==========================================
REACTION_CHANNEL_ID = 1486427940563714230
REACTION_MESSAGE_IDS = [1486540733056946487]
LOG_CHANNEL_ID = 1485739818741928157 # Technical Monitor
AUDIT_LOG_ID = 1486526604065771520  # Audit / Legal Logs
RESTRICTION_ROLE_ID = 1486527735441391626
ADMIN_ROLE_ID = 1486177336054517881
ROLE_QUANTUM = 1486157344944427129
ROLE_LIFETIME = 1486167569432838154
ROLE_CORE = 1486156058836729896

ROLE_TRANSITIONS = {
    # Old Role: New Role
    1486475961590485122: 1486156058836729896,
    1486476406018936953: 1486156558168490055,
    1486476439044755497: 1486157344944427129,
    1486476426419900466: 1486167569432838154
}

COLOR_FREE = 0x5a6cae
COLOR_CORE = 0x2b4c7e
COLOR_QUANTUM = 0x8200c9

# ==========================================
# 🛡️ DYNAMIC COOLDOWN LOGIC
# ==========================================
def ai_cooldown_logic(interaction: discord.Interaction) -> app_commands.Cooldown | None:
    """Tiered AI Limit: Quantum (6h), Core (12h), Free (24h)"""
    if any(r.id == ROLE_QUANTUM for r in interaction.user.roles):
        return app_commands.Cooldown(1, 21600)  # 6 hours
    if any(r.id == ROLE_CORE for r in interaction.user.roles):
        return app_commands.Cooldown(1, 43200)  # 12 hours
    return app_commands.Cooldown(1, 86400)      # 24 hours

def market_cooldown_logic(interaction: discord.Interaction) -> app_commands.Cooldown | None:
    """Tiered Price Limit: Quantum (10s), Core (30s), Free (60s)"""
    if any(r.id == ROLE_QUANTUM for r in interaction.user.roles):
        return app_commands.Cooldown(1, 10)
    if any(r.id == ROLE_CORE for r in interaction.user.roles):
        return app_commands.Cooldown(1, 30)
    return app_commands.Cooldown(1, 60)
class SentinelCog(commands.Cog):
    def __init__(self, bot: commands.Bot, ai_engine=None):
        self.bot = bot
        self.ai_engine = ai_engine
            
        # Fusible diario (persistido en data/bot_ai_fuse_state.json)
        self.daily_ai_count = 0
        self.last_reset_date = datetime.datetime.now(datetime.timezone.utc).date()
        self._bot_ai_fuse_path = os.path.join(os.path.dirname(__file__), "..", "data", "bot_ai_fuse_state.json")
        self._load_bot_ai_fuse_from_disk()
        
        # Caché Quantum Scan: compartida entre todos los usuarios (misma moneda → mismo texto)
        self.quantum_scan_cache_path = os.path.join(
            os.path.dirname(__file__), "..", "data", "quantum_scan_cache.json"
        )
        self.ai_scan_user_api_path = os.path.join(
            os.path.dirname(__file__), "..", "data", "ai_scan_user_api_times.json"
        )
        # {'BTC': {'time': float, 'text': str, 'dual_desk'?: bool}}
        self.ai_cache = {}
        self._ai_scan_user_api_times: dict[str, float] = {}
        self._load_quantum_scan_cache_from_disk()
        self._load_ai_scan_user_api_times_from_disk()

        self.global_cache_path = os.path.join(os.path.dirname(__file__), "..", "data", "global_cache.json")
        
        # Limitadores Diarios Tiers Gratuitos (Trial Alerts)
        self.last_censored_alert_date = None
        self.last_delayed_alert_date = None
        
        # Snippets Educativos (Academy)
        self.educational_snippets = [
            "**Order Blocks:** Son zonas institucionales donde el precio gira repentinamente dejando un desequilibrio. Es donde las 'ballenas' posicionan sus órdenes.",
            "**RSI (Relative Strength Index):** Un nivel por encima de 70 indica sobrecompra (posible caída), mientras que por debajo de 30 indica sobreventa extrema (posible rebote).",
            "**Fair Value Gaps (FVG):** Ocurren cuando el precio se mueve tan rápido que deja vacíos de liquidez. El mercado algorítmico tiende a rellenarlos tarde o temprano.",
            "**Liquidity Sweeps (Cacería de liquidez):** Cuando el precio rompe un mínimo o máximo anterior solo para tomar los Stop Loss antes de revertir su dirección fuertemente.",
            "**VWAP (Volume Weighted Average Price):** El precio medio ponderado por volumen. Sirve como un imán magnético intra-diario para el precio de equilibrio algorítmico."
        ]
        
        # Registro de Usuarios Persistente (Join ID)
        self.user_registry_path = os.path.join(os.path.dirname(__file__), "..", "data", "user_registry.json")
        
        # Instanciar MarketData ligero para los Core Analytics
        from core.market_data import MarketData
        self.market_data = MarketData(exchange_id="kraken")

        # Registro de Performance Persistente
        self.perf_log_path = os.path.join(os.path.dirname(__file__), "..", "data", "performance_log.json")
        
        # Tier-based Coin Access Configuration
        self.COIN_ACCESS = {
            "FREE": ["BTC", "ETH"],
            "CORE": ["BTC", "ETH", "SOL", "XRP", "ADA", "DOT", "MATIC", "NEAR", "AVAX", "LINK", "INJ"],
            "QUANTUM": ["BTC", "ETH", "SOL", "XRP", "ADA", "DOT", "MATIC", "NEAR", "AVAX", "LINK", "INJ", "STX", "FET", "TAO", "RENDER", "FIL", "ICP", "ARB", "OP"]
        }
        
        # Content Library for Deep Dive & Free Analysis rotation
        self.content_library_path = os.path.join(os.path.dirname(__file__), "..", "data", "content_library.json")

    DEEP_DIVE_TAGLINES = [
        "Three dense reads—liquidity, structure, and what could actually move price.",
        "Momentum quality, volatility regimes, and cross-asset beta—desk-style, no hype.",
        "No fluff: funding, levels, and the calendar risks worth respecting.",
        "A fast pass across flow, key zones, and the week’s tripwires.",
        "Tape-first notes: where balance-sheet money hides and where stops cluster.",
        "Cross-asset context in three slices—read once, size risk deliberately.",
    ]
    # Last N VIP deep-dive runs (3 blocks each, split across embeds) must use disjoint block IDs.
    DEEP_DIVE_EMBED_HISTORY = 5
    DEEP_DIVE_RECENT_BLOCK_CAP = DEEP_DIVE_EMBED_HISTORY * 3
    DEEP_DIVE_BLOCKS_PER_EMBED = 2
    # Free channel: last N single-block embeds must not repeat the same block id when avoidable.
    FREE_ANALYSIS_EMBED_HISTORY = 5

    def _get_next_informative_triplet(self) -> list[dict] | None:
        """Picks 3 blocks not used in the last 5 embeds (15 IDs), so nothing repeats across that window."""
        try:
            with open(self.content_library_path, "r", encoding="utf-8") as f:
                library = json.load(f)
        except Exception:
            return None
        blocks = library.get("informative_blocks", [])
        n = len(blocks)
        if n < 3:
            return None
        state = library.setdefault("rotation_state", {})
        raw_recent = state.get("informative_blocks_recent_ids", [])
        recent: list = list(raw_recent) if isinstance(raw_recent, list) else []

        def trio_avoiding(banned_ids: set) -> list[dict] | None:
            candidates = [b for b in blocks if b.get("id") not in banned_ids]
            if len(candidates) >= 3:
                return random.sample(candidates, 3)
            return None

        banned = set(recent)
        trio = trio_avoiding(banned)
        if trio is None and len(recent) > 9:
            trio = trio_avoiding(set(recent[-9:]))
        if trio is None and len(recent) > 3:
            trio = trio_avoiding(set(recent[-3:]))
        if trio is None:
            trio = random.sample(blocks, 3)

        new_ids = [b["id"] for b in trio]
        recent.extend(new_ids)
        if len(recent) > self.DEEP_DIVE_RECENT_BLOCK_CAP:
            recent = recent[-self.DEEP_DIVE_RECENT_BLOCK_CAP :]
        state["informative_blocks_recent_ids"] = recent
        library["rotation_state"] = state
        with open(self.content_library_path, "w", encoding="utf-8") as f:
            json.dump(library, f, indent=4, ensure_ascii=False)
        return trio

    def _get_next_content(self, category: str) -> dict | None:
        """Free analysis: pick a block not used in the last 5 embeds (when the pool allows)."""
        if category != "free_analysis":
            return None
        try:
            with open(self.content_library_path, "r", encoding="utf-8") as f:
                library = json.load(f)
        except Exception:
            return None
        items = library.get("free_analysis", [])
        if not items:
            return None
        state = library.setdefault("rotation_state", {})
        raw = state.get("free_analysis_recent_block_ids", [])
        recent: list = list(raw) if isinstance(raw, list) else []
        cap = self.FREE_ANALYSIS_EMBED_HISTORY
        banned = set(recent[-cap:])
        candidates = [x for x in items if x.get("id") not in banned]
        if not candidates:
            candidates = list(items)
        selected = random.choice(candidates)
        recent.append(selected["id"])
        state["free_analysis_recent_block_ids"] = recent[-cap:]
        library["rotation_state"] = state
        with open(self.content_library_path, "w", encoding="utf-8") as f:
            json.dump(library, f, indent=4, ensure_ascii=False)
        return selected

    def _load_bot_ai_fuse_from_disk(self):
        if not os.path.exists(self._bot_ai_fuse_path):
            return
        try:
            with open(self._bot_ai_fuse_path, encoding="utf-8") as f:
                data = json.load(f)
            today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
            if data.get("utc_date") == today:
                self.daily_ai_count = int(data.get("count", 0))
                self.last_reset_date = datetime.datetime.now(datetime.timezone.utc).date()
        except Exception:
            pass

    def _save_bot_ai_fuse_to_disk(self):
        try:
            os.makedirs(os.path.dirname(self._bot_ai_fuse_path), exist_ok=True)
            payload = {
                "utc_date": self.last_reset_date.isoformat(),
                "count": int(self.daily_ai_count),
            }
            with open(self._bot_ai_fuse_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, ensure_ascii=False)
        except Exception:
            pass

    def _check_ai_quota(self, reservation: bool = False) -> bool:
        """
        Fusible blando diario en proceso (además del reparto por llave en AIEngine).
        Ajusta con SENTINEL_USER_AI_DAILY_CAP / SENTINEL_SYSTEM_AI_DAILY_CAP.
        """
        now = datetime.datetime.now(datetime.timezone.utc).date()
        if now > self.last_reset_date:
            self.daily_ai_count = 0
            self.last_reset_date = now
            self._save_bot_ai_fuse_to_disk()

        user_cap = int(os.getenv("SENTINEL_USER_AI_DAILY_CAP", "200"))
        sys_cap = int(os.getenv("SENTINEL_SYSTEM_AI_DAILY_CAP", "400"))
        limit = sys_cap if reservation else user_cap
        current_val = int(self.daily_ai_count)
        if current_val >= limit:
            return False

        self.daily_ai_count = current_val + 1
        self._save_bot_ai_fuse_to_disk()
        return True

    def _ai_scan_cache_ttl(self) -> float:
        """Ventana ‘fresca’: briefing de primera clase sin nueva llamada a API."""
        return float(os.getenv("AI_SCAN_CACHE_TTL_SEC", "7200"))

    def _ai_scan_cache_soft_ttl(self) -> float:
        """Ventana extendida: mismo texto en caché, sin API; footer distinto (transparente)."""
        mult = float(os.getenv("AI_SCAN_CACHE_SOFT_MULT", "1.75"))
        return max(self._ai_scan_cache_ttl(), self._ai_scan_cache_ttl() * mult)

    def _quantum_scan_cache_hit(self, coin: str) -> tuple[str, str, str | None] | None:
        """
        (text, tier, polish_provider) si hay entrada válida; tier 'fresh' | 'extended'.
        polish_provider: etiqueta auxiliar (ej. groq, grok, groq+grok) o None.
        """
        cu = str(coin).upper()
        cached = self.ai_cache.get(cu)
        if not cached or not isinstance(cached.get("text"), str):
            return None
        text = str(cached["text"]).strip()
        if not text:
            return None
        age = time.time() - float(cached.get("time", 0))
        hard = self._ai_scan_cache_ttl()
        soft = self._ai_scan_cache_soft_ttl()
        prov = cached.get("polish_provider")
        if isinstance(prov, str) and prov.strip():
            polish = prov.strip()
        else:
            polish = "grok" if cached.get("dual_desk") else None
        if age < hard:
            return text, "fresh", polish
        if age < soft:
            return text, "extended", polish
        return None

    def _quantum_ai_scan_api_cooldown_sec(self) -> int:
        """Solo aplica cuando hace falta llamar a Gemini (no si hay caché fresca)."""
        return int(os.getenv("QUANTUM_AI_SCAN_API_COOLDOWN_SEC", "21600"))

    def _load_quantum_scan_cache_from_disk(self):
        if not os.path.exists(self.quantum_scan_cache_path):
            return
        try:
            with open(self.quantum_scan_cache_path, encoding="utf-8") as f:
                raw = json.load(f)
            now = time.time()
            soft = self._ai_scan_cache_soft_ttl()
            for coin, entry in raw.items():
                if not isinstance(entry, dict):
                    continue
                c = str(coin).upper()
                t = float(entry.get("time", 0))
                if now - t >= soft or not entry.get("text"):
                    continue
                row = {"time": t, "text": str(entry["text"])}
                if entry.get("dual_desk") is not None:
                    row["dual_desk"] = bool(entry["dual_desk"])
                if entry.get("polish_provider"):
                    row["polish_provider"] = str(entry["polish_provider"])
                self.ai_cache[c] = row
        except Exception as e:
            print(f"quantum_scan_cache load: {e}")

    def _save_quantum_scan_cache_to_disk(self):
        try:
            os.makedirs(os.path.dirname(self.quantum_scan_cache_path), exist_ok=True)
            now = time.time()
            soft = self._ai_scan_cache_soft_ttl()
            clean = {
                k: v
                for k, v in self.ai_cache.items()
                if isinstance(v, dict) and now - float(v.get("time", 0)) < soft
            }
            self.ai_cache = clean
            with open(self.quantum_scan_cache_path, "w", encoding="utf-8") as f:
                json.dump(clean, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"quantum_scan_cache save: {e}")

    def _load_ai_scan_user_api_times_from_disk(self):
        if not os.path.exists(self.ai_scan_user_api_path):
            return
        try:
            with open(self.ai_scan_user_api_path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                self._ai_scan_user_api_times = {str(k): float(v) for k, v in data.items()}
        except Exception as e:
            print(f"ai_scan_user_api_times load: {e}")

    def _save_ai_scan_user_api_times_to_disk(self):
        try:
            os.makedirs(os.path.dirname(self.ai_scan_user_api_path), exist_ok=True)
            with open(self.ai_scan_user_api_path, "w", encoding="utf-8") as f:
                json.dump(self._ai_scan_user_api_times, f, indent=2)
        except Exception as e:
            print(f"ai_scan_user_api_times save: {e}")

    def _ai_scan_user_api_key(self, user_id: int, coin: str) -> str:
        return f"{user_id}:{str(coin).upper()}"

    def _ai_scan_user_api_blocked(self, user_id: int, coin: str) -> tuple[bool, int]:
        gap = self._quantum_ai_scan_api_cooldown_sec()
        k = self._ai_scan_user_api_key(user_id, coin)
        last = float(self._ai_scan_user_api_times.get(k, 0.0))
        if last <= 0:
            return False, 0
        elapsed = time.time() - last
        if elapsed < gap:
            return True, max(1, int(gap - elapsed))
        return False, 0

    def _record_ai_scan_api_use(self, user_id: int, coin: str):
        self._ai_scan_user_api_times[self._ai_scan_user_api_key(user_id, coin)] = time.time()
        self._save_ai_scan_user_api_times_to_disk()

    def _save_global_cache(self, key: str, text: str):
        """Guarda un texto persistente en el cache global (JSON)"""
        os.makedirs(os.path.dirname(self.global_cache_path), exist_ok=True)
        data = {}
        if os.path.exists(self.global_cache_path):
            with open(self.global_cache_path, "r", encoding="utf-8") as f:
                try: data = json.load(f)
                except: pass
        data[key] = text
        with open(self.global_cache_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4, ensure_ascii=False)

    def _get_or_register_user(self, member: discord.Member) -> int:
        """Asigna o recupera un ID incremental fijo para el usuario en la base de datos local."""
        os.makedirs(os.path.dirname(self.user_registry_path), exist_ok=True)
        registry = {}
        if os.path.exists(self.user_registry_path):
            with open(self.user_registry_path, "r", encoding="utf-8") as f:
                try: registry = json.load(f)
                except: pass
        
        user_id_str = str(member.id)
        if user_id_str in registry:
            return registry[user_id_str]["join_id"]
        
        # Nuevo usuario: Calcular Join ID (Total + 1)
        new_id = len(registry) + 1
        registry[user_id_str] = {
            "name": member.name,
            "join_id": new_id,
            "joined_at": str(datetime.datetime.now())
        }
        
        with open(self.user_registry_path, "w", encoding="utf-8") as f:
            json.dump(registry, f, indent=4)
            
        return new_id

    def _add_safe_field(self, embed: discord.Embed, name: str, value: str, inline: bool = False, format_type: str = ""):
        """Adds a field, splitting it if it exceeds 1000 characters to respect Discord limits."""
        if not value or value == "N/A":
            embed.add_field(name=name, value="N/A", inline=inline)
            return

        # Reserve some space for formatting chars like ```\n \n``` or >
        max_chunk = 1000 
        chunks = [value[i:i+max_chunk] for i in range(0, len(value), max_chunk)]
        
        for i, chunk in enumerate(chunks):
            field_name = name if i == 0 else f"{name} (Part {i+1})"
            if format_type == "code":
                formatted_chunk = f"```\n{chunk}\n```"
            elif format_type == "quote":
                lines = chunk.split('\n')
                formatted_chunk = '\n'.join([f"> {line}" for line in lines if line.strip()])
                if not formatted_chunk: formatted_chunk = "> N/A"
            else:
                formatted_chunk = chunk
                
            # Embed global limit check
            if len(embed) + len(field_name) + len(formatted_chunk) > 5900:
                embed.add_field(name=field_name, value="[Truncated due to Discord limits]", inline=inline)
                break
                
            embed.add_field(name=field_name, value=formatted_chunk, inline=inline)

    @staticmethod
    def _hft_fallback_sentiment(local_type: str) -> str:
        t = (local_type or "").lower()
        if "oversold" in t:
            return "Mean-reversion bias (oversold tape)"
        if "overbought" in t:
            return "Mean-reversion bias (overbought tape)"
        return "Local extreme — desk review"

    def _build_ai_deep_dive_embeds(self, blocks: list[dict], tag: str) -> list[discord.Embed]:
        """Same block fields as before; at most DEEP_DIVE_BLOCKS_PER_EMBED blocks per Discord embed."""
        per = self.DEEP_DIVE_BLOCKS_PER_EMBED
        embeds: list[discord.Embed] = []
        ts = discord.utils.utcnow()
        for i in range(0, len(blocks), per):
            chunk = blocks[i : i + per]
            if i == 0:
                description = tag
            else:
                description = "**AI Deep Dive • Continued**"
            embed = discord.Embed(
                title="🧠 AI Deep Dive",
                description=description,
                color=COLOR_QUANTUM,
                timestamp=ts,
            )
            for b in chunk:
                emoji = b.get("emoji", "▫️")
                title = b.get("title", "Note")
                body = b.get("content", "N/A")
                field_name = f"{emoji} **{title}**"
                self._add_safe_field(embed, field_name, body, inline=False, format_type="")
            embed.set_footer(text="Quantum Hedge Fund Analytics • Educational only")
            embeds.append(embed)
        return embeds

    async def cog_load(self):
        # Iniciar las tareas automáticas
        if not self.daily_bias_task.is_running(): self.daily_bias_task.start()
        if not self.ai_deep_dive_task.is_running(): self.ai_deep_dive_task.start()
        if not self.free_analysis_task.is_running(): self.free_analysis_task.start()
        if not self.unpin_old_logs_task.is_running(): self.unpin_old_logs_task.start()
        if not self.major_signals_task.is_running(): self.major_signals_task.start()
        if not self.order_flow_tracker.is_running(): self.order_flow_tracker.start()
        if not self.major_signals_task.is_running(): self.major_signals_task.start()
        if not self.order_flow_tracker.is_running(): self.order_flow_tracker.start()
        if not self.quantum_signals_task.is_running(): self.quantum_signals_task.start()
        if not self.weekly_performance_report.is_running(): self.weekly_performance_report.start()
        if not self.ai_usage_threshold_alert_task.is_running():
            self.ai_usage_threshold_alert_task.start()

        # Link global tree error handler
        self.bot.tree.on_error = self.on_app_command_error

    async def _generate_welcome_card(self, member: discord.Member, join_id: int = None) -> io.BytesIO:
        """Generates a minimalistic welcome card matching the custom background template."""
        try:
            # 1. Base Canvas (Original Size)
            bg_path = "assets/welcome_bg.png"
            if not os.path.exists(bg_path):
                bg = Image.new('RGB', (1024, 571), (10, 15, 30))
            else:
                bg = Image.open(bg_path).convert('RGBA')

            draw = ImageDraw.Draw(bg)
            
            # 2. Avatar (Profile) at X: 735, Y: 315, Size: 152x152
            avatar_url = member.display_avatar.url
            async with aiohttp.ClientSession() as session:
                async with session.get(avatar_url) as resp:
                    if resp.status == 200:
                        avatar_data = await resp.read()
                        avatar_img = Image.open(io.BytesIO(avatar_data)).convert('RGBA')
                        
                        size = (152, 152)
                        avatar_img = avatar_img.resize(size, Image.LANCZOS)
                        mask = Image.new('L', size, 0)
                        draw_mask = ImageDraw.Draw(mask)
                        draw_mask.ellipse((0, 0) + size, fill=255)
                        
                        output = ImageOps.fit(avatar_img, mask.size, centering=(0.5, 0.5))
                        output.putalpha(mask)
                        
                        # Apply centering offset to X=735, Y=315
                        bg.paste(output, (735 - 76, 315 - 76), output)

            # 3. Typography
            font_bold_path = r"C:\Windows\Fonts\consolab.ttf"  # Consolas Bold (Garantizado en Windows)
            font_reg_path = r"C:\Windows\Fonts\consola.ttf"    # Consolas Regular
            
            # Aumentando tamaños y haciendo el join number Bold para tapar más espacio
            try:
                f_name = ImageFont.truetype(font_bold_path, 51)
                f_join = ImageFont.truetype(font_bold_path, 36)
                f_small = ImageFont.truetype(font_bold_path, 28) 
            except:
                f_name = ImageFont.load_default()
                f_join = ImageFont.load_default()
                f_small = ImageFont.load_default()

            username = member.name.upper()

            # Username at X: 245, Y: 175 (Bajando ligeramente para alinear perfecto con 'Hi,')
            draw.text(xy=(245, 167), text=username + "!", font=f_name, fill=(215, 185, 145), anchor="lm")
            
            # Join text expandido para que se vea más profesional y llene el vacío
            display_id = join_id if join_id is not None else (member.guild.member_count if member.guild else 0)
            
            # Line 1: Welcome message
            line1 = "Thanks for joining us"
            bbox1 = draw.textbbox((168, 295), line1, font=f_join, anchor="lm")
            draw.text(xy=(168, 295), text=line1, font=f_join, fill=(200, 200, 200), anchor="lm")
            
            # Line 2: Member ID (Smaller and Centered relative to line above)
            id_text = f"You're the member #{display_id}"
            center_x = (bbox1[0] + bbox1[2]) / 2
            draw.text(xy=(center_x, 350), text=id_text, font=f_small, fill=(180, 180, 180), anchor="mm")

            # Save to BytesIO
            output_buffer = io.BytesIO()
            bg.save(output_buffer, format='PNG')
            output_buffer.seek(0)
            return output_buffer
            
        except Exception as e:
            print(f"Error generating welcome card: {e}")
            return None

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        """Welcomes users: Immediately with a card, and after 3 minutes with a DM."""
        # --- FILTERS ---
        if member.bot: return # Ignorar bots
        
        # Ignorar si tiene el rango de Admin
        if any(r.id == ADMIN_ROLE_ID for r in member.roles):
            return

        # Registrar o recuperar Join ID
        persistent_id = self._get_or_register_user(member)

        # --- PHASE 1: IMMEDIATE WELCOME CARD ---
        welcome_channel = self.bot.get_channel(WELCOME_CHANNEL_ID)
        if welcome_channel:
            card_buffer = await self._generate_welcome_card(member, join_id=persistent_id)
            if card_buffer:
                file = discord.File(fp=card_buffer, filename=f"welcome_{member.id}.png")
                await welcome_channel.send(content=f"🛰️ **Incoming Transmission... Welcome to the Grid, {member.mention}!**", file=file)
        
        # --- PHASE 2: DELAYED ONBOARDING DM ---
        await asyncio.sleep(180)  # 3 Minute Induction Delay
        
        # Re-fetch member to get updated roles
        try:
            guild = member.guild
            member = await guild.fetch_member(member.id)
        except:
            return # User left during the 3 minutes

        role_ids = [r.id for r in member.roles]
        
        # QUANTUM / LIFETIME
        if 1486476439044755497 in role_ids or 1486476426419900466 in role_ids:
            title = "⚡ Welcome to the Quantum Intelligence Tier"
            color = COLOR_QUANTUM
            content = (
                "Welcome to the pinnacle of Sentinel AI. You now hold the keys to our most advanced HFT validation engine and deep-dive analytics.\n\n"
                "**🧠 Your Quantum Edge:**\n"
                "* **#⚡·quantum-signals:** Real-time signals validated by Sentinel AI. If the AI says 'Active,' the confluence is high.\n"
                "* **#🧠·ai-deep-dive:** Your weekly institutional-grade report. Read it to understand where the big money is moving.\n"
                "* **Command `/ai_scan`:** Your personal analyst on demand. Scan any coin, any time.\n\n"
                "**🔐 Exclusive Benefit:**\n"
                "As a Quantum member, you have priority support. If you have questions about the logic, reach out to @c1vrol directly.\n\n"
                "*You are now ahead of 99% of the market. Trade wisely.*"
            )
        # CORE
        elif 1486476406018936953 in role_ids:
            title = "📈 Welcome to the Core Analytics Tier"
            color = COLOR_CORE
            content = (
                "Congratulations. You’ve just upgraded your market perspective. You are no longer trading based on 'feeling,' but on institutional data.\n\n"
                "**💎 Your New Daily Routine:**\n"
                "1. **08:00 AM EST:** Check <#1486410192093712384> for your Daily Macro Bias.\n"
                "2. **All Day:** Watch <#1486410173193912501> for high-probability swing signals.\n"
                "3. **The Chat:** Join the conversation in <#1486414007324774512> to discuss setups with other Core members.\n\n"
                "**💡 Pro-Tip:**\n"
                "Use the **`/summary`** command anytime to recall the morning's bias without scrolling.\n\n"
                "*Welcome to the elite. Let's dominate the trend.*"
            )
        # BASELINE / FREE TRIAL
        else:
            # We assume users without specific roles or with the Baseline role get this
            title = "🛰️ Welcome to the Sentinel AI Grid"
            color = COLOR_FREE
            content = (
                "Welcome, Trader. You have entered the baseline of our institutional ecosystem. You now have access to our public intelligence tools.\n\n"
                "**🛡️ Your Access Level: [FREE TRIAL]**\n"
                "Currently, you can monitor the market through:\n"
                "* <#1486414055152423063>: See our AI's track record and successful hits.\n"
                "* <#1486410079837094079>: Get a glimpse of what our Quantum engine is detecting.\n"
                "* **Commands:** Use `/price` and `/feargreed` to stay updated.\n\n"
                "**🚀 Level Up**\n"
                "The real edge is in the **Core** and **Quantum** tiers. Don't trade in the dark.\n"
                "Check <#1486410002880139335> to unlock real-time Bias and AI-Validated signals.\n\n"
                "*Sentinel AI: Intelligence is the only edge.*"
            )

        embed = discord.Embed(title=title, description=content, color=color)
        embed.set_footer(text="▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬")
        
        try:
            await member.send(embed=embed)
        except discord.Forbidden:
            # Fallback: Send to the system log channel or a public one if DM is blocked
            # User requested "ephemeral in channel" which is impossible, so we use delete_after
            target_channel = guild.system_channel or guild.text_channels[0]
            if target_channel:
                await target_channel.send(
                    content=f"Welcome {member.mention}! I couldn't send you a DM with your onboarding info, please check this out:",
                    embed=embed,
                    delete_after=300 # 5 minutes visibility
                )

    async def cog_unload(self):
        self.daily_bias_task.cancel()
        self.ai_deep_dive_task.cancel()
        self.free_analysis_task.cancel()
        self.unpin_old_logs_task.cancel()
        self.major_signals_task.cancel()
        self.order_flow_tracker.cancel()
        self.quantum_signals_task.cancel()
        self.weekly_performance_report.cancel()
        if hasattr(self, 'market_data'):
            await self.market_data.close_connection()

    @app_commands.command(name="price", description="Check real-time simulated price.")
    @app_commands.describe(coin="Select the cryptocurrency")
    @app_commands.checks.dynamic_cooldown(market_cooldown_logic)
    async def cmd_price(self, interaction: discord.Interaction, coin: Literal["BTC", "ETH", "SOL", "XRP", "ADA", "DOT", "MATIC", "NEAR", "AVAX", "LINK", "INJ", "STX", "FET", "TAO", "RENDER", "FIL", "ICP", "ARB", "OP"]):
        if not await self._check_coin_access(interaction, coin):
            return

        mock_price = f"${random.uniform(50, 80000):,.2f}"
        
        user_roles = [r.id for r in interaction.user.roles]
        is_quantum = any(r in [ROLE_QUANTUM, ROLE_LIFETIME, 1486476439044755497, 1486476426419900466] for r in user_roles)
        is_core = any(r in [ROLE_CORE, 1486476406018936953] for r in user_roles)
        color = COLOR_QUANTUM if is_quantum else COLOR_CORE if is_core else COLOR_FREE
        
        embed = discord.Embed(
            title=f"📊 Market Price: {coin.upper()}",
            description=f"The current price for **{coin.upper()}/USDT** is approximately **{mock_price}**.",
            color=color,
            timestamp=discord.utils.utcnow()
        )
        embed.set_footer(text="Sentinel AI • Live Data Simulation")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="feargreed", description="Show current market sentiment.")
    @app_commands.checks.dynamic_cooldown(market_cooldown_logic)
    async def cmd_feargreed(self, interaction: discord.Interaction):
        fgi = random.randint(10, 90)
        status = "Extreme Fear" if fgi < 25 else "Fear" if fgi < 45 else "Neutral" if fgi < 55 else "Greed" if fgi < 75 else "Extreme Greed"
        
        user_roles = [r.id for r in interaction.user.roles]
        is_quantum = any(r in [ROLE_QUANTUM, ROLE_LIFETIME, 1486476439044755497, 1486476426419900466] for r in user_roles)
        is_core = any(r in [ROLE_CORE, 1486476406018936953] for r in user_roles)
        color = COLOR_QUANTUM if is_quantum else COLOR_CORE if is_core else COLOR_FREE

        embed = discord.Embed(title="🧭 Fear & Greed Index", description=f"**{fgi}/100** - {status}\n\n*Macro volume remains stable.*", color=color)
        embed.set_footer(text="Market Psychology Analytics")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="upgrade", description="Info on how to get Premium Tiers: Core and Quantum.")
    async def cmd_upgrade(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        
        # Log the upgrade command usage
        log_channel = self.bot.get_channel(UPGRADE_LOG_ID)
        if log_channel:
            embed_log = discord.Embed(
                title="🌟 Upgrade Command Triggered",
                description=f"User {interaction.user.mention} (`{interaction.user.id}`) used `/upgrade`.",
                color=COLOR_CORE,
                timestamp=discord.utils.utcnow()
            )
            try:
                await log_channel.send(embed=embed_log)
            except Exception as e:
                print(f"Error sending upgrade log: {e}")

        try:
            # Intentar obtener el canal (desde caché o fetch)
            channel = self.bot.get_channel(UPGRADE_CHANNEL_ID)
            if not channel:
                channel = await self.bot.fetch_channel(UPGRADE_CHANNEL_ID)
            
            # Recuperar el mensaje específico
            msg = await channel.fetch_message(UPGRADE_MESSAGE_ID)
            
            # Reenviar el contenido y los embeds del mensaje original
            if msg.embeds:
                await interaction.followup.send(content=msg.content if msg.content else None, embeds=msg.embeds, ephemeral=True)
            else:
                await interaction.followup.send(content=msg.content if msg.content else "No information available.", ephemeral=True)
                
        except Exception as e:
            # Fallback en caso de error (canal no visible o mensaje borrado)
            fallback_embed = discord.Embed(
                title="🚀 Sentinel AI: Upgrade Info",
                description="Please visit the official subscription channel to view current tiers and pricing.",
                color=COLOR_CORE
            )
            await interaction.followup.send(embed=fallback_embed, ephemeral=True)
            await self.log_system_error("Upgrade Fetch Failure", f"Could not fetch message {UPGRADE_MESSAGE_ID} in {UPGRADE_CHANNEL_ID}: {e}")

    @app_commands.command(name="levels", description="Show technical support and resistance levels for the day.")
    @app_commands.describe(coin="Select the cryptocurrency to view its levels")
    @app_commands.checks.has_any_role(ROLE_CORE, ROLE_QUANTUM, ROLE_LIFETIME)
    async def cmd_levels(self, interaction: discord.Interaction, coin: Literal["BTC", "ETH", "SOL", "XRP", "ADA", "DOT", "MATIC", "NEAR", "AVAX", "LINK", "INJ", "STX", "FET", "TAO", "RENDER", "FIL", "ICP", "ARB", "OP"]):
        if not await self._check_coin_access(interaction, coin):
            return

        user_roles = [r.id for r in interaction.user.roles]
        is_quantum = any(r in [ROLE_QUANTUM, ROLE_LIFETIME, 1486476439044755497, 1486476426419900466] for r in user_roles)
        color = COLOR_QUANTUM if is_quantum else COLOR_CORE

        embed = discord.Embed(title=f"🧱 Liquidity Map for {coin.upper()}", color=color)
        embed.add_field(name="H4 Resistance", value="🔴 Wall detected", inline=True)
        embed.add_field(name="Weekly VWAP", value="🟡 Equilibrium", inline=True)
        embed.add_field(name="Daily Support", value="🟢 Absorption", inline=True)
        embed.set_footer(text="Sentinel AI • Order Flow Intelligence")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="ai_scan", description="Deep analysis. [Quantum Exclusive]")
    @app_commands.describe(coin="Select the cryptocurrency to scan")
    @app_commands.checks.has_any_role(ROLE_QUANTUM, ROLE_LIFETIME)
    async def cmd_ai_scan(self, interaction: discord.Interaction, coin: Literal["BTC", "ETH", "SOL", "XRP", "ADA", "DOT", "MATIC", "NEAR", "AVAX", "LINK", "INJ", "STX", "FET", "TAO", "RENDER", "FIL", "ICP", "ARB", "OP"]):
        await interaction.response.defer(thinking=True, ephemeral=True)
        
        # QUANTUM/LIFETIME EXCLUSIVE ACCESS CHECK (Extra layer for upsell)
        user_roles = [r.id for r in interaction.user.roles]
        if not any(r in [ROLE_QUANTUM, ROLE_LIFETIME, 1486476439044755497, 1486476426419900466] for r in user_roles):
            embed = discord.Embed(
                title="🔒 Access Denied",
                description="This scan is exclusive to **Sentinel Quantum** members. Upgrade for the full desk workflow.",
                color=0xff0000
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        if not self.ai_engine:
            embed = discord.Embed(
                title="⏳ Sentinel Desk",
                description="We’re reconnecting the analysis channel. **Try again in a moment.**",
                color=COLOR_QUANTUM,
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        # Caché compartida: misma moneda → mismo briefing (fresco o ventana extendida, sin API)
        hit = self._quantum_scan_cache_hit(coin)
        if hit:
            text, tier, polish_tag = hit
            if tier == "fresh":
                foot = "Sentinel Intelligence • Quantum • Live desk cache"
            else:
                foot = "Sentinel Intelligence • Quantum • Extended briefing window (no new API call)"
            if polish_tag:
                foot += f" • Polish ({polish_tag})"
            embed = discord.Embed(
                title=f"🌌 Quantum Scan: {coin.upper()}",
                description=text,
                color=COLOR_QUANTUM,
                timestamp=discord.utils.utcnow(),
            )
            embed.set_footer(text=foot)
            await interaction.followup.send(embed=embed)
            return

        # Sin caché fresca: cooldown por usuario+moneda antes de gastar API (no aplica si arriba hubo caché)
        blocked, retry_sec = self._ai_scan_user_api_blocked(interaction.user.id, coin)
        if blocked:
            if retry_sec < 120:
                human = f"{retry_sec}s"
            elif retry_sec < 3600:
                human = f"~{max(1, retry_sec // 60)} min"
            else:
                human = f"~{retry_sec // 3600}h"
            embed = discord.Embed(
                title="⏳ Fresh scan on cooldown",
                description=(
                    f"Your next **new** desk scan for **{coin.upper()}** is available in **{human}**. "
                    "If another member refreshes this asset first, you’ll receive that same briefing as soon as it’s cached."
                ),
                color=COLOR_QUANTUM,
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        # Fusible diario en proceso (ver también reparto por llave en AIEngine)
        if not self._check_ai_quota(reservation=False):
            embed = discord.Embed(
                title="⏳ High demand",
                description="**Please try again in a few minutes.** Live channels keep updating as usual.",
                color=COLOR_QUANTUM,
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        pair = f"{coin}/USDT"
        gmode = normalize_grok_scan_mode()
        polish_provider: str | None = None
        try:
            snap = await self.market_data.get_ai_snapshot(pair)
            lang_scan = quantum_scan_output_lang(getattr(self.bot, "lang", "en"))
            prompt, sys_instr = AIEngine.build_quantum_scan_prompt(coin, snap, lang=lang_scan)
            resp_text: str | None = None
            gemini_raw_backup: str | None = None
            gemini_used = False

            async def _gem_scan_retry(c: str, s: dict) -> str:
                if not self.ai_engine:
                    return ""
                return await self.ai_engine.generate_quantum_scan_minimal(c, s, lang=lang_scan)

            async def _gem_polish(t: str) -> str:
                if not self.ai_engine:
                    return ""
                return await self.ai_engine.polish_quantum_scan_desk(
                    coin.upper(), t, snap, lang=lang_scan
                )

            gem_retry = _gem_scan_retry if self.ai_engine else None
            gem_polish_fn = _gem_polish if self.ai_engine else None

            if gmode == "always_grok":
                first, prov = await generate_always_grok_or_free_first(
                    coin.upper(), snap, gemini_retry_fn=gem_retry
                )
                if first and first.strip():
                    resp_text = first.strip()
                    polish_provider = prov

            if not resp_text:
                try:
                    response = await self.ai_engine.generate_response(
                        prompt=prompt,
                        system_instruction=sys_instr,
                        mission="USER_VIP",
                    )
                    resp_text = str(response.text or "").strip() or None
                    if resp_text:
                        gemini_used = True
                except Exception as ge:
                    fb, prov = await generate_scan_fallback(
                        coin.upper(), snap, gemini_retry_fn=gem_retry
                    )
                    if fb:
                        resp_text = fb.strip()
                        polish_provider = prov
                    if not resp_text:
                        raise ge

            if not resp_text:
                fb, prov = await generate_scan_fallback(
                    coin.upper(), snap, gemini_retry_fn=gem_retry
                )
                if fb:
                    resp_text = fb.strip()
                    polish_provider = prov

            if gemini_used and resp_text:
                gemini_raw_backup = resp_text.strip() or None
                new_t, prov = await polish_scan_text(
                    resp_text, snap, coin, gemini_polish_fn=gem_polish_fn
                )
                if prov and new_t.strip():
                    resp_text = new_t.strip()
                    polish_provider = prov

            if not resp_text and gemini_raw_backup:
                resp_text = gemini_raw_backup
                polish_provider = polish_provider or "gemini-raw"

            if not resp_text and self.ai_engine:
                try:
                    minimal = await self.ai_engine.generate_quantum_scan_minimal(
                        coin.upper(),
                        snap,
                        lang=lang_scan,
                    )
                    if minimal.strip():
                        resp_text = minimal.strip()
                        polish_provider = (
                            (polish_provider + "+") if polish_provider else ""
                        ) + "gemini-minimal"
                except Exception:
                    pass

            if not resp_text:
                raise RuntimeError("Empty AI response")

            now_ts = time.time()
            self.ai_cache[coin.upper()] = {
                "time": now_ts,
                "text": resp_text,
                "dual_desk": bool(polish_provider),
                "polish_provider": polish_provider or "",
            }
            self._save_quantum_scan_cache_to_disk()
            self._record_ai_scan_api_use(interaction.user.id, coin)

            foot = "Sentinel AI • Educational only • Not financial advice • Quantum"
            if polish_provider:
                foot = (
                    f"Sentinel AI • Desk polish ({polish_provider}) • Educational • Not financial advice"
                )
            embed = discord.Embed(
                title=f"🌌 Quantum Scan: {coin.upper()}",
                description=resp_text,
                color=COLOR_QUANTUM,
                timestamp=discord.utils.utcnow(),
            )
            embed.set_footer(text=foot)
            await interaction.followup.send(embed=embed)
        except Exception as e:
            await self.log_system_error("AI Engine API Error", f"Command /ai_scan failed for {coin}: {e}")
            embed = discord.Embed(
                title="⏳ Couldn’t finish this scan",
                description="**Try again shortly.** If it persists, ping staff.",
                color=COLOR_QUANTUM,
            )
            await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="summary", description="Read the latest saved Daily Macro Bias.")
    @app_commands.checks.has_any_role(ROLE_CORE, ROLE_QUANTUM, ROLE_LIFETIME)
    async def cmd_summary(self, interaction: discord.Interaction):
        try:
            with open(self.global_cache_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                text = data.get("daily_bias", "No Daily Macro Bias saved yet.")
        except:
            text = "No briefing file yet. Await the next broadcast."
        embed = discord.Embed(description=str(text)[:4096], color=COLOR_CORE)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="vip_analysis", description="Read the latest saved Quantum Macro Deep Dive. [Quantum Exclusive]")
    @app_commands.checks.has_any_role(ROLE_QUANTUM, ROLE_LIFETIME)
    async def cmd_vip_analysis(self, interaction: discord.Interaction):
        try:
            with open(self.global_cache_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                raw_cache = data.get("quantum_deep_dive", "{}")
                if isinstance(raw_cache, str):
                    resp_data = json.loads(raw_cache)
                else:
                    resp_data = raw_cache
                
                if not resp_data:
                    raise Exception("Empty Content")

                if isinstance(resp_data, dict) and resp_data.get("version") == 1 and resp_data.get("blocks"):
                    tag = resp_data.get("tag") or random.choice(self.DEEP_DIVE_TAGLINES)
                    embeds = self._build_ai_deep_dive_embeds(list(resp_data["blocks"]), tag)
                    await interaction.response.send_message(embeds=embeds, ephemeral=True)
                    return

                if isinstance(resp_data, dict) and resp_data.get("liquidity"):
                    tag = random.choice(self.DEEP_DIVE_TAGLINES)
                    embed = discord.Embed(title="🧠 AI Deep Dive", description=tag, color=COLOR_QUANTUM)
                    self._add_safe_field(
                        embed, "💧 Liquidity State", resp_data.get("liquidity", "N/A"), inline=False, format_type="code"
                    )
                    self._add_safe_field(
                        embed, "🎯 Critical Zones", resp_data.get("critical_zones", "N/A"), inline=False, format_type="code"
                    )
                    self._add_safe_field(
                        embed, "🔮 Weekly Projection", resp_data.get("weekly_projection", "N/A"), inline=False, format_type="code"
                    )
                    embed.set_footer(text="Quantum Hedge Fund Analytics • Legacy cache • Educational only")
                    await interaction.response.send_message(embed=embed, ephemeral=True)
                    return

                raise Exception("Unknown cache format")
        except Exception as e:
            text = "No saved briefing yet or invalid format. Await the next weekly broadcast."
            
        embed = discord.Embed(title="🧠 AI Deep Dive", description=text, color=COLOR_QUANTUM)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="test_welcome", description="[ADMIN] Trigger a test welcome card in the welcome channel.")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_role(ADMIN_ROLE_ID)
    async def cmd_test_welcome(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        
        welcome_channel = self.bot.get_channel(WELCOME_CHANNEL_ID)
        if not welcome_channel:
            await interaction.followup.send(f"❌ Error: Welcome channel {WELCOME_CHANNEL_ID} not found.", ephemeral=True)
            return

        persistent_id = self._get_or_register_user(interaction.user)
        card_buffer = await self._generate_welcome_card(interaction.user, join_id=persistent_id)
        if card_buffer:
            file = discord.File(fp=card_buffer, filename=f"test_welcome_{interaction.user.id}.png")
            await welcome_channel.send(content=f"🛰️ **TEST TRANSMISSION... Welcome to the Grid, {interaction.user.mention}!**", file=file)
            await interaction.followup.send(f"✅ Test welcome card sent to <#{WELCOME_CHANNEL_ID}>.", ephemeral=True)
        else:
            await interaction.followup.send("❌ Error generating the test card.", ephemeral=True)

    @app_commands.command(name="force_global", description="[ADMIN] Force macro report broadcast.")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_role(ADMIN_ROLE_ID)
    async def cmd_force_global(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=False)
        if not self._check_ai_quota(reservation=True):
            await interaction.followup.send(
                "⚠️ **Capacity reserved for live broadcasts.** Try again later.",
                ephemeral=True,
            )
            return
            
        try:
            btc_s, eth_s = await asyncio.gather(
                self.market_data.get_ai_snapshot("BTC/USDT"),
                self.market_data.get_ai_snapshot("ETH/USDT"),
            )
            prompt, sys_instr = AIEngine.build_admin_macro_prompt(
                btc_s, eth_s, lang=getattr(self.bot, "lang", "en")
            )
            response = await self.ai_engine.generate_response(
                prompt=prompt, system_instruction=sys_instr, mission="ADMIN_FORCE"
            )
            resp_text = str(response.text)
            self._save_global_cache("daily_bias", resp_text)
            
            embed = discord.Embed(title="⚙️ Forced Daily Report (Key 4)", description=resp_text, color=COLOR_QUANTUM)
            embed.set_footer(text="Admin macro note • Not financial advice • Data: BTC/ETH snapshot")
            await interaction.followup.send(embed=embed)
        except Exception as e:
            await interaction.followup.send(f"❌ Force Failed: {e}")

    @app_commands.command(name="force_bias", description="[ADMIN] Force Daily Bias broadcast.")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_role(ADMIN_ROLE_ID)
    async def cmd_force_bias(self, interaction: discord.Interaction):
        await interaction.response.send_message("⚙️ Forcing Daily Bias...", ephemeral=True)
        await self.daily_bias_task()

    @app_commands.command(name="force_deep_dive", description="[ADMIN] Force VIP Deep Dive broadcast.")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_role(ADMIN_ROLE_ID)
    async def cmd_force_deep_dive(self, interaction: discord.Interaction):
        await interaction.response.send_message("⚙️ Forcing /vip_analysis Deep Dive...", ephemeral=True)
        self._force_run = True
        await self.ai_deep_dive_task.coro(self)

    @app_commands.command(name="force_quantum_signal", description="[ADMIN] Force HFT Signal evaluation.")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_role(ADMIN_ROLE_ID)
    async def cmd_force_quantum_signal(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        summary = await self.run_quantum_signals_scan(forced=True)
        posted = summary.get("posted", 0)
        lines = summary.get("lines") or []
        body = "\n".join(lines) if lines else "No detail."
        await interaction.followup.send(
            ephemeral=True,
            content=f"**Quantum signal run** — posted **{posted}** embed(s).\n```{body}```",
        )

    @app_commands.command(name="send_signal", description="[ADMIN] Broadcast manual signal to Quantum.")
    @app_commands.describe(signal="Signal text to transmit")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_role(ADMIN_ROLE_ID)
    async def cmd_send_signal(self, interaction: discord.Interaction, signal: str):
        vip_channel = self.bot.get_channel(QUANTUM_SIGNALS_ID)
        if vip_channel:
            embed = discord.Embed(title="⚡ EMERGENCY INSTITUTIONAL ALERT", description=signal, color=COLOR_QUANTUM)
            await vip_channel.send(embed=embed)
            await interaction.response.send_message("✅ Signal sent to VIP.", ephemeral=True)
        else:
            await interaction.response.send_message("❌ Error: VIP signal channel not available.", ephemeral=True)

    @app_commands.command(name="log_win", description="[ADMIN] Log a profit/trade result with an attachment.")
    @app_commands.describe(
        result="The outcome (e.g., Win, Loss, Breakeven)",
        description="Details about the analysis/trade",
        image="Screenshot of the analysis or profit"
    )
    @app_commands.describe(pnl_pct="Optionally specify the PnL % (e.g., 2.5). Defaults to 1.5 for wins, -0.8 for losses.")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_role(ADMIN_ROLE_ID)
    async def cmd_log_win(self, interaction: discord.Interaction, result: Literal["Win ✅", "Loss ❌", "Breakeven ⏸️"], description: str, pnl_pct: float = None, image: discord.Attachment = None):
        await interaction.response.defer(ephemeral=True)
        wins_channel = self.bot.get_channel(PROFIT_WINS_ID)
        
        if not wins_channel:
            await interaction.followup.send("❌ Error: Profit/Wins channel not found.", ephemeral=True)
            return
            
        color = 0x00ff00 if "Win" in result else 0xff0000 if "Loss" in result else 0xffff00
        embed = discord.Embed(
            title=f"📊 Track Record: {result}",
            description=description,
            color=color,
            timestamp=discord.utils.utcnow()
        )
        embed.set_footer(text="Sentinel AI • Performance Tracker")
        
        if image:
            embed.set_image(url=image.url)
            
        try:
            await wins_channel.send(embed=embed)
            
            # Persist for Performance Stats (with default PnL if not provided)
            if pnl_pct is None:
                pnl_val = 1.5 if "Win" in result else -1.0 if "Loss" in result else 0.0
            else:
                pnl_val = float(pnl_pct)

            await self._record_performance(result, description[:50], pnl=pnl_val)
            
            await interaction.followup.send("✅ Track record logged successfully.", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Failed to log track record: {e}", ephemeral=True)

    async def _check_coin_access(self, interaction: discord.Interaction, coin: str) -> bool:
        """Verifies if the user has the required Tier role for the requested coin."""
        user_roles = [r.id for r in interaction.user.roles]
        
        # Check Roles (Internal + External Fallbacks)
        is_quantum = any(r in [ROLE_QUANTUM, ROLE_LIFETIME, 1486476439044755497, 1486476426419900466] for r in user_roles)
        is_core = any(r in [ROLE_CORE, 1486476406018936953] for r in user_roles)
        
        # Determine User Tier
        tier = "FREE"
        if is_quantum: tier = "QUANTUM"
        elif is_core: tier = "CORE"
        
        # Allowed Coins for User Tier
        allowed = self.COIN_ACCESS.get(tier, ["BTC", "ETH"])
        
        if coin.upper() in allowed:
            return True
            
        # Access Denied: Suggest Upgrade
        if coin.upper() in self.COIN_ACCESS["CORE"]:
            required_tier = "Core"
        else:
            required_tier = "Quantum"
            
        embed = discord.Embed(
            title="🔒 Access Restricted",
            description=f"Monitoring for **{coin.upper()}** is exclusive to **{required_tier}** members.\n\nPlease upgrade your tier in <#{UPGRADE_CHANNEL_ID}> to unlock institutional data for this asset.",
            color=0xff0000
        )
        embed.set_footer(text="Sentinel AI • Privilege Verification")
        
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)
        return False

    async def _record_performance(self, result: str, details: str, pnl: float = 0.0):
        """Helper to store win/loss history for overall performance charts."""
        os.makedirs(os.path.dirname(self.perf_log_path), exist_ok=True)
        history = []
        if os.path.exists(self.perf_log_path):
            with open(self.perf_log_path, "r", encoding="utf-8") as f:
                try: history = json.load(f)
                except: pass
        
        history.append({
            "timestamp": str(datetime.datetime.now()),
            "result": "Win" if "Win" in result else "Loss" if "Loss" in result else "Neut",
            "pnl": float(pnl),
            "details": details
        })
        
        # Keep last 100 records for performance metrics
        if len(history) > 100: history = history[-100:]
        
        with open(self.perf_log_path, "w", encoding="utf-8") as f:
            json.dump(history, f, indent=4)

    @app_commands.command(name="performance", description="View the overall Sentinel AI Track Record chart.")
    async def cmd_performance(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        
        chart_url = await self._generate_total_performance_chart()
        if not chart_url:
            await interaction.followup.send("📊 No performance data available yet. Stay tuned for the next hits.")
            return

        embed = discord.Embed(
            title="🎯 Sentinel AI Performance: Track Record",
            description="Gráfica acumulada de aciertos y tasa de éxito institucional.",
            color=COLOR_QUANTUM,
            timestamp=discord.utils.utcnow()
        )
        embed.set_image(url=chart_url)
        embed.set_footer(text="Verified Performance • Sentinel Intelligence Grid")
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="test_weekly", description="[ADMIN] Force a weekly report and fake test data in profit-wins channel.")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_role(ADMIN_ROLE_ID)
    async def cmd_test_weekly(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        
        # 1. Generate fake data directly in the log with PnL
        fake_history = [
            {"result": "Loss", "pnl": -0.85, "details": "TEST"},
            {"result": "Win", "pnl": 1.42, "details": "TEST"},
            {"result": "Win", "pnl": 2.15, "details": "TEST"},
            {"result": "Loss", "pnl": -1.20, "details": "TEST"},
            {"result": "Win", "pnl": 0.95, "details": "TEST"},
            {"result": "Win", "pnl": 1.64, "details": "TEST"},
            {"result": "Win", "pnl": 3.10, "details": "TEST"}
        ]
        os.makedirs(os.path.dirname(self.perf_log_path), exist_ok=True)
        with open(self.perf_log_path, "w", encoding="utf-8") as f:
            json.dump(fake_history, f, indent=4)
            
        # 2. Trigger an automated win test (ETH)
        await self.bot.log_automated_record(
            symbol="ETH/USDT", 
            entry_price=3000.5, 
            exit_price=3080.0, 
            sentiment="BULLISH", 
            result="Win ✅", 
            history_prices=[2980, 2990, 3000.5, 3020, 3050, 3080]
        )
        
        # 3. Build and send the Weekly Report Grid to the channel
        chart_url = await self._generate_total_performance_chart()
        if chart_url:
            embed = discord.Embed(
                title="📊 WEEKLY PERFORMANCE SUMMARY (TEST)",
                description="Resumen semanal del flujo de aciertos detectados por Sentinel AI.",
                color=COLOR_QUANTUM,
                timestamp=discord.utils.utcnow()
            )
            embed.set_image(url=chart_url)
            embed.set_footer(text="Sentinel AI • Performance Grid Update")
            
            # Send to PROFIT_WINS
            channel = self.bot.get_channel(PROFIT_WINS_ID)
            if channel:
                await channel.send(embed=embed)
                
        await interaction.followup.send("✅ Pruebas enviadas al canal de Profit/Wins.", ephemeral=True)

    async def _generate_total_performance_chart(self) -> str | None:
        """Generates a premium area chart showing cumulative PnL (Equity Curve)."""
        if not os.path.exists(self.perf_log_path):
            return None
            
        with open(self.perf_log_path, "r", encoding="utf-8") as f:
            try: history = json.load(f)
            except: return None
            
        if not history: return None
        
        # Aggregate PnL for an equity-style curve
        equity_curve = [0.0]
        current_pnl = 0.0
        for entry in history:
            pnl_val = entry.get("pnl", 0.0)
            # If no pnl but result is win/loss, use defaults for old logs
            if pnl_val == 0.0:
                res = entry.get("result", "")
                pnl_val = 1.0 if "Win" in res else -1.0 if "Loss" in res else 0.0
            
            current_pnl += pnl_val
            equity_curve.append(round(current_pnl, 2))
            
        labels = [str(i) for i in range(len(equity_curve))]
        
        # Show points only if small dataset
        show_points = 4 if len(equity_curve) < 15 else 0
        
        chart_config = {
            "type": "line",
            "data": {
                "labels": labels,
                "datasets": [{
                    "label": "Cumulative Growth (%)",
                    "data": equity_curve,
                    "borderColor": "rgba(0, 255, 170, 1)",
                    "backgroundColor": "rgba(0, 255, 170, 0.15)",
                    "borderWidth": 3,
                    "pointBackgroundColor": "rgba(0, 255, 170, 1)",
                    "pointRadius": show_points,
                    "fill": True,
                    "tension": 0.3
                }]
            },
            "options": {
                "title": {
                    "display": True, 
                    "text": "QUANTUM PERFORMANCE GRID: CUMULATIVE RETURN (%)", 
                    "fontColor": "#ffffff", 
                    "fontSize": 14
                },
                "legend": {"display": False},
                "scales": {
                    "yAxes": [{
                        "scaleLabel": {
                            "display": True,
                            "labelString": "Net Return (%)",
                            "fontColor": "#a0aec0"
                        },
                        "gridLines": {
                            "color": "rgba(255, 255, 255, 0.05)",
                            "zeroLineColor": "rgba(255, 255, 255, 0.2)"
                        }, 
                        "ticks": {
                            "fontColor": "#a0aec0",
                            "fontFamily": "monospace",
                            "beginAtZero": True
                        }
                    }],
                    "xAxes": [{
                        "scaleLabel": {
                            "display": True,
                            "labelString": "Signals Executed",
                            "fontColor": "#a0aec0"
                        },
                        "display": True, 
                        "ticks": {"display": False},
                        "gridLines": {"display": False}
                    }]
                }
            }
        }
        
        import urllib.parse
        encoded = urllib.parse.quote(json.dumps(chart_config))
        return f"https://quickchart.io/chart?c={encoded}&w=600&h=350&bkg=%230B101E"

    @app_commands.command(name="clear", description="[ADMIN] Deletes a specific amount of messages.")
    @app_commands.describe(amount="Number of messages to delete")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_role(ADMIN_ROLE_ID)
    async def cmd_clear(self, interaction: discord.Interaction, amount: int):
        if amount < 1:
            await interaction.response.send_message("❌ Amount must be at least 1.", ephemeral=True)
            return

        # Defer as ephemeral so no one else sees the process
        await interaction.response.defer(ephemeral=True)
        
        try:
            # Purge channel messages
            deleted = await interaction.channel.purge(limit=amount)
            await interaction.followup.send(f"✅ Deleted **{len(deleted)}** messages.", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Error trying to clear channel: {e}", ephemeral=True)

    @commands.command(name="sync")
    @commands.has_role(ADMIN_ROLE_ID)
    async def sync_guild_slash_commands(self, ctx, mode: str | None = None):
        """[ADMIN] Forces Slash commands synchronization. Use '!sync clear' to remove duplicates."""
        try:
            if mode == "clear":
                # Removes all guild-specific commands (clears duplicates)
                self.bot.tree.clear_commands(guild=ctx.guild)
                synced = await self.bot.tree.sync(guild=ctx.guild)
                await ctx.send(f"✅ Guild commands cleared. Using global definitions only (**{len(synced)}**).", delete_after=10)
            else:
                # Standard Sync: copy global definitions to guild
                self.bot.tree.copy_global_to(guild=ctx.guild)
                synced = await self.bot.tree.sync(guild=ctx.guild)
                await ctx.send(f"✅ Synchronized **{len(synced)}** commands instantly.", delete_after=10)
            
            await ctx.message.delete()
        except Exception as e:
            await ctx.send(f"❌ Sync Error: {e}", delete_after=10)
            await self.log_system_error("Sync Maintenance Error", str(e))

    # 🧠 TAREAS AUTOMÁTICAS
    time_8am_est = datetime.time(hour=13, minute=0, tzinfo=datetime.timezone.utc)
    @tasks.loop(time=time_8am_est)
    async def daily_bias_task(self):
        channel = self.bot.get_channel(DAILY_BIAS_ID)
        if not channel or not self.ai_engine: return
        
        # System Quota check (reservation=True)
        if not self._check_ai_quota(reservation=True):
            print("Daily Bias skipped: no AI quota remaining.")
            return

        try:
            try:
                from zoneinfo import ZoneInfo

                _now_desk = datetime.datetime.now(ZoneInfo("America/New_York"))
            except Exception:
                _now_desk = datetime.datetime.now(datetime.timezone.utc)
            report_date = AIEngine.format_desk_date_english(_now_desk)

            btc_s, eth_s = await asyncio.gather(
                self.market_data.get_ai_snapshot("BTC/USDT"),
                self.market_data.get_ai_snapshot("ETH/USDT"),
            )
            prompt, sys_instr = AIEngine.build_daily_bias_prompt(
                {"BTC": btc_s, "ETH": eth_s},
                report_date,
            )
            response = await self.ai_engine.generate_response(
                prompt=prompt,
                system_instruction=sys_instr,
                mission="SYSTEM_LOOP",
            )
            body = str(response.text or "").strip()
            header = f"**🌅 Daily Macro Bias** • {report_date}"
            full_text = f"{header}\n\n{body}" if body else header
            if len(full_text) > 4096:
                full_text = full_text[:4093] + "..."
            self._save_global_cache("daily_bias", full_text)

            embed = discord.Embed(
                description=full_text,
                color=COLOR_CORE,
                timestamp=discord.utils.utcnow(),
            )
            embed.set_footer(
                text="Comentario educativo de sala • No es asesoramiento de inversión • Verifica los datos"
            )
            await channel.send(embed=embed)
        except Exception as e:
            print(f"Daily Task Error: {e}")
            await self.log_system_error("Daily Bias Failure", str(e))

    time_9am_est = datetime.time(hour=14, minute=0, tzinfo=datetime.timezone.utc)
    @tasks.loop(time=time_9am_est)
    async def ai_deep_dive_task(self):
        is_monday = datetime.datetime.now(datetime.timezone.utc).weekday() == 0
        if not is_monday and not getattr(self, '_force_run', False): return
        self._force_run = False
        
        channel = self.bot.get_channel(AI_DEEP_DIVE_ID)
        if not channel: return

        trio = self._get_next_informative_triplet()
        if not trio:
            print("Deep Dive skipped: informative_blocks needs at least 3 entries.")
            return

        try:
            trio_out = [dict(b) for b in trio]
            de_polish = os.getenv("AI_DEEP_DIVE_POLISH", "").strip().lower() in (
                "1",
                "true",
                "yes",
                "on",
            )
            if (
                de_polish
                and self.ai_engine
                and hasattr(self, "market_data")
                and self.market_data
                and self._check_ai_quota(reservation=True)
            ):
                try:
                    btc_s, eth_s = await asyncio.gather(
                        self.market_data.get_ai_snapshot("BTC/USDT"),
                        self.market_data.get_ai_snapshot("ETH/USDT"),
                    )
                    polished = await self.ai_engine.polish_deep_dive_triplet(
                        trio_out,
                        {"BTC": btc_s, "ETH": eth_s},
                    )
                    if polished and len(polished) == len(trio_out):
                        trio_out = polished
                except Exception as e:
                    print(f"AI_DEEP_DIVE_POLISH skipped: {e}")

            tag = random.choice(self.DEEP_DIVE_TAGLINES)
            embeds = self._build_ai_deep_dive_embeds(trio_out, tag)
            cache_payload = {
                "version": 1,
                "tag": tag,
                "blocks": [
                    {
                        "id": b.get("id"),
                        "emoji": b.get("emoji", "▫️"),
                        "title": b.get("title", "Note"),
                        "content": b.get("content", "N/A"),
                    }
                    for b in trio_out
                ],
            }
            self._save_global_cache("quantum_deep_dive", json.dumps(cache_payload, ensure_ascii=False))
            await channel.send(embeds=embeds)
        except Exception as e:
            print(f"Deep Dive Content Error: {e}")
            await self.log_system_error("Deep Dive Failure", str(e))

    @staticmethod
    def _quantum_local_tape_block(local_signal: dict, px: float) -> str:
        """Monospace-friendly column; first line states which RSI is the signal gate (15m closed)."""
        cond = str(local_signal.get("type", "—"))
        rsi = float(local_signal.get("rsi") or 0)
        v1, v2, v3 = cond, f"{rsi:.1f}", f"${px:,.2f}"
        w = max(len(v1), len(v2), len(v3), 10)
        lw = 28
        banner = "Primary gate: 15m · RSI(14) · last closed bar"
        return (
            f"{banner}\n"
            f"{'Condition:':<{lw}}{v1:>{w}}\n"
            f"{'RSI (primary, 15m):':<{lw}}{v2:>{w}}\n"
            f"{'Reference price (closed):':<{lw}}{v3:>{w}}"
        )

    @staticmethod
    def _quantum_confidence_block(conf_raw: str) -> str:
        """Emoji + bar + short caption — markdown field (not code block)."""
        key = (conf_raw or "Moderate").strip().lower()
        if key == "high":
            emoji, bar = "🟢", "██████████"
            cap = "Stronger alignment between tape and desk read — still hypothetical framing."
        elif key == "low":
            emoji, bar = "🟠", "██░░░░░░░░"
            cap = "Lighter conviction — use as one desk input among many."
        else:
            emoji, bar = "🟡", "█████░░░░░"
            cap = "Balanced read — cross-check with your own process and live liquidity."
        label = (conf_raw or "Moderate").strip()
        return f"{emoji} **{label}**\n`[{bar}]`\n*{cap}*"

    @staticmethod
    def _quantum_emphasize_analysis_terms(text: str) -> str:
        """Bold key desk terms and percentage prints for scannability (markdown field)."""
        if not (text or "").strip():
            return text
        parts = text.split("**")
        terms = (
            "mean-reversion",
            "mean reversion",
            "oversold",
            "overbought",
            "neutral",
            "bullish",
            "bearish",
        )
        for i in range(0, len(parts), 2):
            seg = parts[i]
            seg = re.sub(r"([-+]?\d+\.?\d*%)", r"**\1**", seg)
            for term in terms:
                seg = re.sub(
                    rf"\b({re.escape(term)})\b",
                    lambda m: f"**{m.group(1)}**",
                    seg,
                    flags=re.IGNORECASE,
                )
            parts[i] = seg
        return "**".join(parts)

    @staticmethod
    def _quantum_signal_tape_analysis_copy(local_signal: dict) -> str:
        """Consumer-facing desk copy — no debug or pipeline language."""
        c = (local_signal.get("type") or "").lower()
        if "oversold" in c:
            label = "oversold"
        elif "overbought" in c:
            label = "overbought"
        else:
            label = "mixed"
        rsi_f = float(local_signal.get("rsi") or 0)
        return (
            f"**15m** tape shows an **{label}** conditioning read on **RSI {rsi_f:.1f}** — "
            "the **primary** print (same value as Local Tape). "
            "Hypothetical stress bands bracket the reference print for scenario framing — "
            "confirm live liquidity and your own risk rules."
        )

    @staticmethod
    def _quantum_signal_reason_is_meta(text: str) -> bool:
        low = (text or "").lower()
        if not low.strip():
            return True
        needles = (
            "model validation",
            "forced preview",
            "could not be parsed",
            "unparsed",
            "admin channel",
            "channel test",
            "mechanical stress",
            "mechanical fallback",
            "parse failed",
            "validation deferred",
            "tp/sl failed sanity",
            "desk validation skipped",
            "sanity check",
            "__mechanical_fallback__",
        )
        return any(n in low for n in needles)

    @staticmethod
    def _quantum_signal_desk_read_copy(sentiment: str, forced_preview: bool) -> str:
        s = (sentiment or "").strip() or "Neutral skew"
        if forced_preview:
            return (
                f"{s} — RSI sits outside the usual extreme gate (<30 / >70); "
                "contextual desk snapshot only."
            )
        return f"{s} — consistent with the 15m tape and snapshot inputs."

    async def _dispatch_quantum_signal_embed(
        self,
        channel,
        symbol: str,
        local_signal: dict,
        ai_validation: dict,
        *,
        forced_preview: bool = False,
        mechanical_fallback: bool = False,
    ) -> None:
        tp = float(ai_validation.get("tp", 0.0))
        sl = float(ai_validation.get("sl", 0.0))
        px = float(local_signal["price"])
        pair = symbol.replace("/USDT", "").upper()
        sentiment = (ai_validation.get("sentiment_label") or "").strip() or self._hft_fallback_sentiment(
            local_signal["type"]
        )
        conf = ai_validation.get("confidence") or "Moderate"
        reason_raw = (ai_validation.get("reason") or "").strip()

        header = f"**⚡ Quantum Signal** • {pair}/USDT"
        embed = discord.Embed(
            description=header,
            color=COLOR_QUANTUM,
            timestamp=discord.utils.utcnow(),
        )
        desk_block = self._quantum_signal_desk_read_copy(sentiment, forced_preview)
        tape_block = self._quantum_local_tape_block(local_signal, px)
        levels_block = (
            "Hypothetical stress-test only — not trade instructions\n"
            f"• Anchor ${px:,.2f}  ·  TP ${tp:,.2f}  ·  SL ${sl:,.2f}"
        )
        if mechanical_fallback or self._quantum_signal_reason_is_meta(reason_raw):
            quantum_block = self._quantum_signal_tape_analysis_copy(local_signal)
        else:
            quantum_block = reason_raw or self._quantum_signal_tape_analysis_copy(local_signal)
        quantum_block = self._quantum_emphasize_analysis_terms(quantum_block)
        conf_block = self._quantum_confidence_block(str(conf))
        self._add_safe_field(embed, "📊 **Desk Read**", desk_block, inline=False, format_type="code")
        self._add_safe_field(embed, "📍 **Local Tape**", tape_block, inline=False, format_type="code")
        self._add_safe_field(embed, "🎯 **Reference Bands**", levels_block, inline=False, format_type="code")
        self._add_safe_field(embed, "🧮 **Quantum Analysis**", quantum_block, inline=False, format_type="")
        self._add_safe_field(embed, "⚖️ **Confidence**", conf_block, inline=False, format_type="")
        embed.set_footer(text="Sentinel AI Quantum Engine • Educational only • Not financial advice")
        await channel.send(embed=embed)

    @staticmethod
    def _quantum_forced_preview_mechanical_validation(local_signal: dict) -> dict:
        """
        When admin forces a preview, the model may return VALID:no or unparsable output.
        Mechanical TP/SL (tight band from reference) must pass AIEngine._tp_sl_plausible — test only.
        """
        px = float(local_signal["price"])
        cond = (local_signal.get("type") or "").lower()
        raw_type = local_signal.get("type", "")
        tp, sl = px * 1.01, px * 0.99
        for frac in (0.15, 0.12, 0.08):
            if "oversold" in cond:
                tp, sl = px * (1 + frac), px * (1 - frac)
            else:
                tp, sl = px * (1 - frac), px * (1 + frac)
            if AIEngine._tp_sl_plausible(px, tp, sl, raw_type):
                break
        return {
            "valid": True,
            "tp": tp,
            "sl": sl,
            # Embed copy is supplied in _dispatch_quantum_signal_embed when mechanical_fallback=True.
            "reason": "__mechanical_fallback__",
            "sentiment_label": "Neutral skew",
            "confidence": "Low",
        }

    async def run_quantum_signals_scan(self, *, forced: bool = False) -> dict:
        """
        Returns summary for admin feedback. `forced` uses ETH/USDT only and bypasses the RSI gate
        (synthetic oversold/overbought side) so the channel can be tested without waiting for extremes.
        """
        out: dict = {"posted": 0, "lines": []}
        channel = self.bot.get_channel(QUANTUM_SIGNALS_ID)
        if not channel:
            out["lines"].append("Quantum signals channel not found (check QUANTUM_SIGNALS_ID).")
            return out
        if not hasattr(self, "market_data") or not self.ai_engine:
            out["lines"].append("market_data or ai_engine not ready.")
            return out

        symbols = ["ETH/USDT"] if forced else ["BTC/USDT", "ETH/USDT", "SOL/USDT"]

        for symbol in symbols:
            try:
                raw = await self.market_data.check_hft_signals(symbol, "15m")
                local_signal = dict(raw)
                forced_preview = False

                if not local_signal.get("valid"):
                    if not forced:
                        out["lines"].append(f"{symbol}: skipped (RSI not at gate).")
                        continue
                    rsi_v = local_signal.get("rsi")
                    price_v = local_signal.get("price")
                    if rsi_v is None or price_v is None:
                        out["lines"].append(f"{symbol}: no OHLCV / RSI data.")
                        continue
                    rsi_f = float(rsi_v)
                    local_signal = {
                        "valid": True,
                        "type": "Oversold 🟢" if rsi_f < 50 else "Overbought 🔴",
                        "rsi": rsi_f,
                        "price": float(price_v),
                    }
                    forced_preview = True

                snap = await self.market_data.get_ai_snapshot(symbol)
                ai_validation = await self.ai_engine.evaluate_hft_signal(
                    symbol=symbol,
                    timeframe="15m",
                    rsi=local_signal["rsi"],
                    price=local_signal["price"],
                    condition=local_signal["type"],
                    lang="en",
                    market_context=snap,
                )

                used_mechanical = False
                if not ai_validation.get("valid"):
                    if forced_preview:
                        ai_validation = self._quantum_forced_preview_mechanical_validation(local_signal)
                        used_mechanical = True
                        _log.warning(
                            "quantum forced preview: mechanical fallback for %s (AI invalid/unparsed)",
                            symbol,
                        )
                    else:
                        out["lines"].append(f"{symbol}: AI validation returned invalid.")
                        continue

                await self._dispatch_quantum_signal_embed(
                    channel,
                    symbol,
                    local_signal,
                    ai_validation,
                    forced_preview=forced_preview,
                    mechanical_fallback=used_mechanical,
                )
                out["posted"] += 1
                posted_suffix = ""
                if forced_preview:
                    posted_suffix = " (forced preview"
                    if used_mechanical:
                        posted_suffix += ", mechanical fallback after AI invalid"
                    posted_suffix += ")"
                out["lines"].append(f"{symbol}: posted{posted_suffix}.")
            except Exception as e:
                out["lines"].append(f"{symbol}: error — {e}")
                print(f"quantum_signals_task error [{symbol}]: {e}")

        return out

    @tasks.loop(minutes=15)
    async def quantum_signals_task(self):
        await self.run_quantum_signals_scan(forced=False)

    @tasks.loop(time=time_9am_est)
    async def free_analysis_task(self):
        channel = self.bot.get_channel(FREE_ANALYSIS_ID)
        if not channel: return

        # Rotación desde content_library.json
        content = self._get_next_content("free_analysis")
        if not content:
            print("Free Analysis skipped: No content available in library.")
            return

        try:
            emoji = content.get("emoji", "📌")
            ttl = content.get("title", "Briefing")
            body = content.get("content", "")
            title_line = f"{emoji} {ttl}"
            embed = discord.Embed(
                title="📡 Sentinel Briefing",
                description="*Briefing snapshot—dense by design.*",
                color=COLOR_FREE,
                timestamp=discord.utils.utcnow()
            )
            self._add_safe_field(embed, title_line, body if body else "N/A", inline=False, format_type="code")
            total = 15
            try:
                with open(self.content_library_path, "r", encoding="utf-8") as f:
                    total = len(json.load(f).get("free_analysis", [])) or 15
            except Exception:
                pass
            embed.set_footer(text=f"Block #{content.get('id', '?')} of {total} • /upgrade for live AI analytics")
            await channel.send(embed=embed)
        except Exception as e:
            print(f"Free Analysis Content Error: {e}")

    @tasks.loop(hours=24)
    async def unpin_old_logs_task(self):
        """Unpins critical logs older than 3 days to keep the log channel clean."""
        channel = self.bot.get_channel(LOG_CHANNEL_ID)
        if not channel: return
        
        try:
            pins = await channel.pins()
            now = discord.utils.utcnow()
            unpinned_count = 0
            for msg in pins:
                if (now - msg.created_at).days >= 3:
                    await msg.unpin()
                    unpinned_count += 1
            if unpinned_count > 0:
                print(f"🧹 Maintenance: Unpinned {unpinned_count} old logs.")
        except Exception as e:
            print(f"Error in Maintenance Task: {e}")

    @tasks.loop(hours=6)
    async def ai_usage_threshold_alert_task(self):
        """Avisos al canal admin: suave (~65% del presupuesto) y crítico (~90%), una vez cada uno por día UTC."""
        from core.ai_usage_metrics import (
            mark_usage_critical_alert_sent,
            mark_usage_soft_alert_sent,
            today_tokens_and_cost,
            today_usage_digest,
            token_alert_limits,
            usage_critical_alert_sent_for_today,
            usage_soft_alert_sent_for_today,
        )

        budget, soft_lim, crit_lim = token_alert_limits()
        if budget <= 0:
            return

        tokens, cost_usd = today_tokens_and_cost()
        if tokens < soft_lim:
            return

        try:
            cid = int(os.getenv("AI_USAGE_DISCORD_CHANNEL_ID", "0").strip() or "0")
        except ValueError:
            cid = 0
        if not cid:
            cid = LOG_CHANNEL_ID
        channel = self.bot.get_channel(cid)
        if not channel:
            return

        digest = today_usage_digest()
        bp = digest.get("by_provider") or {}
        pl = digest.get("polish_layers") or {}
        prov_lines = "\n".join(
            f"• **{k}**: llamadas **{v.get('calls', 0)}**, tokens ≈ **{v.get('tokens', 0):,}**"
            for k, v in sorted(bp.items())
        ) or "• (sin desglose aún)"
        pg = int(pl.get("groq", 0) or 0)
        pk = int(pl.get("grok", 0) or 0)
        pz = int(pl.get("gemini", 0) or 0)
        psum = pg + pk + pz
        if psum:
            polish_line = (
                f"Pasadas de polish: Groq **{pg}** ({100 * pg // psum}%) · Grok **{pk}** ({100 * pk // psum}%) · "
                f"Gemini **{pz}** ({100 * pz // psum}%)"
            )
        else:
            polish_line = "Pasadas de polish: aún sin datos (capas `polish_layers` en JSON)."

        base_desc = (
            f"Presupuesto diario (referencia): **{budget:,}** tokens · Suave **{soft_lim:,}** · Crítico **{crit_lim:,}**\n"
            f"Acumulado hoy: **{tokens:,}** · Coste estimado Grok (si aplica): **${cost_usd:.4f}**\n\n"
            f"**Por proveedor**\n{prov_lines}\n\n{polish_line}"
        )

        try:
            if tokens >= crit_lim and not usage_critical_alert_sent_for_today():
                embed = discord.Embed(
                    title="🔴 Uso de IA — crítico (día UTC)",
                    description=base_desc,
                    color=0xE74C3C,
                    timestamp=discord.utils.utcnow(),
                )
                embed.set_footer(
                    text="data/ai_usage_daily.json • AI_USAGE_TOKEN_ALERT_THRESHOLD • AI_USAGE_TOKEN_SOFT_PCT • AI_USAGE_TOKEN_CRITICAL_PCT"
                )
                await channel.send(embed=embed)
                mark_usage_critical_alert_sent()
                mark_usage_soft_alert_sent()
                _log.info(
                    "AI usage CRITICAL (UTC) | tokens=%s budget=%s | by_provider=%s | polish_layers=%s",
                    tokens,
                    budget,
                    bp,
                    pl,
                )
            elif tokens >= soft_lim and not usage_soft_alert_sent_for_today():
                embed = discord.Embed(
                    title="🟡 Uso de IA — aviso (día UTC)",
                    description=base_desc,
                    color=0xF1C40F,
                    timestamp=discord.utils.utcnow(),
                )
                embed.set_footer(
                    text="data/ai_usage_daily.json • AI_USAGE_TOKEN_ALERT_THRESHOLD • AI_USAGE_TOKEN_SOFT_PCT • AI_USAGE_TOKEN_CRITICAL_PCT"
                )
                await channel.send(embed=embed)
                mark_usage_soft_alert_sent()
                _log.info(
                    "AI usage SOFT (UTC) | tokens=%s budget=%s | by_provider=%s | polish_layers=%s",
                    tokens,
                    budget,
                    bp,
                    pl,
                )
        except Exception as e:
            print(f"ai_usage_threshold_alert_task: {e}")

    @tasks.loop(hours=4)
    async def major_signals_task(self):
        channel = self.bot.get_channel(MAJOR_SIGNALS_ID)
        if not channel or not hasattr(self, 'market_data'): return
        
        for symbol in ["BTC/USDT", "ETH/USDT"]:
            try:
                signal = await self.market_data.check_major_signals(symbol)
                if signal:
                    color = 0x00ff00 if signal['type'] == "BULLISH" else 0xff0000
                    if signal["type"] == "BULLISH":
                        sesgo_line = "**Sesgo mayor:** 🟢 **Alcista**"
                        matices = (
                            "**Matices:** Un repunte de riesgo global o pérdida de soportes en timeframes "
                            "superiores podría matizar el sesgo; lectura educativa 4h, sin señales de trading."
                        )
                    else:
                        sesgo_line = "**Sesgo mayor:** 🔴 **Bajista**"
                        matices = (
                            "**Matices:** Sobreventa extrema o reversiones de flujo pueden suavizar la lectura; "
                            "validar con datos propios — contenido educativo únicamente."
                        )
                    embed = discord.Embed(
                        title=f"🎯 Major Signals • {symbol}",
                        description=(
                            f"{sesgo_line}\n\n"
                            f"**Razón:** {signal['reason']}\n\n"
                            f"{matices}"
                        ),
                        color=color,
                        timestamp=discord.utils.utcnow(),
                    )
                    embed.set_footer(text="Sentinel AI • 4H Macro Monitor • Solo educativo")
                    await channel.send(embed=embed)
            except Exception as e:
                print(f"Error en major_signals_task para {symbol}: {e}")

    @tasks.loop(minutes=15)
    async def order_flow_tracker(self):
        channel = self.bot.get_channel(VOLUME_SPIKES_ID)
        if not channel or not hasattr(self, 'market_data'): return
        
        try:
            spike_data = await self.market_data.analyze_volume_spike("BTC/USDT")
            if spike_data:
                volume_multiplier = spike_data['volume'] / spike_data['ma']
                
                # Campos calculados en market_data
                tf = "15m"
                volume_btc = spike_data['volume']
                close_p = spike_data['close']
                open_p = spike_data.get('open', close_p)
                change_pct = spike_data.get('change_pct', 0.0)
                
                dollar_value = volume_btc * close_p
                
                oi_ctx = spike_data.get('oi_context', {})
                oi_trend = oi_ctx.get('trend', 'N/A')
                
                # Construccion del Embed Institucional
                embed = discord.Embed(
                    title=f"🚨 Volume Spike Detectado • {spike_data['direction']}",
                    description=f"**Activo:** BTC/USDT",
                    color=0x8200c9,
                    timestamp=discord.utils.utcnow()
                )
                
                embed.add_field(name="⏱️ Temporalidad", value=f"**TF:** {tf}", inline=True)
                embed.add_field(name="💰 Volumen Real", value=f"**{volume_multiplier:.1f}x**\n{volume_btc:.2f} BTC\n(~${dollar_value:,.0f} USD)", inline=True)
                
                impacto_txt = f"Entrada: ${open_p:,.2f}\nImpacto: {change_pct:+.2f}%"
                absorcion = "\n*(Absorbido / Muro de límite)*" if abs(change_pct) < 0.1 else ""
                embed.add_field(name="🏓 Impacto de Precio", value=f"{impacto_txt}{absorcion}", inline=False)
                
                embed.add_field(name="📊 Contexto Derivados (OI)", value=f"{oi_trend}", inline=False)
                
                embed.set_footer(text="Sentinel Flow Tracker • Institutional Data")
                await channel.send(embed=embed)
        except Exception as e:
            print(f"Error en order_flow_tracker: {e}")

    @tasks.loop(hours=168) # 1 Weekly Summary
    async def weekly_performance_report(self):
        """Sends the cumulative hit record to the Profit Wins channel every week."""
        channel = self.bot.get_channel(PROFIT_WINS_ID)
        if not channel: return
        
        chart_url = await self._generate_total_performance_chart()
        if not chart_url: return
        
        embed = discord.Embed(
            title="📊 WEEKLY PERFORMANCE SUMMARY",
            description="Resumen semanal del flujo de aciertos detectados por Sentinel AI.",
            color=0x00ff00,
            timestamp=discord.utils.utcnow()
        )
        embed.set_image(url=chart_url)
        embed.set_footer(text="Sentinel AI • Performance Grid Update")
        await channel.send(embed=embed)

    # ==========================================
    # 🛡️ EVENT LISTENERS: REACTION ROLES & LOGS
    # ==========================================
    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if payload.channel_id != REACTION_CHANNEL_ID:
            return
        if payload.message_id not in REACTION_MESSAGE_IDS:
            return
        
        guild = self.bot.get_guild(payload.guild_id)
        if not guild:
            return
            
        member = payload.member
        if not member or member.bot:
            return
            
        emoji_name = str(payload.emoji.name)
        
        if emoji_name == "✅":
            # Lógica de Aprobación
            roles_to_add = []
            roles_to_remove = []
            
            # Revisar si tiene el rol de restricción y quitarlo.
            restriction_role = guild.get_role(RESTRICTION_ROLE_ID)
            if restriction_role and restriction_role in member.roles:
                roles_to_remove.append(restriction_role)

            for old_id, new_id in ROLE_TRANSITIONS.items():
                old_role = guild.get_role(old_id)
                if old_role and old_role in member.roles:
                    # Ahora conservamos su rol original (Base), solo le añadimos el nuevo (VIP)
                    new_role = guild.get_role(new_id)
                    if new_role:
                        roles_to_add.append(new_role)
                    if new_role:
                        roles_to_add.append(new_role)
            
            if roles_to_add or roles_to_remove:
                try:
                    await member.remove_roles(*roles_to_remove)
                    await member.add_roles(*roles_to_add)
                    await self._send_log(guild, member, "✅", "Approved", "Roles updated successfully.")
                except Exception as e:
                    print(f"Error actualizando roles tras ✅: {e}")
                    
        elif emoji_name == "❌":
            # Lógica de Denegación
            restriction_role = guild.get_role(RESTRICTION_ROLE_ID)
            
            # Removemos temporalmente los roles VIP si los tiene, para que la penalización aplique,
            # PERO conservamos su rol original (ej. 1486475961590485122) para que el ✅ funcione después.
            roles_to_remove = []
            for new_id in ROLE_TRANSITIONS.values():
                r = guild.get_role(new_id)
                if r and r in member.roles:
                    roles_to_remove.append(r)
            
            try:
                if roles_to_remove:
                    await member.remove_roles(*roles_to_remove)
                
                if restriction_role and restriction_role not in member.roles:
                    await member.add_roles(restriction_role)
                    
                await self._send_log(guild, member, "❌", "Denied", "Restriction role assigned.")
            except Exception as e:
                print(f"Error assigning restriction role after ❌: {e}")

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        if payload.channel_id != REACTION_CHANNEL_ID:
            return
        if payload.message_id not in REACTION_MESSAGE_IDS:
            return
            
        guild = self.bot.get_guild(payload.guild_id)
        if not guild:
            return
            
        member = guild.get_member(payload.user_id)
        if not member:
            try:
                member = await guild.fetch_member(payload.user_id)
            except discord.NotFound:
                return
                
        if not member or member.bot:
            return
            
        emoji_name = str(payload.emoji.name)
        
        if emoji_name == "✅":
            # Lógica de Revocación
            roles_to_add = []
            roles_to_remove = []
            
            for old_id, new_id in ROLE_TRANSITIONS.items():
                new_role = guild.get_role(new_id)
                if new_role and new_role in member.roles:
                    roles_to_remove.append(new_role)
                    old_role = guild.get_role(old_id)
                    if old_role:
                        roles_to_add.append(old_role)
                        
            if roles_to_add or roles_to_remove:
                try:
                    await member.remove_roles(*roles_to_remove)
                    await member.add_roles(*roles_to_add)
                    await self._send_log(guild, member, "✅", "Approval Revoked", "Original roles restored.")
                except Exception as e:
                    error_msg = f"Error revoking roles after removing ✅: {e}"
                    print(error_msg)
                    await self.log_system_error("Role Revocation Error", f"Member: {member.name} | Error: {e}")
                    
        elif emoji_name == "❌":
            # Condición estricta: NO se quita el rol de restricción, y no se envía log.
            pass

    async def _send_log(self, guild, member, emoji, action, details, is_critical=False):
        log_channel = self.bot.get_channel(AUDIT_LOG_ID)
        if not log_channel:
            return
            
        embed = discord.Embed(
            title=f"{'🚨 CRITICAL: ' if is_critical else ''}Audit Log: {action}",
            color=0x800000 if is_critical else (0x2b4c7e if emoji == "✅" else 0x5a6cae),
            timestamp=discord.utils.utcnow()
        )
        
        if member:
            embed.add_field(name="User", value=f"{member.mention} ({member.name})", inline=False)
            embed.add_field(name="User ID", value=str(member.id), inline=True)
        else:
            embed.add_field(name="User", value="System/Internal", inline=False)
            
        embed.add_field(name="Emoji Used", value=emoji, inline=True)
        embed.add_field(name="Final Status", value=details, inline=False)
        embed.set_footer(text="Sentinel AI • Security Logs")
        
        try:
            message = await log_channel.send(embed=embed)
            if is_critical:
                await message.pin()
        except Exception as e:
            print(f"Error sending log: {e}")

    async def log_system_error(self, action, details):
        """Helper to log critical system errors not tied to a specific member."""
        log_channel = self.bot.get_channel(LOG_CHANNEL_ID)
        if not log_channel: return
        
        embed = discord.Embed(
            title=f"🚨 SYSTEM CRITICAL: {action}",
            description=details,
            color=0x800000,
            timestamp=discord.utils.utcnow()
        )
        embed.set_footer(text="Sentinel AI • System Monitor")
        try:
            msg = await log_channel.send(embed=embed)
            await msg.pin()
        except Exception as e:
            print(f"Error logging system issue: {e}")

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        roles_before = set([r.id for r in before.roles])
        roles_after = set([r.id for r in after.roles])
        
        # Detectar si un bot externo removió algún rol
        removed_roles = roles_before - roles_after
        
        roles_to_remove = []
        for old_id in removed_roles:
            if old_id in ROLE_TRANSITIONS:
                # El usuario perdió el rol base (ej. expiró suscripción Whop/externa)
                new_id = ROLE_TRANSITIONS[old_id]
                new_role = after.guild.get_role(new_id)
                if new_role and new_role in after.roles:
                    roles_to_remove.append(new_role)
                    
        if roles_to_remove:
            try:
                await after.remove_roles(*roles_to_remove)
                await self._send_log(after.guild, after, "🤖", "Access Expired", "Base role was removed externally. VIP role automatically revoked.")
            except Exception as e:
                print(f"Error removing expired VIP roles: {e}")

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        details = f"Member {member.name} left the server."
        await self._send_log(member.guild, member, "👋", "User Left", details)

    @commands.Cog.listener()
    async def on_app_command_completion(self, interaction: discord.Interaction, command: app_commands.Command | app_commands.ContextMenu):
        """Logs successful slash command executions to specific Audit channels."""
        if command.name == "upgrade":
            return  # Upgrade already has its own dedicated log channel

        is_admin = any(r.id == ADMIN_ROLE_ID for r in interaction.user.roles)
        target_channel_id = 1486805412215783494 if is_admin else 1486806720830509106
        log_channel = self.bot.get_channel(target_channel_id)

        if log_channel:
            embed = discord.Embed(
                title=f"Audit Log: Command Executed",
                color=0x800000 if is_admin else 0x5a6cae,
                timestamp=discord.utils.utcnow()
            )
            embed.add_field(name="User", value=f"{interaction.user.mention} ({interaction.user.name})", inline=False)
            embed.add_field(name="User ID", value=str(interaction.user.id), inline=True)
            embed.add_field(name="Emoji Used", value="⚡", inline=True)
            embed.add_field(name="Final Status", value=f"The user successfully executed the slash command `/{command.name}` in {interaction.channel.mention}.", inline=False)
            embed.set_footer(text="Sentinel AI • Security Logs")
            
            try:
                await log_channel.send(embed=embed)
            except Exception as e:
                print(f"Error sending command execution log: {e}")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """Auto-React to human screenshots in the profit-wins channel for social proof."""
        if message.author.bot:
            return
            
        # MARKET CHAT MODERATION
        MARKET_CHAT_ID_LOCAL = 1486414007324774512
        if message.channel.id == MARKET_CHAT_ID_LOCAL:
            content_lower = message.content.lower()
            risk_keywords = ["liquidado", "liquidad", "crash", "pérdida", "perdi", "quemada"]
            if any(word in content_lower for word in risk_keywords):
                try:
                    await message.reply("🛡️ **Sentinel Risk Management:** Recuerda nunca arriesgar más del 1-2% de tu capital por trade. El mercado siempre da nuevas oportunidades. Protege tu capital.")
                except Exception as e:
                    print(f"Error respondiendo en market chat: {e}")
                    
        if message.channel.id == PROFIT_WINS_ID and message.attachments:
            emojis = ["🔥", "🚀", "💰"]
            for emoji in emojis:
                try:
                    await message.add_reaction(emoji)
                except Exception as e:
                    print(f"Failed to add reaction {emoji}: {e}")

    # ==========================================
    # 🛡️ GLOBAL APP COMMAND ERROR HANDLER
    # ==========================================
    async def on_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        """Centralized cool-down and error handling for all Slash commands."""
        if isinstance(error, app_commands.CommandOnCooldown):
            retry_after = int(error.retry_after)
            if retry_after >= 3600:
                time_str = f"{max(1, retry_after // 3600)} hours"
            elif retry_after >= 60:
                time_str = f"{max(1, retry_after // 60)} minutes"
            else:
                time_str = f"{retry_after} seconds"
            
            return await interaction.response.send_message(
                f"⏳ **Cooldown Active**: Please wait **{time_str}** before using this command again.",
                ephemeral=True
            )

        if isinstance(error, app_commands.MissingAnyRole):
            roles_str = " or ".join([f"<@&{r}>" for r in error.missing_roles])
            return await interaction.response.send_message(
                f"❌ **Access Denied**: You are missing at least one of the required roles: {roles_str}",
                ephemeral=True
            )

        if isinstance(error, app_commands.MissingRole):
            return await interaction.response.send_message(
                f"❌ **Access Denied**: You need the role <@&{error.missing_role}> to use this command.",
                ephemeral=True
            )
        
        # Log other errors to console
        print(f"AppCommand Error: {error}")
        if not interaction.response.is_done():
            # Avoid sending message if already deferred or answered
            try:
                await interaction.response.send_message(f"❌ An error occurred: {error}", ephemeral=True)
            except discord.InteractionResponded:
                await interaction.followup.send(f"❌ An error occurred: {error}", ephemeral=True)


# ==========================================
# 🚀 CLASE PRINCIPAL EXPORTADA
# ==========================================
class DiscordBotClient(commands.Bot):
    def __init__(self, channel_id=None, ai_engine=None):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True # REQUERIDO: Para detectar on_member_join
        super().__init__(command_prefix="!", intents=intents)
        self.channel_id = channel_id
        self.ai_engine = ai_engine
        # Ensure internal language is set to English
        self.lang = "en"

    async def setup_hook(self):
        # Cargamos el Cog que contiene todas las funciones deseadas
        await self.add_cog(SentinelCog(self, self.ai_engine))
        try:
            synced = await self.tree.sync()
            print(f"✅ Synchronized {len(synced)} slash commands.")
        except Exception as e:
            print(f"❌ Error syncing commands: {e}")
            await self.log_system_error("Sync Failure", str(e))

    async def on_ready(self):
        print(f'✅ Sentinel AI System initialized as {self.user}')

    # El método send_alert se requiere por la tarea market_monitor de main.py
    async def send_alert(self, symbol, price_action, sentiment_data, ai_insight=None, second_read_note=None):
        channel = self.get_channel(self.channel_id)
        if channel:
            # print(f"🔔 Sending alert generated by main.py to {channel.name}...") 
            # (Silenced for cleaner telemetry)
            
            color = 0xff0000 if "Bear" in sentiment_data.get('sentiment', '') else 0x00ff00
            embed = discord.Embed(
                title=f"🚨 CONFLUENCE ALERT: {symbol}",
                description=f"Validated Sentiment: **{sentiment_data.get('sentiment')}** ({sentiment_data.get('confidence')})",
                color=color
            )
            embed.add_field(name="Close Price", value=f"${price_action.get('close_price', 0):,.2f}", inline=True)
            embed.add_field(name="24h Change", value=f"{price_action.get('change_pct', 0):+.2f}%", inline=True)
            
            if ai_insight:
                embed.add_field(name="🧠 Desk read", value=f"*{ai_insight}*", inline=False)
            if second_read_note:
                embed.add_field(name="⚡ Desk second read", value=second_read_note[:1024], inline=False)
            
            foot = "Sentinel AI • Elite Analytics"
            if second_read_note:
                foot += " • Second read on strong move"
            embed.set_footer(text=foot)
            await channel.send(embed=embed)
            
            # --- TIER FREE: ALERTA CENSURADA (MAX 1 VEZ AL DÍA) ---
            trial_channel = self.get_channel(TRIAL_ALERTS_ID)
            cog = self.get_cog("SentinelCog")
            if trial_channel and cog:
                now_date = datetime.datetime.now(datetime.timezone.utc).date()
                if cog.last_censored_alert_date != now_date:
                    cog.last_censored_alert_date = now_date
                    
                    blur_embed = discord.Embed(
                        title="🚨 ALERTA INSTITUCIONAL DETECTADA",
                        description="Sentinel AI ha detectado una zona de entrada perfecta y alta volatilidad inminente en **[MONEDA OCULTA]**.\n\nLos miembros VIP ya han recibido los niveles exactos de entrada, Take Profit y Stop Loss. Moverse rápido es clave.",
                        color=COLOR_FREE,
                        timestamp=discord.utils.utcnow()
                    )
                    blur_embed.set_footer(text="Usa /upgrade para recibir estas alertas reales en tiempo real.")
                    try:
                        await trial_channel.send(embed=blur_embed)
                    except Exception as e:
                        print(f"Error enviando Trial Alert (Censurada): {e}")

    async def log_automated_record(self, symbol: str, entry_price: float, exit_price: float, sentiment: str, result: str, history_prices: list):
        """Automatically logs a win/loss record to the profit channel with a generated chart."""
        channel_id = PROFIT_WINS_ID  # Use the constant defined at the top
        wins_channel = self.get_channel(channel_id)
        if not wins_channel:
            return

        # Calculate PnL percentage
        if sentiment.upper() == "BULLISH":
            pnl = ((exit_price - entry_price) / entry_price) * 100
        else:
            pnl = ((entry_price - exit_price) / entry_price) * 100

        color = 0x00ff00 if "Win" in result else 0xff0000 if "Loss" in result else 0xffff00
        
        embed = discord.Embed(
            title=f"🤖 Sentinel Auto-Track: {symbol} - {result}",
            description=f"Automated trade resolution for **{sentiment}** signal.",
            color=color,
            timestamp=discord.utils.utcnow()
        )
        embed.add_field(name="Entry Price", value=f"${entry_price:,.2f}", inline=True)
        embed.add_field(name="Exit Price", value=f"${exit_price:,.2f}", inline=True)
        embed.add_field(name="Net PnL", value=f"{pnl:+.2f}%", inline=True)
        
        # Generate a lightweight QuickChart URL for the "screenshot"
        if history_prices:
            # Sample max 20 data points for cleaner chart
            step = max(1, len(history_prices) // 20)
            sampled = history_prices[::step][-20:]
            labels = [str(i) for i in range(len(sampled))]
            
            line_color = "rgb(0, 255, 0)" if "Win" in result else "rgb(255, 0, 0)"
            chart_config = {
                "type": "line",
                "data": {
                    "labels": labels,
                    "datasets": [{
                        "label": f"{symbol} Price Action",
                        "data": sampled,
                        "borderColor": line_color,
                        "backgroundColor": "rgba(0, 255, 0, 0.1)" if "Win" in result else "rgba(255, 0, 0, 0.1)",
                        "borderWidth": 2,
                        "pointRadius": 0,
                        "fill": True
                    }]
                },
                "options": {
                    "legend": {"display": False},
                    "scales": {
                        "xAxes": [{"display": False}],
                        "yAxes": [{"gridLines": {"display": False}, "ticks": {"fontColor": "#888"}}]
                    }
                }
            }
            encoded_config = urllib.parse.quote(json.dumps(chart_config))
            chart_url = f"https://quickchart.io/chart?c={encoded_config}&w=400&h=200&bkg=%230a0f1e"
            embed.set_image(url=chart_url)
            
        embed.set_footer(text="Sentinel AI • Autonomous Validation")
        
        try:
            await wins_channel.send(embed=embed)
            # Log for Global Stats via Cog
            cog = self.get_cog("SentinelCog")
            if cog:
                await cog._record_performance(result, f"{symbol} trade", pnl=pnl)
        except Exception as e:
            print(f"Failed to send auto-record: {e}")
            
        # --- TIER FREE: ALERTA RETRASADA (MAX 1 VEZ AL DÍA Y SOLO WINS) ---
        if "Win" in result:
            trial_channel = self.get_channel(TRIAL_ALERTS_ID)
            cog = self.get_cog("SentinelCog")
            if trial_channel and cog:
                now_date = datetime.datetime.now(datetime.timezone.utc).date()
                if cog.last_delayed_alert_date != now_date:
                    cog.last_delayed_alert_date = now_date
                    
                    delayed_embed = discord.Embed(
                        title="✅ TRACK RECORD: SEÑAL COMPLETADA",
                        description=f"Esta señal se envió hoy a los usuarios Quantum.\n\n**Resultado:** {pnl:+.2f}% 🚀\n\nActualiza tu plan para recibir las próximas oportunidades en vivo antes de que ocurra el movimiento.",
                        color=0x00ff00,
                        timestamp=discord.utils.utcnow()
                    )
                    delayed_embed.set_footer(text="Sentinel AI • Quick Recap")
                    try:
                        await trial_channel.send(embed=delayed_embed)
                    except Exception as e:
                        print(f"Error enviando Trial Alert (Retrasada): {e}")
