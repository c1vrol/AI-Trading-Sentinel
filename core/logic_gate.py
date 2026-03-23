import logging

logger = logging.getLogger(__name__)

class LogicGate:
    """
    Evalúa si la volatilidad del precio y el sentimiento de IA se alinean
    para generar una alerta accionable.
    """
    def __init__(self, confidence_threshold: float = 0.70):
        """
        Inicializa la puerta lógica.
        
        :param confidence_threshold: Nivel mínimo de confianza del LLM (0.0 a 1.0) para validar el trade.
        """
        self.confidence_threshold = confidence_threshold
        logger.info(f"LogicGate inicializado con threshold de confianza: {self.confidence_threshold}")

    def evaluate(self, price_action: dict, sentiment_data: dict) -> bool:
        """
        Lógica de confluencia.
        - Si Price_Action es 'Volatile UP' y Sentiment es 'BULLISH' con alta confianza -> Alert
        - Si Price_Action es 'Volatile DOWN' y Sentiment es 'BEARISH' con alta confianza -> Alert
        
        :param price_action: Diccionario con la acción del precio (status, close_price, change_pct).
        :param sentiment_data: Diccionario con el sentimiento, confianza y reasoning de IA.
        :return: True si se debe emitir la alerta, False en caso contrario.
        """
        sentiment = sentiment_data.get("sentiment", "NEUTRAL")
        confidence = sentiment_data.get("confidence", 0.0)
        status = price_action.get("status", "NEUTRAL")

        # Filtro de confianza
        if confidence < self.confidence_threshold:
            logger.info("Alerta rechazada: Nivel de confianza del LLM es menor al umbral.")
            return False

        # Confluencia Alcista
        if status == "Volatile UP" and sentiment == "BULLISH":
            logger.info("Confluencia ALCISTA detectada.")
            return True

        # Confluencia Bajista
        if status == "Volatile DOWN" and sentiment == "BEARISH":
            logger.info("Confluencia BAJISTA detectada.")
            return True

        logger.info(f"Sin confluencia: PA={status}, Sent={sentiment}")
        return False
