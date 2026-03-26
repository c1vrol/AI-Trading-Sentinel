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
        Analiza la volatilidad y la acción de precio real para detectar desequilibrios.
        Sensibilidad ajustada para Modo Comercio (0.2%).
        """
        data = await self.fetch_ohlcv(symbol, timeframe, limit=2)
        if not data or len(data) < 2:
            return {"status": "NEUTRAL", "close_price": 0.0, "change_pct": 0.0}
        
        last_closed_candle = data[-2]
        open_price = last_closed_candle[1]
        close_price = last_closed_candle[4]
        
        change_pct = ((close_price - open_price) / open_price) * 100
        
        # Umbral institucional ajustado (0.2% para 4h/1h/15m)
        status = "NEUTRAL"
        if change_pct > 0.2:
            status = "Volatile UP"
        elif change_pct < -0.2:
            status = "Volatile DOWN"
            
        return {
            "status": status,
            "close_price": close_price,
            "change_pct": change_pct
        }

    def calculate_rsi(self, prices: list, period: int = 14) -> float:
        if len(prices) < period + 1:
            return 50.0
        gains = []
        losses = []
        for i in range(len(prices) - period, len(prices)):
            change = prices[i] - prices[i-1]
            if change > 0:
                gains.append(change)
                losses.append(0.0)
            else:
                gains.append(0.0)
                losses.append(abs(change))
                
        avg_gain = sum(gains) / period
        avg_loss = sum(losses) / period
        
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100.0 - (100.0 / (1.0 + rs))

    async def check_major_signals(self, symbol: str) -> dict:
        """Determina cruces de SMA50/SMA200 y valores RSI(14) en temporalidad 4H."""
        data = await self.fetch_ohlcv(symbol, '4h', limit=205)
        if not data or len(data) < 201: return None
        closes = [candle[4] for candle in data]
        
        # Ignorar vela actual no consolidada
        prev_closes = closes[:-1]
        
        sma50_current = sum(prev_closes[-50:]) / 50
        sma50_prev = sum(prev_closes[-51:-1]) / 50
        
        sma200_current = sum(prev_closes[-200:]) / 200
        sma200_prev = sum(prev_closes[-201:-1]) / 200
        
        golden_cross = sma50_current > sma200_current and sma50_prev <= sma200_prev
        death_cross = sma50_current < sma200_current and sma50_prev >= sma200_prev
        
        current_rsi = self.calculate_rsi(prev_closes)
        oversold = current_rsi < 25
        overbought = current_rsi > 75
        
        if golden_cross or oversold:
            return {"type": "BULLISH", "reason": "Golden Cross (SMA 50 > SMA 200)" if golden_cross else f"RSI Oversold ({current_rsi:.1f})"}
        elif death_cross or overbought:
            return {"type": "BEARISH", "reason": "Death Cross (SMA 50 < SMA 200)" if death_cross else f"RSI Overbought ({current_rsi:.1f})"}
            
        return None

    async def analyze_volume_spike(self, symbol: str) -> dict:
        """Detecta picos de volumen anormales institucionales usando temporalidad de 15m."""
        data = await self.fetch_ohlcv(symbol, '15m', limit=22)
        if not data or len(data) < 22: return None
        
        latest_candle = data[-2]
        prev_candles = data[-22:-2]
        
        volumes = [c[5] for c in prev_candles]
        ma_vol = sum(volumes) / len(volumes) if volumes else 1.0
        
        current_vol = latest_candle[5]
        
        if current_vol > ma_vol * 3.0:
            open_p = latest_candle[1]
            close_p = latest_candle[4]
            direction = "Compra Agresiva 🟢" if close_p > open_p else "Venta Agresiva 🔴"
            return {"spike": True, "direction": direction, "volume": current_vol, "ma": ma_vol, "close": close_p}
            
        return None

    async def check_hft_signals(self, symbol: str, timeframe: str = '15m') -> dict:
        """Prefiltro técnico para operaciones HFT (Quantum). Detecta extremos de RSI."""
        data = await self.fetch_ohlcv(symbol, timeframe, limit=20)
        if not data or len(data) < 16: return {"valid": False}
        closes = [candle[4] for candle in data]
        
        # Ignorar vela actual no consolidada
        prev_closes = closes[:-1]
        current_price = prev_closes[-1]
        
        current_rsi = self.calculate_rsi(prev_closes, period=14)
        
        if current_rsi < 30:
            return {"valid": True, "type": "Oversold 🟢", "rsi": current_rsi, "price": current_price}
        elif current_rsi > 70:
            return {"valid": True, "type": "Overbought 🔴", "rsi": current_rsi, "price": current_price}
            
        return {"valid": False}

    async def close_connection(self):
        """
        Cierra limpiamente la sesión de CCXT para evitar fugas de memoria o sockets abiertos.
        """
        await self.exchange.close()
