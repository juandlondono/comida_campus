"""Cliente mínimo para la API /chat/completions compatible con OpenAI (Ollama, Groq, OpenAI...).
Solo usa la librería estándar."""

from __future__ import annotations

import asyncio
import json
import time
import urllib.error
import urllib.request

from . import config


class ErrorLLM(RuntimeError):
    pass


def _post(payload: dict, intentos: int = 4) -> dict:
    """POST con reintentos: los planes gratuitos en la nube responden 429 si se pasa el límite por minuto."""
    for intento in range(intentos):
        try:
            return _post_una_vez(payload)
        except ErrorLimite as e:
            if intento == intentos - 1:
                raise ErrorLLM(f"El proveedor sigue limitando las peticiones (429): {e}") from e
            time.sleep(min(e.espera, 60))
    raise ErrorLLM("No se obtuvo respuesta del LLM.")


class ErrorLimite(Exception):
    def __init__(self, espera: float, detalle: str):
        super().__init__(detalle)
        self.espera = espera


def _post_una_vez(payload: dict) -> dict:
    req = urllib.request.Request(
        f"{config.LLM_BASE_URL}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
                headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {config.LLM_API_KEY}",
            "User-Agent": "comida-campus/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=config.LLM_TIMEOUT_S) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        cuerpo = e.read().decode("utf-8", "replace")[:500]
        if e.code == 429:
            try:
                espera = float(e.headers.get("retry-after", "10"))
            except ValueError:
                espera = 10.0
            raise ErrorLimite(espera + 1, cuerpo) from e
        raise ErrorLLM(f"El LLM respondió HTTP {e.code}: {cuerpo}") from e
    except urllib.error.URLError as e:
        raise ErrorLLM(
            f"No pude conectarme al LLM en {config.LLM_BASE_URL} ({e.reason}). "
            "Si usas Ollama, verifica que esté abierto (`ollama serve`) y que descargaste el modelo "
            f"(`ollama pull {config.LLM_MODEL}`)."
        ) from e


async def chat(messages: list[dict], tools: list[dict] | None = None) -> dict:
    """Devuelve el mensaje del asistente: {'role','content','tool_calls'?}."""
    payload = {"model": config.LLM_MODEL, "messages": messages, "temperature": config.LLM_TEMPERATURA}
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    datos = await asyncio.to_thread(_post, payload)
    try:
        return datos["choices"][0]["message"]
    except (KeyError, IndexError) as e:
        raise ErrorLLM(f"Respuesta inesperada del LLM: {str(datos)[:300]}") from e
