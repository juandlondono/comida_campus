"""
Interfaz web mínima (nivel 1 de la Fig. 1), sin dependencias extra.
Incluye el panel de vendedores en /admin (edita precios y disponibilidad en la base de datos).

    python web.py                      # abre http://localhost:8000
    python web.py --sin-herramientas   # línea base sin MCP
"""

import argparse
import asyncio
import json
import sys
import threading
import webbrowser
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from agente import AgenteComida
from agente import config

sys.path.insert(0, str(Path(__file__).resolve().parent / "servidor_mcp"))
from repositorio import Repositorio  # noqa: E402  (solo para el panel de vendedores)

STATIC = Path(__file__).resolve().parent / "static"
HTML = STATIC / "index.html"
HTML_PANEL = STATIC / "admin.html"


class Puente:
    """Mantiene el agente (y su conexión MCP) vivo en un hilo con su propio event loop."""

    def __init__(self, usar_herramientas: bool):
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        self.agente = AgenteComida(usar_herramientas=usar_herramientas)
        self._ejecutar(self.agente.__aenter__())
        self.candado = threading.Lock()

    def _ejecutar(self, corrutina):
        return asyncio.run_coroutine_threadsafe(corrutina, self.loop).result()

    def responder(self, texto: str) -> dict:
        with self.candado:  # una pregunta a la vez (prototipo de un solo usuario)
            r = self._ejecutar(self.agente.responder(texto))
        return {"texto": r.texto, "error": r.error, "latencia_ms": r.latencia_total_ms, "pasos": r.pasos,
                "trazas": [{k: v for k, v in asdict(t).items() if k != "resultado"} for t in r.trazas]}


def crear_manejador(puente: Puente, modo: str, repo: Repositorio):
    class Manejador(BaseHTTPRequestHandler):
        def _json(self, datos: dict, codigo: int = 200):
            cuerpo = json.dumps(datos, ensure_ascii=False).encode("utf-8")
            self.send_response(codigo)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(cuerpo)))
            self.end_headers()
            self.wfile.write(cuerpo)

        def do_GET(self):
            if self.path == "/api/info":
                return self._json({"modelo": config.LLM_MODEL, "modo": modo,
                                   "organizacion": repo.organizacion()["nombre"],
                                   "herramientas": sorted(puente.agente.nombres_herramientas)})
            if self.path == "/api/admin/productos":
                return self._json({"organizacion": repo.organizacion()["nombre"],
                                   "productos": repo.listar_para_panel()})
            cuerpo = (HTML_PANEL if self.path.startswith("/admin") else HTML).read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(cuerpo)))
            self.end_headers()
            self.wfile.write(cuerpo)

        def do_POST(self):
            datos = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            if self.path == "/api/chat":
                texto = (datos.get("mensaje") or "").strip()
                if not texto:
                    return self._json({"error": "mensaje vacío"}, 400)
                return self._json(puente.responder(texto))
            if self.path == "/api/admin/producto":
                try:
                    r = repo.actualizar_producto(datos.get("id"), datos.get("precio_min"),
                                                 datos.get("precio_max"), bool(datos.get("disponible")))
                    return self._json(r)
                except (ValueError, TypeError) as e:
                    return self._json({"error": str(e)}, 400)
            if self.path == "/api/reiniciar":
                puente.agente.reiniciar()
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
        ThreadingHTTPServer(("127.0.0.1", args.puerto), crear_manejador(puente, modo, Repositorio())).serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
