import os
import discord
from discord.ext import commands
import logging
import dotenv

logger = logging.getLogger(__name__)

class DiscordLoggingHandler(logging.Handler):
    """
    Handler de Logging que envía mensajes asíncronos a un canal de Discord.
    """
    def __init__(self, bot, log_channel_id):
        super().__init__()
        self.bot = bot
        self.log_channel_id = log_channel_id
        self.setFormatter(logging.Formatter('`[%(levelname)s]` **%(name)s:** %(message)s'))

    def emit(self, record):
        # Ignorar logs internos de la librería discord para evitar spam o bucle infinito
        if record.name.startswith('discord.') or record.name.startswith('websockets'):
            return
            
        log_entry = self.format(record)
        try:
            channel = self.bot.get_channel(self.log_channel_id)
            if channel:
                # Enviar de forma no bloqueante a la tarea de discord, asegurando casting a str
                msg = str(log_entry)
                self.bot.loop.create_task(channel.send(msg[:1990]))
        except Exception:
            pass

class DiscordBotClient(commands.Bot):
    """
    Cliente de Discord para el envio de alertas automatizado.
    """
    def __init__(self, channel_id: int):
        intents = discord.Intents.default()
        intents.message_content = True  # Requerido para procesar comandos por texto (si hay)
        super().__init__(command_prefix="!", intents=intents)
        self.alert_channel_id = channel_id
        
        # Canal de logs especificado por administrador
        self.log_channel_id = 1485739818741928157
        self._log_handler_setup = False
        
        # Cargar los IDs de canales autorizados (Candado SaaS)
        allowed_str = os.getenv("ALLOWED_CHANNEL_IDS", "")
        self.allowed_channel_ids = [int(cid.strip()) for cid in allowed_str.split(",") if cid.strip().isdigit()]

    async def on_ready(self):
        logger.info(f'Logged on as {self.user}!')
        
        # 1. Configurar log a Discord
        if not self._log_handler_setup:
            discord_handler = DiscordLoggingHandler(self, self.log_channel_id)
            discord_handler.setLevel(logging.INFO)
            logging.getLogger().addHandler(discord_handler)
            self._log_handler_setup = True
            
            # 2. Notificación en canal de logs
            log_channel = self.get_channel(self.log_channel_id)
            if log_channel:
                await log_channel.send("🛠️ **SISTEMA DE LOGS CONECTADO.** *Recibiendo consola en tiempo real...*")
            
            # 3. Notificación VIP de inicio
            alert_channel = self.get_channel(self.alert_channel_id)
            if alert_channel:
                embed = discord.Embed(
                    title="🟢 Sentinel Activado",
                    description="El sistema de Inteligencia Artificial está en línea y ha comenzado a monitorear el mercado.",
                    color=discord.Color.green()
                )
                await alert_channel.send(embed=embed)

    async def on_message(self, message):
        """
        Intercepción de mensajes para procesar comandos manuales o comandos del Candado.
        """
        if message.author == self.user:
            return
            
        if self.allowed_channel_ids and message.channel.id not in self.allowed_channel_ids:
            return

        # Comando !setchannel para que administradores asignen dinámicamente el canal
        if message.content.startswith("!setchannel"):
            parts = message.content.split()
            if len(parts) == 2 and parts[1].isdigit():
                new_channel = int(parts[1])
                self.alert_channel_id = new_channel
                
                # Guardar permanentemente en .env
                env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
                dotenv.set_key(env_path, "ALERT_CHANNEL_ID", str(new_channel))
                
                await message.channel.send(f"✅ ¡Canal de alertas actualizado a <#{new_channel}> de forma permanente!")
            else:
                await message.channel.send("⚠️ Uso correcto: `!setchannel <ID_DEL_CANAL>`")
            return

        # Solo procesar otros comandos en canales autorizados
        await self.process_commands(message)

    async def send_alert(self, symbol: str, price_action: dict, sentiment_data: dict):
        """
        Construye y envía un Embed enriquecido a Discord con los detalles de la alerta generada por LogicGate.
        """
        # Candado de escritura: Verificar si el canal objetivo está autorizado
        if self.allowed_channel_ids and self.alert_channel_id not in self.allowed_channel_ids:
            logger.warning("ALERTA DESECHADA: Intento de emitir en canal no autorizado por el candado SaaS.")
            return

        channel = self.get_channel(self.alert_channel_id)
        if not channel:
            logger.error(f"No se pudo encontrar el canal de discord con ID {self.alert_channel_id}")
            return
        
        status = price_action.get('status', 'NEUTRAL')
        current_price = price_action.get('close_price', 0.0)
        change_pct = price_action.get('change_pct', 0.0)
        
        sentiment = sentiment_data.get('sentiment', 'NEUTRAL')
        confidence = sentiment_data.get('confidence', 0.0)
        reasoning = sentiment_data.get('reasoning', 'Sin razonamiento proporcionado.')

        color = discord.Color.green() if sentiment == "BULLISH" else discord.Color.red()
        
        embed = discord.Embed(
            title=f"🚨 ALERTA DE TRADING: {symbol} 🚨",
            description="La Inteligencia Artificial ha detectado una fuerte confluencia en el mercado.",
            color=color
        )
        embed.add_field(name="💰 Precio Actual", value=f"${current_price:,.2f} ({change_pct:+.2f}%)", inline=False)
        embed.add_field(name="📈 Acción del Precio", value=status, inline=False)
        embed.add_field(name="🧠 Sentimiento de IA", value=f"**{sentiment}** (Confianza: {confidence:.2f})", inline=False)
        embed.add_field(name="📄 Justificación", value=reasoning, inline=False)
        embed.set_footer(text="AI Trading Sentinel V2.0")

        await channel.send(embed=embed)
        logger.info(f"Alerta enviada a Discord exitosamente en canal VIP ({symbol} - {sentiment}).")
