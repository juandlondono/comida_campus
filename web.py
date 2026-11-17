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
import time
import webbrowser
from http.cookies import SimpleCookie
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from agente import AgenteComida
from agente import config

sys.path.insert(0, str(Path(__file__).resolve().parent / "servidor_mcp"))
from repositorio import CATEGORIAS, Cuentas, Repositorio, listar_organizaciones  # noqa: E402

STATIC = Path(__file__).resolve().parent / "static"
HTML = STATIC / "index.html"
HTML_PANEL = STATIC / "admin.html"
ORG_POR_DEFECTO = os.getenv("CAMPUS_ORG", "unibague")
COOKIE = "cc_sesion"
MAX_FALLOS, VENTANA_FALLOS = 5, 300  # 5 intentos fallidos cada 5 minutos por usuario


class LimiteIntentos:
    """Frena ataques de fuerza bruta contra el inicio de sesión."""

    def __init__(self):
        self.fallos: dict[str, list[float]] = {}
        self.candado = threading.Lock()

    def bloqueado(self, usuario: str) -> bool:
        ahora = time.time()
        with self.candado:
            recientes = [t for t in self.fallos.get(usuario, []) if ahora - t < VENTANA_FALLOS]
            self.fallos[usuario] = recientes
            return len(recientes) >= MAX_FALLOS

    def fallo(self, usuario: str) -> None:
        with self.candado:
            self.fallos.setdefault(usuario, []).append(time.time())

    def exito(self, usuario: str) -> None:
        with self.candado:
            self.fallos.pop(usuario, None)


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
    cuentas = Cuentas()
    limite = LimiteIntentos()

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

        # --- sesión ---------------------------------------------------------------
        def _token(self) -> str | None:
            c = SimpleCookie(self.headers.get("Cookie", ""))
            return c[COOKIE].value if COOKIE in c else None

        def _usuario(self) -> dict | None:
            return cuentas.usuario_de_sesion(self._token())

        def _json_con_cookie(self, datos: dict, token: str | None):
            cuerpo = json.dumps(datos, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(cuerpo)))
            # HttpOnly: JavaScript no puede leerla. SameSite=Strict: otros sitios no pueden usarla.
            # (En producción, con HTTPS, se agrega también "Secure".)
            vida = "Max-Age=28800" if token else "Max-Age=0"
            self.send_header("Set-Cookie", f"{COOKIE}={token or ''}; HttpOnly; SameSite=Strict; Path=/; {vida}")
            self.end_headers()
            self.wfile.write(cuerpo)

        def _org_del_panel(self, usuario: dict, pedida: str | None) -> str:
            """Un vendedor solo puede abrir las organizaciones de sus establecimientos."""
            if usuario["rol"] == "admin":
                return org_de(pedida)
            permitidas = usuario["organizaciones"]
            return pedida if pedida in permitidas else permitidas[0]

        def do_GET(self):
            url = urlparse(self.path)
            org = org_de((parse_qs(url.query).get("org") or [None])[0])
            if url.path == "/api/organizaciones":
                return self._json({"por_defecto": ORG_POR_DEFECTO, "organizaciones": listar_organizaciones()})
            if url.path == "/api/info":
                return self._json({"modelo": config.LLM_MODEL, "modo": modo, "org": org,
                                   "organizacion": Repositorio(organizacion=org).organizacion()["nombre"],
                                   "herramientas": sorted(puente.agente(org).nombres_herramientas)})
            if url.path == "/api/yo":
                return self._json({"configurado": cuentas.hay_usuarios(), "usuario": self._usuario()})
            if url.path.startswith("/api/admin/"):
                usuario = self._usuario()
                if not usuario:
                    return self._json({"error": "Inicia sesión."}, 401)
                if url.path == "/api/admin/productos":
                    pedida = (parse_qs(url.query).get("org") or [None])[0]
                    org = self._org_del_panel(usuario, pedida)
                    repo = Repositorio(organizacion=org)
                    productos = [p for p in repo.listar_para_panel() if cuentas.puede_editar(usuario, p)]
                    orgs = listar_organizaciones()
                    if usuario["rol"] != "admin":
                        orgs = [o for o in orgs if o["id"] in usuario["organizaciones"]]
                    destinos = [d for d in repo.destinos() if cuentas.puede_editar(usuario, d)]
                    return self._json({"org": org, "organizacion": repo.organizacion()["nombre"],
                                       "organizaciones": orgs, "productos": productos,
                                       "destinos": destinos, "categorias": CATEGORIAS})
                if usuario["rol"] != "admin":
                    return self._json({"error": "Solo un administrador puede hacer esto."}, 403)
                if url.path == "/api/admin/usuarios":
                    return self._json({"usuarios": cuentas.listar_usuarios(),
                                       "establecimientos": cuentas.establecimientos()})
                return self._json({"error": "ruta no encontrada"}, 404)
            cuerpo = (HTML_PANEL if url.path.startswith("/admin") else HTML).read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(cuerpo)))
            self.end_headers()
            self.wfile.write(cuerpo)

        def do_POST(self):
            # Solo JSON: un formulario de otro sitio no puede enviar este tipo sin permiso (protección CSRF).
            if not self.headers.get("Content-Type", "").startswith("application/json"):
                return self._json({"error": "Se esperaba JSON."}, 415)
            try:
                datos = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            except json.JSONDecodeError:
                return self._json({"error": "JSON inválido."}, 400)
            org = org_de(datos.get("org"))
            if self.path == "/api/setup":  # solo funciona mientras no exista ningún usuario
                if cuentas.hay_usuarios():
                    return self._json({"error": "La plataforma ya está configurada."}, 403)
                try:
                    cuentas.crear_usuario(datos.get("usuario"), datos.get("nombre"), datos.get("contrasena"), "admin")
                except ValueError as e:
                    return self._json({"error": str(e)}, 400)
                token = cuentas.iniciar_sesion(datos.get("usuario"), datos.get("contrasena"))
                return self._json_con_cookie({"ok": True}, token)
            if self.path == "/api/login":
                nombre = (datos.get("usuario") or "").strip().lower()
                if limite.bloqueado(nombre):
                    return self._json({"error": "Demasiados intentos fallidos. Espera unos minutos."}, 429)
                token = cuentas.iniciar_sesion(nombre, datos.get("contrasena"))
                if not token:
                    limite.fallo(nombre)
                    return self._json({"error": "Usuario o contraseña incorrectos."}, 401)
                limite.exito(nombre)
                return self._json_con_cookie({"ok": True}, token)
            if self.path == "/api/logout":
                if self._token():
                    cuentas.cerrar_sesion(self._token())
                return self._json_con_cookie({"ok": True}, None)
            if self.path == "/api/chat":
                texto = (datos.get("mensaje") or "").strip()
                if not texto:
                    return self._json({"error": "mensaje vacío"}, 400)
                return self._json(puente.responder(texto, org))
            if self.path.startswith("/api/admin/"):
                usuario = self._usuario()
                if not usuario:
                    return self._json({"error": "Inicia sesión."}, 401)
                try:
                    if self.path == "/api/admin/producto":
                        producto = cuentas.producto(datos.get("id"))
                        # El permiso se verifica AQUÍ, en el servidor, no solo ocultando botones en la página.
                        if not producto or not cuentas.puede_editar(usuario, producto):
                            return self._json({"error": "No tienes permiso para editar este producto."}, 403)
                        return self._json(Repositorio(organizacion=org).actualizar_producto(
                            producto["id"], datos.get("precio_min"), datos.get("precio_max"),
                            bool(datos.get("disponible"))))
                    if self.path == "/api/admin/producto/crear":
                        destino = datos.get("destino") or {}
                        clave = "catalogo_id" if destino.get("tipo") == "catalogo" else "establecimiento_id"
                        if not cuentas.puede_editar(usuario, {clave: destino.get("id")}):
                            return self._json({"error": "No puedes agregar productos en ese lugar."}, 403)
                        return self._json(Repositorio(organizacion=org).crear_producto(destino, datos))
                    if self.path in ("/api/admin/producto/editar", "/api/admin/producto/eliminar"):
                        producto = cuentas.producto(datos.get("id"))
                        if not producto or not cuentas.puede_editar(usuario, producto):
                            return self._json({"error": "No tienes permiso para modificar este producto."}, 403)
                        repo = Repositorio(organizacion=org)
                        if self.path.endswith("editar"):
                            return self._json(repo.editar_producto(producto["id"], datos))
                        return self._json(repo.eliminar_producto(producto["id"]))
                    if usuario["rol"] != "admin":
                        return self._json({"error": "Solo un administrador puede hacer esto."}, 403)
                    if self.path == "/api/admin/usuarios":
                        return self._json(cuentas.crear_usuario(
                            datos.get("usuario"), datos.get("nombre"), datos.get("contrasena"),
                            datos.get("rol"), datos.get("establecimientos")))
                    if self.path == "/api/admin/usuarios/estado":
                        if int(datos.get("id", 0)) == usuario["id"] and not datos.get("activo"):
                            return self._json({"error": "No puedes desactivarte a ti mismo."}, 400)
                        return self._json(cuentas.cambiar_estado(int(datos.get("id", 0)), bool(datos.get("activo"))))
                    if self.path == "/api/admin/usuarios/contrasena":
                        return self._json(cuentas.cambiar_contrasena(int(datos.get("id", 0)), datos.get("contrasena")))
                except (ValueError, TypeError) as e:
                    return self._json({"error": str(e)}, 400)
                return self._json({"error": "ruta no encontrada"}, 404)
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
    print(f"🧾 Panel de vendedores en {url}/admin  (la primera vez te pide crear el administrador)")
    if not args.no_abrir:
        webbrowser.open(url)
    try:
        ThreadingHTTPServer(("127.0.0.1", args.puerto), crear_manejador(puente, modo)).serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
