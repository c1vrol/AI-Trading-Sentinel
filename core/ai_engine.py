import json
import os
import asyncio
import logging

# Fallback in case google.genai is not robustly installed in linting env
try:
    from google import genai
    from google.genai import types
except ImportError:
    genai = None
    types = None

logger = logging.getLogger(__name__)

class AIEngine:
    """
    Motor de Inteligencia Artificial que utiliza la API de Google Gemini Pro asíncronamente
    para realizar análisis de sentimiento del mercado.
    """
    
    def __init__(self, api_key: str = ""):
        """
        Inicia el motor con un esquema de aislamiento estricto de 5 llaves:
        1. MONITOR (L 1, 2): Autonomous logic (main.py).
        2. USER_VIP (L 3): High-priority /ai_scan (Quantum).
        3. ADMIN_FORCE (L 4): Emergency /force_global override.
        4. SYSTEM_LOOP (L 5): Automated background tasks.
        """
        self.missions = {
            "MONITOR": ["AIzaSyA5xc847AXAdXE60MNtchC03P2bmdFm-m0", "AIzaSyBMP3MiUuF6rciJ5-dKvYQm_a45PXmwNak"],
            "USER_VIP": ["AIzaSyDaEkcuOqYyP2XXwJXmAvPEmeNKJ015d7s"],
            "ADMIN_FORCE": ["AIzaSyBN_7jgeWG-7K3_EpYAiBDhRKB5Y-7sW8s"],
            "SYSTEM_LOOP": ["AIzaSyBx_IadhKwisUWVY82-Hl3bokKVQ2QhDx4"]
        }
        
        # Estado de rotación para pools multi-llave
        self.rotation_indices = {m: 0 for m in self.missions}
        
        # Rastreador de Cooldowns (API Key -> Timestamp)
        self.cooldowns = {}
        
        # Cache de clientes para evitar reinicializaciones constantes
        self.clients = {}
        for pool in self.missions.values():
            for k in pool:
                self.clients[k] = genai.Client(api_key=k)
        
        # Contador global de peticiones (Telemetría)
        self.api_call_count = 0
        logger.info("AIEngine Advanced Key Manager inicializado (5 keys / 4 missions).")

    def _get_client(self, mission: str):
        """
        Retorna un cliente (key) disponible para la misión solicitada.
        Implementa Round Robin y salta llaves en cooldown.
        """
        pool = self.missions.get(mission, self.missions.get("MONITOR"))
        if not pool:
            # Emergency fallback: use any available key
            pool = list(self.clients.keys()) if self.clients else []
        num_keys = len(pool)
        import time
        
        # Intentar encontrar una llave que no esté en cooldown
        for _ in range(num_keys * 2):
            idx = self.rotation_indices[mission] % num_keys
            key = pool[idx]
            self.rotation_indices[mission] += 1
            
            # Verificar cooldown
            cooldown_until = self.cooldowns.get(key, 0)
            if time.time() > cooldown_until:
                return key, self.clients[key]
                
        # Fallback a la primera si todas fallan
        key = pool[0]
        return key, self.clients[key]

    def _mark_cooldown(self, key: str):
        """Marca una llave con un enfriamiento de 60 segundos tras un error 429."""
        import time
        logger.warning(f"🚨 API Key {key[:10]}... en cooldown por saturación (429).")
        self.cooldowns[key] = time.time() + 60

    async def generate_response(self, prompt: str, system_instruction: str = "", mission: str = "USER_VIP", response_mime_type: str = "text/plain"):
        """
        Método centralizado para generar contenido con rotación de llaves y failover.
        """
        max_retries = 3
        for attempt in range(max_retries):
            key, client = self._get_client(mission)
            try:
                self.api_call_count += 1
                # Siempre usar gemini-2.5-flash para máximo rendimiento y evitar 404s
                response = await asyncio.to_thread(
                    client.models.generate_content,
                    model='gemini-2.5-flash',
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        system_instruction=system_instruction if system_instruction else None,
                        response_mime_type=response_mime_type
                    )
                )
                return response
            except Exception as e:
                error_str = str(e).lower()
                if "429" in error_str or "resourceexhausted" in error_str or "too many requests" in error_str:
                    self._mark_cooldown(key)
                    if attempt < max_retries - 1:
                        logger.info(f"🔄 Reintentando misión {mission} con siguiente llave...")
                        continue
                logger.error(f"Error en misión {mission} [Key: {key[:8]}...]: {e}")
                raise e

    async def analyze_sentiment(self, text: str, lang: str = "es", mission: str = "MONITOR") -> dict:
        """
        Analiza el sentimiento de una noticia usando la llave asignada a MONITOR o la misión provista.
        """
        sys_instr = f"Lang:{lang}. HFT. Return tight JSON."
        prompt = f'Parse:"{text}"\nOutput JSON: {{"sentiment":"BULLISH|BEARISH|NEUTRAL","confidence":0.0,"support":0.0,"resistance":0.0,"thesis":"1 line","disclaimer":"Risk Note"}}'
        
        try:
            # Explicitly await and use result
            gen_response = await self.generate_response(
                prompt=prompt, 
                system_instruction=sys_instr, 
                mission=mission,
                response_mime_type="application/json"
            )
            # Accessing the .text attribute from the response object
            response_text = str(gen_response.text)
            data = json.loads(response_text)
            return {
                "sentiment": data.get("sentiment", "NEUTRAL").upper(),
                "confidence": float(data.get("confidence", 0.0)),
                "support": float(data.get("support", 0.0)),
                "resistance": float(data.get("resistance", 0.0)),
                "thesis": data.get("thesis", "Sin tesis suficiente."),
                "disclaimer": data.get("disclaimer", "Sujeto a riesgos.")
            }
        except Exception as e:
            logger.error(f"Fallo en analyze_sentiment: {e}")
            return {"sentiment": "NEUTRAL", "confidence": 0.0, "thesis": f"Error: {e}", "disclaimer": ""}

    async def analyze_market_batch(self, market_data: dict, lang: str = "es") -> dict:
        """
        Analiza múltiples pares en lote. Usado típicamente por MONITOR o BROADCAST.
        """
        sys_instr = f"Lang:{lang}. Quant HFT. Output JSON."
        prompt = f"Batch Analysis: {json.dumps(market_data)}"
        
        try:
            gen_response = await self.generate_response(
                prompt=prompt, 
                system_instruction=sys_instr, 
                mission="MONITOR",
                response_mime_type="application/json"
            )
            return json.loads(gen_response.text)
        except Exception as e:
            logger.error(f"Fallo en analyze_market_batch: {e}")
            return {}

    async def get_emergency_insight(self, symbol: str, price_change: float, timeframe: str = "5m", lang: str = "es") -> str:
        """
        Explicación flash ante movimientos bruscos. Usa la misión MONITOR.
        """
        sys_instr = f"Lang:{lang}. HFT."
        prompt = f"Explain {price_change:+.2f}% move in {symbol} ({timeframe}) in <25 words."
        
        try:
            gen_response = await self.generate_response(
                prompt=prompt, 
                system_instruction=sys_instr, 
                mission="MONITOR"
            )
            return gen_response.text.strip()
        except Exception as e:
            return "Emergency insight unavailable."

    async def evaluate_hft_signal(self, symbol: str, timeframe: str, rsi: float, price: float, condition: str, lang: str = "es") -> dict:
        """Llamada ultrarrápida (Misión MONITOR) para confirmar señales de Day Trading/HFT."""
        sys_instr = f"Lang:{lang}. Quant HFT. Return strict JSON ONLY."
        prompt = f"HFT. Analyze {symbol} {timeframe}. RSI is {rsi:.1f}. Price is ${price:,.2f}. Condition: {condition}. Valid fast trade? JSON format: {{\\\"valid\\\": bool, \\\"tp\\\": float, \\\"sl\\\": float, \\\"reason\\\": \\\"string\\\"}}"
        
        try:
            gen_response = await self.generate_response(
                prompt=prompt, 
                system_instruction=sys_instr, 
                mission="MONITOR",
                response_mime_type="application/json"
            )
            return json.loads(gen_response.text)
        except Exception as e:
            logger.error(f"Fallo en evaluate_hft_signal: {e}")
            return {"valid": False, "reason": str(e)}

