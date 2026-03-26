import discord
from discord.ext import commands, tasks
from discord import app_commands
from google import genai
import asyncio
import datetime
import random
import os
import json
import time
import io
from PIL import Image, ImageDraw, ImageFont, ImageOps, ImageFilter
import aiohttp
import psutil
from typing import Literal

# ==========================================
# 🗺️ MAPA DE CANALES Y COLORES
# ==========================================
TRIAL_ALERTS_ID = 1486410079837094079
FREE_ANALYSIS_ID = 1486410103627190443
MAJOR_SIGNALS_ID = 1486410173193912501
DAILY_BIAS_ID = 1486410192093712384
ORDER_FLOWS_ID = 1486410214604275822
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
            
        # Global Quota Guard (Safety counter per session)
        self.daily_ai_count = 0
        self.last_reset_date = datetime.datetime.now(datetime.timezone.utc).date()
        
        # Memoria y Caché
        self.ai_cache = {}  # {'BTC': {'time': float, 'text': str}}
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

    def _check_ai_quota(self, reservation: bool = False) -> bool:
        """Protects the daily 100-req limit. 
        Limits: User (30 max), System/VIP overrides (90 hardcap).
        """
        now = datetime.datetime.now(datetime.timezone.utc).date()
        if now > self.last_reset_date:
            self.daily_ai_count = 0
            self.last_reset_date = now
            
        limit = 90 if reservation else 30
        current_val = int(self.daily_ai_count)
        if current_val >= limit:
            return False
            
        self.daily_ai_count = current_val + 1
        return True

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
        if hasattr(self, 'market_data'):
            await self.market_data.close_connection()

    @app_commands.command(name="price", description="Check real-time simulated price.")
    @app_commands.describe(coin="Select the cryptocurrency")
    @app_commands.checks.dynamic_cooldown(market_cooldown_logic)
    @app_commands.checks.has_any_role(ROLE_CORE, ROLE_QUANTUM, ROLE_LIFETIME)
    async def cmd_price(self, interaction: discord.Interaction, coin: Literal["BTC", "ETH", "SOL", "XRP", "ADA"]):
        mock_price = f"${random.uniform(50, 80000):,.2f}"
        embed = discord.Embed(title=f"📈 Current Price for **{coin.upper()}**", description=f"Detected Value: `{mock_price}`", color=COLOR_FREE)
        embed.set_footer(text="💡 Tip: Access /ai_scan for fundamental analysis today.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="feargreed", description="Show current market sentiment.")
    @app_commands.checks.dynamic_cooldown(market_cooldown_logic)
    @app_commands.checks.has_any_role(ROLE_CORE, ROLE_QUANTUM, ROLE_LIFETIME)
    async def cmd_feargreed(self, interaction: discord.Interaction):
        fgi = random.randint(10, 90)
        status = "Extreme Fear" if fgi < 25 else "Fear" if fgi < 45 else "Neutral" if fgi < 55 else "Greed" if fgi < 75 else "Extreme Greed"
        
        embed = discord.Embed(title="🧭 Fear & Greed Index", description=f"**{fgi}/100** - {status}\n\n*Macro volume remains stable.*", color=COLOR_CORE)
        embed.set_footer(text="💡 Tip: Access /ai_scan for fundamental analysis today.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="upgrade", description="Info on how to get Premium Tiers: Core and Quantum.")
    async def cmd_upgrade(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=False)
        
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
                await interaction.followup.send(content=msg.content if msg.content else None, embeds=msg.embeds, ephemeral=False)
            else:
                await interaction.followup.send(content=msg.content if msg.content else "No information available.", ephemeral=False)
                
        except Exception as e:
            # Fallback en caso de error (canal no visible o mensaje borrado)
            fallback_embed = discord.Embed(
                title="🚀 Sentinel AI: Upgrade Info",
                description="Please visit the official subscription channel to view current tiers and pricing.",
                color=COLOR_CORE
            )
            await interaction.followup.send(embed=fallback_embed, ephemeral=False)
            await self.log_system_error("Upgrade Fetch Failure", f"Could not fetch message {UPGRADE_MESSAGE_ID} in {UPGRADE_CHANNEL_ID}: {e}")

    @app_commands.command(name="levels", description="Show technical support and resistance levels for the day.")
    @app_commands.describe(coin="Select the cryptocurrency to view its levels")
    @app_commands.checks.has_any_role(ROLE_CORE, ROLE_QUANTUM, ROLE_LIFETIME)
    async def cmd_levels(self, interaction: discord.Interaction, coin: Literal["BTC", "ETH", "SOL", "XRP", "ADA"]):
        embed = discord.Embed(title=f"🧱 Liquidity Map for {coin.upper()}", color=COLOR_CORE)
        embed.add_field(name="H4 Resistance", value="🔴 Wall detected", inline=True)
        embed.add_field(name="Weekly VWAP", value="🟡 Equilibrium", inline=True)
        embed.add_field(name="Daily Support", value="🟢 Absorption", inline=True)
        embed.set_footer(text="💡 Tip: Access /ai_scan for fundamental analysis today.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="ai_scan", description="Deep analysis. [Quantum Exclusive]")
    @app_commands.describe(coin="Select the cryptocurrency to scan")
    @app_commands.checks.has_any_role(ROLE_QUANTUM, ROLE_LIFETIME)
    async def cmd_ai_scan(self, interaction: discord.Interaction, coin: Literal["BTC", "ETH", "SOL", "AVAX", "LINK", "INJ"]):
        await interaction.response.defer(thinking=True, ephemeral=True)
        
        # QUANTUM/LIFETIME EXCLUSIVE ACCESS CHECK
        if not any(r.id in [ROLE_QUANTUM, ROLE_LIFETIME] for r in interaction.user.roles):
            await interaction.followup.send("🔒 **Acceso Denegado:** `/ai_scan` ahora es EXCLUSIVO del Plan Quantum para proteger las cuotas de IA. Por favor realiza un `/upgrade`.", ephemeral=True)
            return

        if not self.ai_engine:
            await interaction.followup.send("❌ Error: AI engine not configured.", ephemeral=True)
            return

        # SMART CACHE CHECK (60 Minutes TTL) - Zero Cost
        import time
        cached = self.ai_cache.get(coin)
        if cached and (time.time() - cached['time']) < 3600:
            embed = discord.Embed(title=f"🌌 Quantum Scan: {coin.upper()} [CACHED]", description=cached['text'], color=COLOR_QUANTUM, timestamp=discord.utils.utcnow())
            embed.set_footer(text="Powered by Sentinel AI • Zero-Cost Memory")
            await interaction.followup.send(embed=embed)
            return

        # Global Quota Check (Safety guard before rotation, User Limit: 30)
        if not self._check_ai_quota(reservation=False):
            await interaction.followup.send("🚨 **System Load at Max**: Cuota de usuarios VIP agotada por hoy. Los sistemas de alerta en vivo operan con normalidad.", ephemeral=True)
            return

        prompt = f"HFT Analyst. Scan {coin}. <60 words."
        try:
            # USAR Misión USER_VIP (Llave 3)
            response = await self.ai_engine.generate_response(
                prompt=prompt,
                mission="USER_VIP"
            )
            resp_text = str(response.text)
            self.ai_cache[coin] = {'time': time.time(), 'text': resp_text}
            
            embed = discord.Embed(title=f"🌌 Quantum Scan: {coin.upper()}", description=resp_text, color=COLOR_QUANTUM, timestamp=discord.utils.utcnow())
            embed.set_footer(text="Powered by Sentinel AI • Quantum Tier")
            await interaction.followup.send(embed=embed)
        except Exception as e:
            error_details = f"AI Generation Failed: {e}"
            await interaction.followup.send(f"❌ {error_details}", ephemeral=True)
            await self.log_system_error("AI Engine API Error", f"Command /ai_scan failed for {coin}: {e}")

    @app_commands.command(name="summary", description="Read the persistently cached Daily Macro Bias. Zero Cost.")
    @app_commands.checks.has_any_role(ROLE_CORE, ROLE_QUANTUM, ROLE_LIFETIME)
    async def cmd_summary(self, interaction: discord.Interaction):
        try:
            with open(self.global_cache_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                text = data.get("daily_bias", "No Daily Bias cached yet.")
        except:
            text = "Cache file not created. Await the next broadcast."
        embed = discord.Embed(title="📊 Daily Macro Bias (Persistent Cache)", description=text, color=COLOR_CORE)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="vip_analysis", description="Read the persistently cached Quantum Deep Dive. [Quantum Exclusive]")
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
                    
                embed = discord.Embed(title="🏦 Sentinel Macro Deep Dive (Offline Cache)", description="Reporte Institucional Semanal recuperado de la caché HFT.", color=COLOR_QUANTUM)
                self._add_safe_field(embed, "💧 Liquidity State", resp_data.get('liquidity', 'N/A'), inline=False, format_type="quote")
                self._add_safe_field(embed, "🎯 Critical Zones", resp_data.get('critical_zones', 'N/A'), inline=False, format_type="quote")
                self._add_safe_field(embed, "🔮 Weekly Projection", resp_data.get('weekly_projection', 'N/A'), inline=False, format_type="quote")
                embed.set_footer(text="Premium Quantum Level • Zero Cost Access")
                
                await interaction.response.send_message(embed=embed, ephemeral=True)
                return
        except Exception as e:
            text = "Cache empty or invalid format. Await the next weekly broadcast."
            
        embed = discord.Embed(title="🌌 Quantum Deep Dive", description=text, color=COLOR_QUANTUM)
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

    @app_commands.command(name="force_global", description="[ADMIN] Force report consuming critical RPD quota.")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_role(ADMIN_ROLE_ID)
    async def cmd_force_global(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=False)
        if not self._check_ai_quota(reservation=True):
            await interaction.followup.send("⚠️ Critical Block: Global RPD at >=90. Emergency Shutdown active.")
            return
            
        try:
            prompt = "Quick global crypto macro HFT summary. Return 50 words. End with emoji."
            response = await self.ai_engine.generate_response(prompt=prompt, mission="ADMIN_FORCE")
            resp_text = str(response.text)
            self._save_global_cache("daily_bias", resp_text)
            
            embed = discord.Embed(title="⚙️ Forced Daily Report (Key 4)", description=resp_text, color=COLOR_QUANTUM)
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
        await interaction.response.send_message("⚙️ Forcing Quantum HFT Signals scan...", ephemeral=True)
        await self.quantum_signals_task.coro(self)

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
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_role(ADMIN_ROLE_ID)
    async def cmd_log_win(self, interaction: discord.Interaction, result: Literal["Win ✅", "Loss ❌", "Breakeven ⏸️"], description: str, image: discord.Attachment = None):
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
            await interaction.followup.send("✅ Track record logged successfully.", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Failed to log track record: {e}", ephemeral=True)

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
            print("Daily Bias skipped: No AI quota remaining.")
            return

        try:
            prompt = "Write today's market macro sentiment. Max 50 words. End with SENTINEL VERDICT EMOJI 🟢🔴🟡."
            # USAR Misión SYSTEM_LOOP (Clave 5)
            response = await self.ai_engine.generate_response(
                prompt=prompt,
                mission="SYSTEM_LOOP"
            )
            resp_text = str(response.text)
            self._save_global_cache("daily_bias", resp_text)
            
            embed = discord.Embed(title="📊 Daily Macro Bias", description=resp_text, color=COLOR_CORE, timestamp=discord.utils.utcnow())
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
        if not channel or not self.ai_engine: return

        # System Quota check (reservation=True)
        if not self._check_ai_quota(reservation=True):
            print("Deep Dive skipped: No AI quota remaining.")
            return

        try:
            prompt = 'Generate a hedge fund style weekly macro deep dive for crypto (Liquidity, Critical Zones, Weekly Projection). Return ONLY strict JSON: {"liquidity":"...", "critical_zones":"...", "weekly_projection":"..."}. Keep each section of the JSON response under 800 characters to ensure compatibility with Discord Embed limits.'
            # USAR Misión SYSTEM_LOOP (Clave 5)
            response = await self.ai_engine.generate_response(
                prompt=prompt,
                mission="SYSTEM_LOOP",
                response_mime_type="application/json"
            )
            resp_data = json.loads(str(response.text))
            self._save_global_cache("quantum_deep_dive", json.dumps(resp_data))
            
            embed = discord.Embed(title="🏦 Sentinel Macro Deep Dive", description="Reporte Institucional Semanal", color=COLOR_QUANTUM, timestamp=discord.utils.utcnow())
            self._add_safe_field(embed, "💧 Liquidity State", resp_data.get('liquidity', 'N/A'), inline=False, format_type="code")
            self._add_safe_field(embed, "🎯 Critical Zones", resp_data.get('critical_zones', 'N/A'), inline=False, format_type="code")
            self._add_safe_field(embed, "🔮 Weekly Projection", resp_data.get('weekly_projection', 'N/A'), inline=False, format_type="code")
            embed.set_footer(text="Quantum Hedge Fund Analytics • Key 5")
            
            await channel.send(embed=embed)
        except Exception as e:
            print(f"Weekly Task Error: {e}")
            await self.log_system_error("Deep Dive Failure", str(e))

    @tasks.loop(minutes=15)
    async def quantum_signals_task(self):
        channel = self.bot.get_channel(QUANTUM_SIGNALS_ID)
        if not channel or not hasattr(self, 'market_data') or not self.ai_engine: return
        
        for symbol in ["BTC/USDT", "ETH/USDT", "SOL/USDT"]:
            try:
                local_signal = await self.market_data.check_hft_signals(symbol, "15m")
                if local_signal.get("valid"):
                    ai_validation = await self.ai_engine.evaluate_hft_signal(
                        symbol=symbol,
                        timeframe="15m",
                        rsi=local_signal["rsi"],
                        price=local_signal["price"],
                        condition=local_signal["type"]
                    )
                    
                    if ai_validation.get("valid"):
                        tp = ai_validation.get("tp", 0.0)
                        sl = ai_validation.get("sl", 0.0)
                        
                        embed = discord.Embed(
                            title=f"🧠 Validación Cuántica: Activa [{symbol}]",
                            description=f"Sentinel AI ha confirmado una ineficiencia en el flujo de órdenes.\n\n"
                                        f"**Situación Local:** {local_signal['type']} (RSI: {local_signal['rsi']:.1f})\n"
                                        f"**Entrada Sugerida:** ${local_signal['price']:,.2f}\n"
                                        f"**Target Profit (TP):** ${tp:,.2f}\n"
                                        f"**Stop Loss (SL):** ${sl:,.2f}\n\n"
                                        f"**Análisis HFT:** {ai_validation.get('reason', 'Flujo de liquidez validado.')}",
                            color=COLOR_QUANTUM,
                            timestamp=discord.utils.utcnow()
                        )
                        embed.set_footer(text="Premium Quantum Level • Algorithmic Signal")
                        await channel.send(embed=embed)
            except Exception as e:
                print(f"Error en quantum_signals_task para {symbol}: {e}")

    @tasks.loop(time=time_9am_est)
    async def free_analysis_task(self):
        channel = self.bot.get_channel(FREE_ANALYSIS_ID)
        if channel:
            # 1. Seleccionar snippet educativo
            snippet = random.choice(self.educational_snippets)
            
            # 2. Recuperar el daily bias cacheados de los usuarios Core
            try:
                with open(self.global_cache_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    daily_bias_text = data.get("daily_bias", "Mercado en consolidación buscando definir una dirección.")
            except:
                daily_bias_text = "Mercado estable a la espera de confirmación de volumen direccional."
            
            # 3. Formatear la versión Free: Solo tomar la apertura (ej. primeras 15 palabras)
            words = daily_bias_text.split()
            short_bias = " ".join(words[:15]) + "..." if len(words) > 15 else daily_bias_text
            
            # 4. Enviar reporte estructurado
            embed = discord.Embed(
                title="📚 Sentinel Academy: Market Briefing",
                description=f"Sentinel AI ha concluido el análisis de la jornada.\n\n**🔍 Concepto de Hoy:**\n{snippet}\n\n**🤖 Resumen Macro (Teaser):**\n*{short_bias}*",
                color=COLOR_FREE,
                timestamp=discord.utils.utcnow()
            )
            embed.set_footer(text="Usa /upgrade para desbloquear el análisis completo y direcciones de mercado.")
            await channel.send(embed=embed)

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

    @tasks.loop(hours=4)
    async def major_signals_task(self):
        channel = self.bot.get_channel(MAJOR_SIGNALS_ID)
        if not channel or not hasattr(self, 'market_data'): return
        
        for symbol in ["BTC/USDT", "ETH/USDT"]:
            try:
                signal = await self.market_data.check_major_signals(symbol)
                if signal:
                    color = 0x00ff00 if signal['type'] == "BULLISH" else 0xff0000
                    embed = discord.Embed(
                        title=f"🎯 CORE ANALYTICS: MAJOR SIGNAL [{symbol}]",
                        description=f"Sentinel AI Core System ha detectado una confirmación macro-técnica sin uso dinámico de IA.\n\n**Señal:** {signal['type']}\n**Disparador:** {signal['reason']}",
                        color=color,
                        timestamp=discord.utils.utcnow()
                    )
                    embed.set_footer(text="Sentinel AI • 4H Macro Monitor")
                    await channel.send(embed=embed)
            except Exception as e:
                print(f"Error en major_signals_task para {symbol}: {e}")

    @tasks.loop(minutes=15)
    async def order_flow_tracker(self):
        channel = self.bot.get_channel(ORDER_FLOWS_ID)
        if not channel or not hasattr(self, 'market_data'): return
        
        try:
            spike_data = await self.market_data.analyze_volume_spike("BTC/USDT")
            if spike_data:
                volume_multiplier = spike_data['volume'] / spike_data['ma']
                embed = discord.Embed(
                    title=f"🚨 Institutional Volume Spike Detected",
                    description=f"**Activo:** BTC/USDT\n**Tipo:** {spike_data['direction']}\n**Magnitud:** {volume_multiplier:.1f}x por encima de la media móvil.",
                    color=0x8200c9,
                    timestamp=discord.utils.utcnow()
                )
                embed.set_footer(text="Sentinel Flow Tracker • Zero AI Cost")
                await channel.send(embed=embed)
        except Exception as e:
            print(f"Error en order_flow_tracker: {e}")

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
    async def send_alert(self, symbol, price_action, sentiment_data, ai_insight=None):
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
                embed.add_field(name="🧠 AI Insight (Sentinel Engine)", value=f"*{ai_insight}*", inline=False)
            
            embed.set_footer(text="Sentinel AI • Elite Analytics")
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
                        "backgroundColor": "rgba(0,0,0,0)",
                        "borderWidth": 2,
                        "pointRadius": 0
                    }]
                },
                "options": {
                    "legend": {"display": False},
                    "scales": {"xAxes": [{"display": False}]}
                }
            }
            import urllib.parse
            import json
            encoded_config = urllib.parse.quote(json.dumps(chart_config))
            chart_url = f"https://quickchart.io/chart?c={encoded_config}&w=400&h=200&bkg=white"
            embed.set_image(url=chart_url)
            
        embed.set_footer(text="Sentinel AI • Autonomous Validation")
        
        try:
            await wins_channel.send(embed=embed)
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
