import asyncio
import logging
import google.generativeai as genai

logger = logging.getLogger(__name__)

class AIEngine:
    """
    Motor de Inteligencia Artificial que utiliza la API de Google Gemini Pro asíncronamente
    para realizar análisis de sentimiento del mercado.
    """
    
    def __init__(self, api_key: str):
        """
        Inicializa el cliente de Gemini y el contador de peticiones.
        
        :param api_key: Clave de la API para Google Generative AI.
        """
        genai.configure(api_key=api_key)
        # Instanciar el modelo Gemini Pro
        self.model = genai.GenerativeModel('gemini-pro')
        
        # Contador global de peticiones a la API
        self.api_call_count = 0
        
        logger.info("AIEngine inicializado con el modelo Gemini Pro.")

    async def analyze_sentiment(self, text: str) -> dict:
        """
        Analiza el sentimiento de una noticia o cadena de texto usando LLM, de manera no bloqueante.
        Aplica Backoff Exponencial en caso de recibir error 429 (Saturación de API).
        
        :param text: Texto o noticias relevantes al par de mercado.
        :return: Dict con formato {'sentiment': '...', 'confidence': ..., 'reasoning': '...'}
        """
        prompt = f'''
        Analiza el siguiente texto de noticias financieras o criptomonedas.
        Debes clasificar el sentimiento en exactamente una de estas tres categorías: BULLISH, BEARISH, o NEUTRAL.
        También debes estimar una puntuación de confianza entre 0.0 y 1.0, e incluir un breve razonamiento (de 1 sola oración).
        
        Texto: "{text}"
        
        Devuelve el resultado ESTRICTAMENTE en este formato (separado por una barra vertical | sin espacios adicionales):
        SENTIMENT|CONFIDENCE|REASONING
        Ejemplo: BULLISH|0.85|Las noticias indican fuerte acumulación institucional e inversiones millonarias.
        '''
        
        max_retries = 5
        base_delay = 2
        
        for attempt in range(max_retries):
            try:
                self.api_call_count += 1
                logger.info(f"Gemini Call #{self.api_call_count} (Límite sugerido para capa gratuita: ~1500/día)")
                
                # Usando to_thread internamente o wrapper para generar asíncronamente
                response = await asyncio.to_thread(self.model.generate_content, prompt)
                
                # Parsear respuesta: "BULLISH|0.85|Razonamiento..."
                result_text = response.text.strip()
                parts = result_text.split('|')
                
                if len(parts) >= 3:
                    sentiment = parts[0].strip().upper()
                    confidence = float(parts[1].strip())
                    reasoning = parts[2].strip()
                    if sentiment not in ["BULLISH", "BEARISH", "NEUTRAL"]:
                        sentiment = "NEUTRAL"
                        confidence = 0.0
                    return {"sentiment": sentiment, "confidence": confidence, "reasoning": reasoning}
                else:
                    return {"sentiment": "NEUTRAL", "confidence": 0.0, "reasoning": "Respuesta de IA con formato inesperado."}
                    
            except Exception as e:
                error_str = str(e)
                if "429" in error_str or "Too Many Requests" in error_str or "quota" in error_str.lower():
                    if attempt < max_retries - 1:
                        sleep_time = base_delay * (2 ** attempt)  # 2, 4, 8, 16s...
                        logger.warning(f"⚠️ ¡LÍMITE DE API ALCANZADO (429)! Entrando en modo hibernación por {sleep_time}s... (Intento {attempt+1}/{max_retries})")
                        await asyncio.sleep(sleep_time)
                        continue
                    else:
                        logger.error("Se superó el límite máximo de reintentos por error 429.")
                else:
                    logger.error(f"Error analizando sentimiento con IA: {e}")
                    
                # Retornamos error en los fallos (o tras agotar intentos)
                return {"sentiment": "NEUTRAL", "confidence": 0.0, "reasoning": error_str}
        
        return {"sentiment": "NEUTRAL", "confidence": 0.0, "reasoning": "Fallo de conexión crítico con IA."}
