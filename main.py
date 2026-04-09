import asyncio
import logging
import yaml
import os

from core.market_data import MarketData
from core.ai_engine import AIEngine
from core.ai_polish_manager import should_run_alert_second_read, volatility_alert_second_read
from core.logic_gate import LogicGate
from interfaces.discord_bot import DiscordBotClient
from scrapers.news_fetcher import NewsFetcher
import json

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

def load_active_trades() -> list:
    path = os.path.join(os.path.dirname(__file__), "data", "active_trades.json")
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as file:
            return json.load(file)
    except Exception as e:
        logger.error(f"Error loading active trades: {e}")
        return []

def save_active_trades(trades: list):
    path = os.path.join(os.path.dirname(__file__), "data", "active_trades.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        with open(path, "w", encoding="utf-8") as file:
            json.dump(trades, file, indent=4)
    except Exception as e:
        logger.error(f"Error saving active trades: {e}")

async def market_monitor(config: dict, bot: DiscordBotClient):
    """
    Bucle principal de monitoreo no bloqueante del bot.
    """
    # Recoger campos del config
    symbols = config['trading'].get('symbols', [config['trading'].get('symbol', 'BTC/USDT')])
    timeframe = config['trading']['timeframe']
    exchange = config['trading']['exchange']
    confidence_thresh = config['trading']['confidence_threshold']

    # Inicialización de módulos
    market = MarketData(exchange_id=exchange)
    ai = bot.ai_engine  # Usar la instancia inyectada en el bot
    gate = LogicGate(confidence_threshold=confidence_thresh)
    news_fetcher = NewsFetcher()

    logger.info(f"Iniciando bucle de monitoreo para {len(symbols)} activos...")

    active_trades = load_active_trades()  # Persistence: Load live signals

    try:
        while True:
            # --- Check active trades for automatic logging ---
            if active_trades:
                logger.info(f"🔍 Revisando estado de {len(active_trades)} señales activas...")
                save_needed = False
                for trade in active_trades[:]:
                    trade_price_action = await market.analyze_price_action(trade['symbol'], timeframe)
                    current_trade_price = trade_price_action.get("close_price", 0.0)
                    if current_trade_price > 0:
                        trade['history'].append(current_trade_price)
                        save_needed = True
                        
                        if trade['sentiment'] == 'BULLISH':
                            if current_trade_price >= trade['tp']:
                                await bot.log_automated_record(trade['symbol'], trade['entry_price'], current_trade_price, 'BULLISH', 'Win ✅', trade['history'])
                                active_trades.remove(trade)
                            elif current_trade_price <= trade['sl']:
                                # Loss ❌ - Logged to Audit
                                await bot.log_automated_record(trade['symbol'], trade['entry_price'], current_trade_price, 'BULLISH', 'Loss ❌', trade['history'])
                                active_trades.remove(trade)
                        else: # BEARISH
                            if current_trade_price <= trade['tp']:
                                await bot.log_automated_record(trade['symbol'], trade['entry_price'], current_trade_price, 'BEARISH', 'Win ✅', trade['history'])
                                active_trades.remove(trade)
                            elif current_trade_price >= trade['sl']:
                                # Loss ❌ - Logged to Audit
                                await bot.log_automated_record(trade['symbol'], trade['entry_price'], current_trade_price, 'BEARISH', 'Loss ❌', trade['history'])
                                active_trades.remove(trade)
                
                if save_needed:
                    save_active_trades(active_trades)

            # --- Multi-Coin Scan ---
            for symbol in symbols:
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
                    logger.info(f"⚠️ Volatilidad detectada en {symbol} ({status}). Iniciando subsistema de IA y Noticias...")
                    # 2. Conseguir las últimas noticias
                    news_text = await news_fetcher.fetch_latest_news(symbol)
                    market_snap = await market.get_ai_snapshot(symbol)

                    logger.info(f"🧠 Consultando a Sentinel AI para {symbol}...")
                    # 3. Analizar sentimiento con Sentinel AI (con hechos de mercado reales)
                    sentiment_data = await ai.analyze_sentiment(
                        news_text, lang=bot.lang, market_context=market_snap
                    )
                    
                    # 4. Validar Confluencia
                    alert_trigger = gate.evaluate(price_action, sentiment_data)
                    
                    if alert_trigger:
                        logger.info(f"✅ Confluencia validada para {symbol} ({sentiment_data.get('sentiment')} {sentiment_data.get('confidence')}).")
                        
                        # 5. Obtener Insight de Emergencia (Smart Alert)
                        ai_insight = None
                        try:
                            ai_insight = await ai.get_emergency_insight(
                                symbol=symbol,
                                price_change=change_pct,
                                timeframe=timeframe,
                                lang=bot.lang,
                                market_snapshot=market_snap,
                            )
                        except Exception as e:
                            logger.error(f"No se pudo obtener insight de IA: {e}")

                        second_read = None
                        if should_run_alert_second_read(sentiment_data, change_pct):
                            try:
                                second_read = await volatility_alert_second_read(
                                    symbol,
                                    sentiment_data,
                                    ai_insight,
                                    market_snap,
                                    change_pct,
                                    timeframe=timeframe,
                                )
                                if second_read:
                                    second_read = second_read.strip()[:1024] or None
                            except Exception as e:
                                logger.error(f"Alert second read: {e}")

                        # 6. Enviar Alerta Asíncrona con el insight
                        await bot.send_alert(
                            symbol,
                            price_action,
                            sentiment_data,
                            ai_insight=ai_insight,
                            second_read_note=second_read,
                        )
                        
                        # 7. Registrar señal para su seguimiento automático
                        sentiment_str = sentiment_data.get('sentiment', '').upper()
                        if 'BULL' in sentiment_str or 'BEAR' in sentiment_str:
                            is_bull = 'BULL' in sentiment_str
                            # Metas institucionales: +1.2% TP / -0.8% SL (Más frecuencia)
                            tp_pct = 1.012 if is_bull else 0.988
                            sl_pct = 0.992 if is_bull else 1.008
                            
                            active_trades.append({
                                'symbol': symbol,
                                'sentiment': 'BULLISH' if is_bull else 'BEARISH',
                                'entry_price': current_price,
                                'tp': current_price * tp_pct,
                                'sl': current_price * sl_pct,
                                'history': [current_price]
                            })
                            save_active_trades(active_trades)
                            logger.info(f"📈 {symbol} tracked para validación: TP={current_price*tp_pct:,.2f} SL={current_price*sl_pct:,.2f}")
                    else:
                        logger.info(f"❌ Confluencia insuficiente para {symbol}. Alerta abortada.")
            
            # Esperar antes del siguiente check
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
    
    gemini_key = os.getenv("GEMINI_API_KEY")
    ai_engine = AIEngine(api_key=gemini_key)
    bot = DiscordBotClient(channel_id=channel_id, ai_engine=ai_engine)

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
