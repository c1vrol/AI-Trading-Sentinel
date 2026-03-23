import ccxt.async_support as ccxt
import asyncio
import logging

logger = logging.getLogger(__name__)

class MarketData:
    """
    Módulo para obtener datos de mercado utilizando CCXT de forma asíncrona.
    """
    def __init__(self, exchange_id: str = "binance"):
        """
        Inicializa el cliente del exchange de manera asíncrona.
        :param exchange_id: Nombre del exchange a utilizar (ej. 'binance').
        """
        self.exchange_id = exchange_id
        # Instanciar el exchange asíncrono dinámicamente
        exchange_class = getattr(ccxt, self.exchange_id)
        self.exchange = exchange_class({
            'enableRateLimit': True,
            'headers': {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
            }
        })
        logger.info(f"MarketData inicializado con el exchange: {exchange_id}")

    async def fetch_ohlcv(self, symbol: str, timeframe: str = '1h', limit: int = 5) -> list:
        """
        Obtiene velas (OHLCV) en vivo para el símbolo especificado.
        
        :param symbol: Par de trading (ej. 'BTC/USDT').
        :param timeframe: Intervalo de tiempo (ej. '1h', '15m').
        :param limit: Número de velas a obtener.
        :return: Lista de velas, donde cada vela es [timestamp, open, high, low, close, volume].
        """
        try:
            # fetch_ohlcv es una operación de red asíncrona
            ohlcv = await self.exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
            return ohlcv
        except Exception as e:
            logger.error(f"Error al obtener ohlcv para {symbol}: {e}")
            return []

    async def analyze_price_action(self, symbol: str, timeframe: str = '1h') -> dict:
        """
        Método de conveniencia para analizar la volatilidad y la acción de precio reciente.
        En esta V1, una lógica sencilla que mira si el precio subió o bajó en la última vela cerrada.
        
        :return: Dict con status ('Volatile UP', 'Volatile DOWN', o 'NEUTRAL'), precio actual y porcentaje.
        """
        data = await self.fetch_ohlcv(symbol, timeframe, limit=2)
        if not data or len(data) < 2:
            return {"status": "NEUTRAL", "close_price": 0.0, "change_pct": 0.0}
        
        # Velas son [timestamp, open, high, low, close, volume]
        last_closed_candle = data[-2]
        open_price = last_closed_candle[1]
        close_price = last_closed_candle[4]
        
        # Calcula el % de cambio
        change_pct = ((close_price - open_price) / open_price) * 100
        
        # Umbral simple de volatilidad: > 1% en la vela
        status = "NEUTRAL"
        if change_pct > 1.0:
            status = "Volatile UP"
        elif change_pct < -1.0:
            status = "Volatile DOWN"
            
        return {
            "status": status,
            "close_price": close_price,
            "change_pct": change_pct
        }

    async def close_connection(self):
        """
        Cierra limpiamente la sesión de CCXT para evitar fugas de memoria o sockets abiertos.
        """
        await self.exchange.close()
