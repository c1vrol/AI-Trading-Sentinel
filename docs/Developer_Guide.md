# 🧠 AI Trading Sentinel - Guía del Desarrollador (BaaS / Micro-SaaS)

¡Hola Andre! Esta documentación está escrita exclusivamente para ti en tu rol de Arquitecto y Dueño del Negocio. 

El modelo de monetización del **AI Trading Sentinel** no es la venta del código fuente, sino un **Bot as a Service (BaaS)** o el acceso a una comunidad premium (VIP). Tus clientes finales jamás tendrán acceso a estos archivos. El bot vivirá de manera permanente en un entorno controlado y seguro administrado por ti, distribuyendo valor a través de Discord.

---

## 📑 Índice
1. [Modelo de Negocio y Fase Inicial (Zero-Cost)](#1-modelo-de-negocio-y-fase-inicial-zero-cost)
2. [Arquitectura Multi-Tenant Básica](#2-arquitectura-multi-tenant-básica)
3. [El Flujo de Asincronía: Vida o Muerte para tu SaaS](#3-el-flujo-de-asincronía-vida-o-muerte-para-tu-saas)
4. [Propiedad Intelectual y Seguridad de Claves](#4-propiedad-intelectual-y-seguridad-de-claves)
5. [Despliegue y Hosting Inicial (VPS)](#5-despliegue-y-hosting-inicial-vps)

---

## 1. Modelo de Negocio y Fase Inicial (Zero-Cost)

En esta Fase 1, la prioridad es **validar la rentabilidad del bot sin incurrir en gastos fijos**. 
Para lograr esto, la integración con Google Gemini Pro en `core/ai_engine.py` funciona utilizando la llave de la versión **Free Tier** de Google AI Studio.

* **Ventaja:** Cero costos operativos por inferencia del LLM en la fase de prueba.
* **Limitaciones del Free Tier:** Límites de peticiones por minuto (RPM) y peticiones por día (RPD).
* **Escalabilidad:** A medida que vendas suficientes suscripciones de Discord VIP para generar flujo de caja positivo, el cambio al nivel *Pay-as-you-go* de Gemini se hace de forma transparente sin tener que reescribir la lógica; solo recargando la cuenta en Google Cloud y subiendo el umbral de monitoreo.

## 2. Arquitectura Multi-Tenant Básica

Como estás vendiendo acceso a comunidades en Discord, tu bot operará en un modelo **Multi-Tenant (Múltiples Inquilinos)**. Esto significa que una sola instancia del código alimentará varias comunidades o canales sin mezclar los datos.

### ¿Cómo estructurar `main.py` y `discord_bot.py`?
1. **Canales Concurrentes:** En vez de un solo `channel_id` hardcodeado en `config.yaml`, tu bot V2 mantendrá una lista (o base de datos SQL) de los distintos canales vinculados a los clientes de pago.
2. **Casteo de Alertas:** Cuando la `LogicGate` dictamine que una confluencia de mercado es válida, la función enviará la alerta iterando sobre los identificadores de tu base de suscriptores (ej. `for channel_id in suscriptores_premium: ...`).
3. **Roles y Permisos:** Tu bot de Discord debe estar configurado de tal manera que, si lo invitan a un servidor externo, tenga permisos de crear "Comandos Slash (`/`)" exclusivos para los roles de Administrador, bloqueando el acceso a usuarios que no pagaron.

## 3. El Flujo de Asincronía: Vida o Muerte para tu SaaS

Si el Sentinel va a mandar señales a 50 grupos de Discord diferentes mientras analiza 10 criptomonedas distintas al mismo tiempo, **el código síncrono bloquearía el servidor por completo**.

Por ello, la arquitectura que diseñamos descansa intrínsecamente en `asyncio`:
* **`ccxt.async_support`:** Cuando `market_data.py` pide velas (OHLCV) a Binance, la solicitud de red tarda milisegundos. Usar asincronía (`await`) permite que durante esos milisegundos, el procesador salte a enviar una alerta o atender un comando de usuario en Discord, en vez de quedarse congelado esperando a Binance.
* **Gemini LLM Asíncrono:** Analizar el sentimiento de un texto con IA suele tardar de 1 a 3 segundos. Hemos blindado esta llamada asíncronamente. Si un cliente escribe un comando (`!ping`) en su servidor mientras Gemini está "pensando", el bot le responderá al instante porque el hilo principal de Discord está liberado.

**Regla de oro:** Nunca incluyas un `time.sleep()` o librerías HTTP bloqueantes clásicas (`requests`); utiliza siempre las variantes asíncronas (`asyncio.sleep`, `aiohttp`, `ccxt.async_support`).

## 4. Propiedad Intelectual y Seguridad de Claves

Para que tu Micro-SaaS sea invulnerable y tu Propiedad Intelectual quede intacta:
* **Código Ofuscado / Privado:** Mantén siempre tu repositorio en GitHub o GitLab configurado como **Privado**.
* **Gestión de variables:** Hemos establecido un archivo `data/config.yaml`.
  * **¡ADVERTENCIA!** JAMÁS comitees `config.yaml` o cualquier `.env` a tu panel de control o repositorio en la nube. Tus _API Keys_ de Exchange (si conectas cuentas para operar), la _Key de Gemini_, y tu _Discord Bot Token_ dan acceso directo a tu negocio y tu dinero.
  * Utiliza `.gitignore` inmediatamente para ignorar el archivo `config.yaml`.
  * En el servidor final, sube el archivo de configuraciones manualmente por SSH/SFTP (ej: FileZilla).

## 5. Despliegue y Hosting Inicial (VPS)

En la Fase 1, no necesitas gastar $50 USD mensuales en clusters o nubes complejas (AWS, GCP). Lo óptimo es el aprovisionamiento de un **VPS (Virtual Private Server)** económico, puro, en Linux (Ubuntu 22.04 LTS).

**Nuestras Recomendaciones Híbridas (Calidad/Precio):**
1. **Contabo / Hetzner:** Por ~$5 a ~$8 USD, adquieres un servidor de 4GB a 8GB de RAM. Sobrado para correr el bot de Discord asíncrono y los parsers de NLP/Data simultáneamente.
2. **DigitalOcean o Vultr (Droplets básicos):** Si prefieres algo más amigable al usuario con excelentes guías, sus VPS básicos de $6 USD son más que suficientes mientras estás en el Free Tier y arrancas las iteraciones con tus primeros usuarios beta.

*Para mantener el bot vivo 24/7 en el servidor, simplemente ejecuta tu script utilizando gestores de procesos de Linux como `pm2`, `tmux` o `systemd`.*
