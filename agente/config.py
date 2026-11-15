"""Configuración leída de variables de entorno o del archivo .env (sin dependencias extra)."""

import os
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent


def _cargar_env() -> None:
    archivo = RAIZ / ".env"
    if not archivo.exists():
        return
    for linea in archivo.read_text(encoding="utf-8").splitlines():
        linea = linea.strip()
        if not linea or linea.startswith("#") or "=" not in linea:
            continue
        clave, valor = linea.split("=", 1)
        os.environ.setdefault(clave.strip(), valor.strip().strip('"').strip("'"))


_cargar_env()

# Por defecto: Ollama local. Cualquier proveedor compatible con OpenAI sirve
# (Groq, OpenRouter, LM Studio, OpenAI...) cambiando estas 3 variables.
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "http://localhost:11434/v1").rstrip("/")
LLM_MODEL = os.getenv("LLM_MODEL", "qwen2.5:7b")
LLM_API_KEY = os.getenv("LLM_API_KEY", "ollama")
LLM_TEMPERATURA = float(os.getenv("LLM_TEMPERATURA", "0.2"))
LLM_TIMEOUT_S = float(os.getenv("LLM_TIMEOUT_S", "180"))

MAX_PASOS_HERRAMIENTAS = int(os.getenv("MAX_PASOS_HERRAMIENTAS", "6"))
SERVIDOR_MCP = RAIZ / "servidor_mcp" / "servidor.py"
CARPETA_LOGS = RAIZ / "logs"
