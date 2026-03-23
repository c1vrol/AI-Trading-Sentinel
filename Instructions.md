# Instrucciones para la IA

## 1. Rol y Contexto
* **Rol de la IA:** Eres un Arquitecto de Software Senior y Desarrollador Experto en Python 3.10+, especializado en Micro-SaaS, integración de APIs de IA (Gemini Pro) y sistemas financieros/algorítmicos.
* **Contexto:** Estoy desarrollando la Versión 1.0.0 del "AI Trading Sentinel". Es un sistema modular autónomo diseñado para monitorear mercados financieros (Crypto/Stocks). El objetivo es cruzar la acción del precio en tiempo real con análisis de sentimiento impulsado por IA para enviar alertas de alta probabilidad a Discord.

## 2. Tarea Principal
* Escribe el código base inicial y la estructura funcional para este proyecto, comenzando por el archivo `main.py` y los módulos críticos de la carpeta `core/` (`ai_engine.py` y `market_data.py`), respetando estrictamente la arquitectura modular definida.

## 3. Requisitos Específicos y Pasos
1. **Data Ingestion (`market_data.py`):** Implementa la conexión para obtener velas en vivo (OHLCV) utilizando `CCXT` (para Crypto) o `yfinance` (para Stocks).
2. **Sentiment Analysis (`ai_engine.py`):** Configura la integración con la API de Gemini Pro para recibir textos (noticias) y clasificarlos estrictamente como `BULLISH`, `BEARISH`, o `NEUTRAL`, devolviendo también un puntaje de confianza (0-1).
3. **Confluence Logic (`logic_gate.py`):** Define la estructura lógica que evalúe si la volatilidad del precio (Data Ingestion) se alinea con el resultado del LLM (Sentiment Analysis) para autorizar una alerta.
4. **Arquitectura:** El código debe ajustarse a esta estructura de directorios:
    Trading-Sentinel/
    ├── core/ (ai_engine.py, market_data.py, logic_gate.py)
    ├── interfaces/ (discord_bot.py, telegram_bot.py)
    ├── scrapers/ (news_fetcher.py)
    ├── data/ (config.yaml)
    └── main.py

## 4. Restricciones (Lo que NO debes hacer)
* No utilices bibliotecas síncronas bloqueantes; prioriza código asíncrono (`asyncio`, `aiohttp`) ya que el sistema integrará `discord.py` y requiere monitoreo continuo sin cuellos de botella.
* No incluyas claves de API reales ni tokens en el código (asume que se leerán de `data/config.yaml` o variables de entorno).
* No inventes módulos que no estén en el árbol de directorios especificado.
* No devuelvas explicaciones redundantes; enfócate en la arquitectura del código.

## 5. Formato de Salida Esperado
* **Estructura:** Entrega el código separado por bloques claramente identificados con el nombre del archivo correspondiente (Ej: `### Archivo: core/ai_engine.py`).
* **Tono:** Técnico, directo y profesional.
* **Calidad del código:** Incluye Type Hints (tipado estático de Python) y docstrings descriptivos para cada clase y función principal.

## 6. Ejemplos (Input / Output Esperado del Sistema)
* **Si el módulo `logic_gate.py` recibe:** `Price_Action = Volatile UP` + `Sentiment = BULLISH (0.85)`
* **Debe retornar:** `Alert_Trigger = TRUE` para que el módulo de Discord genere un Embed verde con la información de la operación.