import asyncio
import logging
import yaml
import os

from core.market_data import MarketData
from core.ai_engine import AIEngine
from core.logic_gate import LogicGate
from interfaces.discord_bot import DiscordBotClient
from scrapers.news_fetcher import NewsFetcher

from dotenv import load_dotenv

# Cargar variables de entorno desde .env
load_dotenv()

# Configuración básica de logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def load_config() -> dict:
    config_path = os.path.join(os.path.dirname(__file__), "data", "config.yaml")
    with open(config_path, "r", encoding="utf-8") as file:
        return yaml.safe_load(file)

async def market_monitor(config: dict, bot: DiscordBotClient):
    """
    Bucle principal de monitoreo no bloqueante del bot.
    """
    # Recoger campos del config
    symbol = config['trading']['symbol']
    timeframe = config['trading']['timeframe']
    exchange = config['trading']['exchange']
    confidence_thresh = config['trading']['confidence_threshold']
    gemini_key = os.getenv("GEMINI_API_KEY")

    # Inicialización de módulos
    market = MarketData(exchange_id=exchange)
    ai = AIEngine(api_key=gemini_key)
    gate = LogicGate(confidence_threshold=confidence_thresh)
    news_fetcher = NewsFetcher()

    logger.info("Iniciando bucle de monitoreo...")

    try:
        while True:
            # 1. Obtener acción del precio (Ingestión de Datos)
            logger.info(f"🔎 Analizando acción de precio para {symbol} ({timeframe})...")
            price_action = await market.analyze_price_action(symbol, timeframe)
            
            status = price_action.get("status", "NEUTRAL")
            current_price = price_action.get("close_price", 0.0)
            change_pct = price_action.get("change_pct", 0.0)
            
            # Mostrar la telemetría exacta en consola
            logger.info(f"📊 TELEMETRÍA: {symbol} | Precio: ${current_price:,.2f} | Variación: {change_pct:+.2f}% | Estado: {status}")
            
            # Solo seguimos si hay volatilidad (optimización de llamadas a la IA)
            if status != "NEUTRAL":
                logger.info(f"⚠️ Volatilidad detectada ({status}). Iniciando subsistema de IA y Noticias...")
                # 2. Conseguir las últimas noticias
                news_text = await news_fetcher.fetch_latest_news(symbol)
                
                logger.info("🧠 Consultando a Gemini IA para análisis de sentimiento profundo...")
                # 3. Analizar sentimiento con Gemini Pro
                sentiment_data = await ai.analyze_sentiment(news_text)
                
                # 4. Validar Confluencia
                alert_trigger = gate.evaluate(price_action, sentiment_data)
                
                if alert_trigger:
                    logger.info(f"✅ Confluencia validada ({sentiment_data.get('sentiment')} {sentiment_data.get('confidence')}). Preparando alerta.")
                    # 5. Enviar Alerta Asíncrona
                    await bot.send_alert(symbol, price_action, sentiment_data)
                else:
                    logger.info("❌ Confluencia insuficiente (o no alcanza el threshold). Alerta abortada.")
            else:
                logger.info("⏸️ Mercado Neutral. Motor IA en reposo ahorrando tokens. Pausa de 60s...")
            
            # Esperar antes del siguiente check (ej: 60 segundos)
            await asyncio.sleep(60)

    except asyncio.CancelledError:
        logger.info("Monitoreo detenido por cancelación.")
    finally:
        await market.close_connection()

async def main():
    config = load_config()
    
    # Inicializar bot de discord leyendo desde .env
    token = os.getenv("DISCORD_BOT_TOKEN")
    
    # Intentamos leer de .env, si no usar el de config
    channel_id_str = os.getenv("ALERT_CHANNEL_ID")
    if channel_id_str and channel_id_str.isdigit():
        channel_id = int(channel_id_str)
    else:
        channel_id = config['discord']['channel_id']
        
    if not token or token == "tu_token_aqui":
        logger.error("No se encontró el DISCORD_BOT_TOKEN en el archivo .env o es inválido.")
        return
    
    bot = DiscordBotClient(channel_id=channel_id)

    # Iniciar la tarea de monitoreo de mercado en el background y arrancar el bot de discord.
    # El bot de discord es un loop consumidor prolongado así que debemos correr todo el setup con asyncio.
    async with bot:
        bot.loop.create_task(market_monitor(config, bot))
        await bot.start(token)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Sistema apagado por el usuario.")
