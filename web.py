"""
Interfaz web (nivel 1 de la arquitectura), sin dependencias extra.

    python web.py                      # chat en http://localhost:8000 y panel en /admin
    python web.py --sin-herramientas   # línea base sin MCP

Multi-organización: el selector de la interfaz cambia de organización sin reiniciar.
Cada organización tiene su propio agente con su propio servidor MCP (CAMPUS_ORG distinto).
"""

import argparse
import asyncio
import json
import os
import sys
import threading
import webbrowser
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from agente import AgenteComida
from agente import config

sys.path.insert(0, str(Path(__file__).resolve().parent / "servidor_mcp"))
from repositorio import Repositorio, listar_organizaciones  # noqa: E402  (panel de vendedores)

STATIC = Path(__file__).resolve().parent / "static"
HTML = STATIC / "index.html"
HTML_PANEL = STATIC / "admin.html"
ORG_POR_DEFECTO = os.getenv("CAMPUS_ORG", "unibague")


class Puente:
    """Mantiene un agente (con su conexión MCP) por organización, en un hilo con su propio event loop."""

    def __init__(self, usar_herramientas: bool):
        self.usar_herramientas = usar_herramientas
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        self.agentes: dict[str, AgenteComida] = {}
        self.candado = threading.Lock()
        self.agente(ORG_POR_DEFECTO)  # arranca la organización por defecto de una vez

    def _ejecutar(self, corrutina):
        return asyncio.run_coroutine_threadsafe(corrutina, self.loop).result()

    def agente(self, org: str) -> AgenteComida:
        if org not in self.agentes:
            nuevo = AgenteComida(usar_herramientas=self.usar_herramientas, organizacion=org)
            self._ejecutar(nuevo.__aenter__())
            self.agentes[org] = nuevo
        return self.agentes[org]

    def responder(self, texto: str, org: str) -> dict:
        with self.candado:  # una pregunta a la vez (prototipo)
            try:
                r = self._ejecutar(self.agente(org).responder(texto))
            except Exception as e:  # nunca dejar la interfaz sin respuesta
                return {"texto": "", "error": f"⚠️ Ocurrió un error inesperado: {e}", "latencia_ms": 0,
                        "pasos": 0, "trazas": []}
        return {"texto": r.texto, "error": r.error, "latencia_ms": r.latencia_total_ms, "pasos": r.pasos,
                "trazas": [{k: v for k, v in asdict(t).items() if k != "resultado"} for t in r.trazas]}


def crear_manejador(puente: Puente, modo: str):
    orgs_validas = {o["id"] for o in listar_organizaciones()}

    def org_de(valor) -> str:
        return valor if valor in orgs_validas else ORG_POR_DEFECTO

    class Manejador(BaseHTTPRequestHandler):
        def _json(self, datos: dict, codigo: int = 200):
            cuerpo = json.dumps(datos, ensure_ascii=False).encode("utf-8")
            self.send_response(codigo)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(cuerpo)))
            self.end_headers()
            self.wfile.write(cuerpo)

        def do_GET(self):
            url = urlparse(self.path)
            org = org_de((parse_qs(url.query).get("org") or [None])[0])
            if url.path == "/api/organizaciones":
                return self._json({"por_defecto": ORG_POR_DEFECTO, "organizaciones": listar_organizaciones()})
            if url.path == "/api/info":
                return self._json({"modelo": config.LLM_MODEL, "modo": modo, "org": org,
                                   "organizacion": Repositorio(organizacion=org).organizacion()["nombre"],
                                   "herramientas": sorted(puente.agente(org).nombres_herramientas)})
            if url.path == "/api/admin/productos":
                repo = Repositorio(organizacion=org)
                return self._json({"org": org, "organizacion": repo.organizacion()["nombre"],
                                   "productos": repo.listar_para_panel()})
            cuerpo = (HTML_PANEL if url.path.startswith("/admin") else HTML).read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(cuerpo)))
            self.end_headers()
            self.wfile.write(cuerpo)

        def do_POST(self):
            datos = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            org = org_de(datos.get("org"))
            if self.path == "/api/chat":
                texto = (datos.get("mensaje") or "").strip()
                if not texto:
                    return self._json({"error": "mensaje vacío"}, 400)
                return self._json(puente.responder(texto, org))
            if self.path == "/api/admin/producto":
                try:
                    r = Repositorio(organizacion=org).actualizar_producto(
                        datos.get("id"), datos.get("precio_min"), datos.get("precio_max"),
                        bool(datos.get("disponible")))
                    return self._json(r)
                except (ValueError, TypeError) as e:
                    return self._json({"error": str(e)}, 400)
            if self.path == "/api/reiniciar":
                puente.agente(org).reiniciar()
                return self._json({"ok": True})
            self._json({"error": "ruta no encontrada"}, 404)

        def log_message(self, *a):
            pass

    return Manejador


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--puerto", type=int, default=8000)
    ap.add_argument("--sin-herramientas", action="store_true")
    ap.add_argument("--no-abrir", action="store_true", help="no abrir el navegador")
    args = ap.parse_args()

    modo = "sin herramientas (línea base)" if args.sin_herramientas else "LLM + MCP"
    puente = Puente(usar_herramientas=not args.sin_herramientas)
    url = f"http://localhost:{args.puerto}"
    print(f"🍽️  Comida Campus en {url}  (modelo {config.LLM_MODEL}, {modo}). Ctrl+C para salir.")
    print(f"🧾 Panel de vendedores en {url}/admin")
    if not args.no_abrir:
        webbrowser.open(url)
    try:
        ThreadingHTTPServer(("127.0.0.1", args.puerto), crear_manejador(puente, modo)).serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
