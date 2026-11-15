"""
LLM FALSO compatible con /v1/chat/completions, para probar el agente sin Ollama ni internet.
Imita a un modelo que pide herramientas. NO es inteligente: solo sirve para pruebas.

    python tests/llm_falso.py          # escucha en http://127.0.0.1:8765/v1
    LLM_BASE_URL=http://127.0.0.1:8765/v1 python chat.py --traza
"""

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def _decidir(mensajes: list[dict]) -> dict:
    ultimo = mensajes[-1]
    if ultimo["role"] == "tool":
        datos = json.loads(ultimo["content"])
        if "error" in datos:  # recuperación ante error: pide la lista de cafeterías
            return _llamada("get_cafeterias", {})
        res = datos.get("resultados") or datos.get("establecimientos") or []
        if not res:
            return {"role": "assistant", "content": "No encontré opciones con esas condiciones."}
        r = res[0]
        donde = ", ".join(r.get("donde", [])) or r.get("ubicacion", "")
        texto = f"Te recomiendo {r.get('producto', r.get('nombre'))} ({r.get('precio', '')}) en {donde}."
        return {"role": "assistant", "content": texto}

    texto = ultimo["content"].lower()
    if "menú" in texto or "menu" in texto:
        return _llamada("get_menu", {"cafeteria_id": "cafeteria_que_no_existe"})
    args = {}
    if m := re.search(r"(\d+)[\.\s]?(000|mil|k)", texto):
        args["presupuesto_max"] = int(m.group(1)) * 1000
    if m := re.search(r"(\d+)\s*min", texto):
        args["tiempo_max_min"] = int(m.group(1))
    if "vegetarian" in texto:
        args["vegetariano"] = True
    if "ingenier" in texto:
        args["zona_usuario"] = "Ingeniería"
    return _llamada("buscar_opciones", args)


def _llamada(nombre: str, args: dict) -> dict:
    return {"role": "assistant", "content": None, "tool_calls": [
        {"id": "call_1", "type": "function", "function": {"name": nombre, "arguments": json.dumps(args)}}]}


class Manejador(BaseHTTPRequestHandler):
    def do_POST(self):
        cuerpo = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        salida = json.dumps({"choices": [{"message": _decidir(cuerpo["messages"])}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(salida)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    print("LLM falso en http://127.0.0.1:8765/v1")
    ThreadingHTTPServer(("127.0.0.1", 8765), Manejador).serve_forever()
