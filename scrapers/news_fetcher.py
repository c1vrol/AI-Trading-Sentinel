import aiohttp
import logging
import re
import html

logger = logging.getLogger(__name__)

class NewsFetcher:
    """
    Fetcher de noticias REAL para el Sentinel AI Engine.
    Extrae titulares de feeds RSS públicos (CoinDesk/CoinTelegraph/CryptoSummary).
    """
    def __init__(self):
        self.rss_url = "https://www.coindesk.com/arc/outboundfeed/rss/"
        logger.info("NewsFetcher (Modo Comercio) inicializado.")

    async def fetch_latest_news(self, symbol: str) -> str:
        """
        Obtiene los titulares más recientes del feed RSS de CoinDesk.
        """
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as session:
                async with session.get(self.rss_url) as resp:
                    if resp.status == 200:
                        xml_content = await resp.text()
                        # Extraer los primeros 5 items y sus títulos (lógica regex simple)
                        titles = re.findall(r'<title>(.*?)</title>', xml_content, re.DOTALL)
                        # El primer título suele ser el del Canal, no una noticia
                        news_pool = [html.unescape(t.strip()) for t in titles[1:10] if len(t) > 10]
                        
                        if news_pool:
                            full_context = " | ".join(news_pool[:5])
                            logger.info(f"Contexto Macro Real recuperado para {symbol}: {full_context[:100]}...")
                            return full_context
                    
            logger.warning("No se pudo obtener el feed RSS. Usando fallback de seguridad.")
            return "Mercado en consolidación lateral. El flujo institucional se mantiene estable a la espera de datos macroeconómicos."
            
        except Exception as e:
            logger.error(f"Error fetching real news: {e}")
            return "Análisis macro temporalmente limitado por conexión. Monitoreo On-chain activo."
