"""
Agente: LLM + cliente MCP (niveles 2 y 3 de la Fig. 1 del artículo).

Ciclo (estilo ReAct / tool calling):
  1. El LLM recibe la conversación y la lista de herramientas MCP.
  2. Si pide herramientas, el cliente MCP las ejecuta en el servidor y se devuelven los resultados.
  3. Se repite hasta que el LLM responde en texto (o se alcanza MAX_PASOS_HERRAMIENTAS).

Cada llamada queda registrada en una "traza" (herramienta, argumentos, éxito, latencia),
que luego sirve para medir selección de herramientas y precisión de parámetros.
"""

from __future__ import annotations

import json
import os
import re
import sys
import unicodedata
import time
import uuid
from contextlib import AsyncExitStack
from dataclasses import asdict, dataclass, field
from datetime import datetime

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from . import config, llm

PROMPT_SISTEMA = """Eres "Comida Campus", asistente que recomienda qué comer en el lugar descrito en CONTEXTO.

Reglas:
- NUNCA inventes productos, precios, horarios ni tiempos: usa las herramientas.
- Recomendaciones y "¿dónde venden X?": usa `buscar_opciones` SOLO con los parámetros que el usuario dijo.
  No pongas vegetariano=true si no lo pidió. "10 mil"/"10k" -> 10000.
- Si quiere comer Y tomar algo, no pongas `tipo`; arma un combo de comida + bebida que no supere el presupuesto.
- Si pregunta por "bebidas" sin decir frías o calientes, usa tipo="bebida" (no categoria).
- vegetariano "por_confirmar": di que hay que confirmar ingredientes. No hay datos veganos.
- Precios en rango o "desde": dilos así.
- No hay horarios de atención ni tiempos de fila medidos: no digas minutos; sí comunica los avisos de afluencia.
- Sin resultados: dilo y sugiere qué condición relajar.

Responde en español, en pocas líneas: de 1 a 3 opciones con producto, precio y dónde.
"""

PROMPT_SIN_HERRAMIENTAS = """Eres "Comida Campus", un asistente que recomienda qué comer en el lugar descrito en CONTEXTO.
No tienes acceso a ninguna base de datos ni herramienta: responde solo con tu conocimiento general.
Responde en español, de forma breve."""


@dataclass
class LlamadaHerramienta:
    herramienta: str
    argumentos: dict          # tal como los propuso el LLM (lo que mide la evaluación)
    ok: bool
    latencia_ms: int
    resultado: str
    descartados: list = field(default_factory=list)  # parámetros que el usuario nunca mencionó


@dataclass
class Respuesta:
    texto: str
    trazas: list[LlamadaHerramienta] = field(default_factory=list)
    latencia_total_ms: int = 0
    pasos: int = 0
    error: str | None = None


def _compactar(esquema):
    """Quita 'title', 'default' y los anyOf con null del JSON Schema: menos tokens para el LLM."""
    if isinstance(esquema, list):
        return [_compactar(x) for x in esquema]
    if not isinstance(esquema, dict):
        return esquema
    e = {k: _compactar(v) for k, v in esquema.items() if k not in ("title", "default")}
    if "anyOf" in e:
        tipos = [t for t in e["anyOf"] if t.get("type") != "null"]
        if len(tipos) == 1:
            del e["anyOf"]
            e.update(tipos[0])
    return e


def _mcp_a_openai(tool) -> dict:
    """Convierte la definición de una herramienta MCP al formato 'tools' de la API de OpenAI."""
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description or "",
            "parameters": _compactar(tool.input_schema or {"type": "object", "properties": {}}),
        },
    }


def _norm(texto: str) -> str:
    t = unicodedata.normalize("NFKD", (texto or "").lower())
    return "".join(c for c in t if not unicodedata.combining(c))


def _sin_respaldo(args: dict, texto_usuario: str) -> list[str]:
    """Parámetros de búsqueda que no aparecen en lo que escribió el usuario (el LLM los inventó)."""
    u = _norm(texto_usuario)
    hay_numero = bool(re.search(r"\d|\bmil\b|\bk\b|media hora|\bhora", u))
    malos = []
    for clave, valor in args.items():
        if clave in ("presupuesto_max", "tiempo_max_min") and valor is not None and not hay_numero:
            malos.append(clave)
        elif clave == "vegetariano" and valor is True and "vegetar" not in u and "sin carne" not in u:
            malos.append(clave)
        elif clave in ("zona_usuario", "zona", "establecimiento") and isinstance(valor, str) and valor:
            palabras = [w for w in re.findall(r"[a-z0-9]+", _norm(valor)) if len(w) > 3]
            if palabras and not any(w[:5] in u for w in palabras):
                malos.append(clave)
    return malos


def _parsear_args(crudos) -> dict:
    if isinstance(crudos, dict):
        return crudos
    if not crudos:
        return {}
    try:
        valor = json.loads(crudos)
        return valor if isinstance(valor, dict) else {}
    except json.JSONDecodeError:
        return {}


class AgenteComida:
    """Uso:
        async with AgenteComida() as agente:
            r = await agente.responder("tengo 10 mil y 15 minutos...")
    """

    def __init__(self, usar_herramientas: bool = True, guardar_log: bool = True):
        self.usar_herramientas = usar_herramientas
        self.guardar_log = guardar_log
        self.id_sesion = uuid.uuid4().hex[:8]
        self._pila = AsyncExitStack()
        self._sesion: ClientSession | None = None
        self.herramientas_openai: list[dict] = []
        self.nombres_herramientas: set[str] = set()
        self.contexto = "organización no especificada"
        self.reiniciar()

    # ----------------------------------------------------------- ciclo de vida
    async def __aenter__(self) -> "AgenteComida":
        params = StdioServerParameters(
            command=sys.executable, args=[str(config.SERVIDOR_MCP)], env=dict(os.environ)
        )
        leer, escribir = await self._pila.enter_async_context(stdio_client(params))
        self._sesion = await self._pila.enter_async_context(ClientSession(leer, escribir))
        inicio = await self._sesion.initialize()
        # El servidor MCP anuncia qué organización atiende (nombre, establecimientos, fecha de datos).
        # La línea base sin herramientas también recibe este contexto, para que la comparación sea justa.
        self.contexto = inicio.instructions or self.contexto
        if self.usar_herramientas:
            lista = await self._sesion.list_tools()
            self.herramientas_openai = [_mcp_a_openai(t) for t in lista.tools]
            self.nombres_herramientas = {t.name for t in lista.tools}
        self.reiniciar()
        return self

    async def __aexit__(self, *exc) -> None:
        await self._pila.aclose()

    def reiniciar(self) -> None:
        prompt = PROMPT_SISTEMA if self.usar_herramientas else PROMPT_SIN_HERRAMIENTAS
        prompt += f"\nCONTEXTO: {self.contexto}"
        ahora = datetime.now().strftime("%Y-%m-%d %H:%M")
        self.mensajes: list[dict] = [{"role": "system", "content": f"{prompt}\nFecha y hora del sistema: {ahora}."}]

    # --------------------------------------------------------------- herramientas
    async def _ejecutar(self, nombre: str, args: dict) -> LlamadaHerramienta:
        """Valida los parámetros contra lo que dijo el usuario y ejecuta la herramienta en el servidor MCP."""
        texto_usuario = " ".join(m["content"] for m in self.mensajes if m["role"] == "user")
        descartados = _sin_respaldo(args, texto_usuario)
        args_limpios = {k: v for k, v in args.items() if k not in descartados}
        traza = await self._llamar(nombre, args_limpios)
        traza.argumentos, traza.descartados = args, descartados
        if descartados:
            traza.resultado = json.dumps({"aviso": f"Se ignoraron {descartados}: el usuario no los mencionó."},
                                         ensure_ascii=False) + "\n" + traza.resultado
        return traza

    async def _llamar(self, nombre: str, args: dict) -> LlamadaHerramienta:
        t0 = time.perf_counter()
        if nombre not in self.nombres_herramientas:
            texto, ok = json.dumps({"error": f"La herramienta '{nombre}' no existe.",
                                    "disponibles": sorted(self.nombres_herramientas)}, ensure_ascii=False), False
        else:
            try:
                r = await self._sesion.call_tool(nombre, args)
                texto = "\n".join(getattr(c, "text", "") for c in (r.content or []))
                ok = not getattr(r, "is_error", False) and '"error"' not in texto[:20]
            except Exception as e:  # fallo de la herramienta / del servidor
                texto, ok = json.dumps({"error": f"Fallo al ejecutar {nombre}: {e}"}, ensure_ascii=False), False
        return LlamadaHerramienta(nombre, args, ok, int((time.perf_counter() - t0) * 1000), texto)

    def _tool_call_en_texto(self, contenido: str | None) -> dict | None:
        """Algunos modelos pequeños escriben la llamada como JSON en el texto en vez de usar tool_calls."""
        if not contenido:
            return None
        s = contenido.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        if not (s.startswith("{") and s.endswith("}")):
            return None
        try:
            obj = json.loads(s)
        except json.JSONDecodeError:
            return None
        nombre = obj.get("name") or obj.get("function")
        if nombre in self.nombres_herramientas:
            args = obj.get("arguments", obj.get("parameters", {}))
            return {"id": f"txt_{uuid.uuid4().hex[:6]}", "type": "function",
                    "function": {"name": nombre, "arguments": json.dumps(args if isinstance(args, dict) else {})}}
        return None

    # ------------------------------------------------------------------ respuesta
    async def responder(self, texto_usuario: str) -> Respuesta:
        t0 = time.perf_counter()
        largo_inicial = len(self.mensajes)
        self.mensajes.append({"role": "user", "content": texto_usuario})
        respuesta = Respuesta(texto="")
        herramientas = self.herramientas_openai if self.usar_herramientas else None

        try:
            for paso in range(config.MAX_PASOS_HERRAMIENTAS + 1):
                respuesta.pasos = paso + 1
                # En el último paso no se ofrecen herramientas: se fuerza una respuesta final.
                ofrecer = herramientas if paso < config.MAX_PASOS_HERRAMIENTAS else None
                msg = await llm.chat(self.mensajes, ofrecer)
                llamadas = (msg.get("tool_calls") or []) if ofrecer else []  # sin herramientas ofrecidas, se ignoran
                if not llamadas and ofrecer:
                    falsa = self._tool_call_en_texto(msg.get("content"))
                    if falsa:
                        llamadas = [falsa]

                if not llamadas:
                    respuesta.texto = (msg.get("content") or "").strip()
                    self.mensajes.append({"role": "assistant", "content": respuesta.texto})
                    break

                self.mensajes.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": llamadas})
                for i, tc in enumerate(llamadas):
                    fn = tc.get("function", {})
                    traza = await self._ejecutar(fn.get("name", ""), _parsear_args(fn.get("arguments")))
                    respuesta.trazas.append(traza)
                    self.mensajes.append({
                        "role": "tool", "tool_call_id": tc.get("id") or f"call_{i}",
                        "name": traza.herramienta, "content": traza.resultado,
                    })
        except llm.ErrorLLM as e:
            respuesta.error = str(e)
            respuesta.texto = f"⚠️ {e}"
            del self.mensajes[largo_inicial:]  # deja el historial como estaba antes del turno fallido

        respuesta.latencia_total_ms = int((time.perf_counter() - t0) * 1000)
        self._log(texto_usuario, respuesta)
        return respuesta

    def _log(self, pregunta: str, r: Respuesta) -> None:
        """Guarda cada turno en logs/conversaciones.jsonl (insumo para la evaluación)."""
        if not self.guardar_log:
            return
        config.CARPETA_LOGS.mkdir(exist_ok=True)
        registro = {
            "fecha": datetime.now().isoformat(timespec="seconds"), "sesion": self.id_sesion,
            "modelo": config.LLM_MODEL, "con_herramientas": self.usar_herramientas,
            "pregunta": pregunta, "respuesta": r.texto, "error": r.error, "pasos": r.pasos,
            "latencia_total_ms": r.latencia_total_ms,
            "trazas": [{k: v for k, v in asdict(t).items() if k != "resultado"} for t in r.trazas],
        }
        with open(config.CARPETA_LOGS / "conversaciones.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(registro, ensure_ascii=False) + "\n")
