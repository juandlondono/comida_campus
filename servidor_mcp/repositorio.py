"""
Capa de datos de Comida Campus sobre SQLite (nivel 5 de la arquitectura).

Es la ÚNICA parte del sistema que sabe que hay una base de datos. El servidor MCP y el
panel de vendedores la usan; el agente y el LLM nunca la tocan. Cambiar a PostgreSQL
o MySQL significa reescribir solo este archivo.

    python servidor_mcp/repositorio.py                       # (re)crea data/campus.db desde las semillas
                                                             # (BORRA usuarios y cambios del panel)
    python servidor_mcp/repositorio.py clave <usuario>       # restablece la contraseña de un usuario
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
RUTA_BD = Path(os.getenv("CAMPUS_BD", RAIZ / "data" / "campus.db"))
CARPETA_SEMILLAS = RAIZ / "data" / "semillas"
ESQUEMA = Path(__file__).resolve().parent / "esquema.sql"
ORG_POR_DEFECTO = os.getenv("CAMPUS_ORG", "unibague")


def _texto_busqueda(p: dict) -> str:
    return " ".join([p["nombre"], p["categoria"], " ".join(p.get("opciones") or [])])


# ---------------------------------------------------------------- creación / semillas
def crear_bd(ruta: Path = RUTA_BD, carpeta: Path = CARPETA_SEMILLAS) -> list[str]:
    """Crea la base de datos desde cero con todos los JSON de data/semillas/. Devuelve las organizaciones."""
    ruta.parent.mkdir(parents=True, exist_ok=True)
    if ruta.exists():
        ruta.unlink()
    con = sqlite3.connect(ruta)
    con.executescript(ESQUEMA.read_text(encoding="utf-8"))
    orgs = []
    for archivo in sorted(carpeta.glob("*.json")):
        orgs.append(_importar(con, json.loads(archivo.read_text(encoding="utf-8"))))
    con.commit()
    con.close()
    return orgs


def _importar(con: sqlite3.Connection, datos: dict) -> str:
    m = datos["metadatos"]
    org = m["organizacion"]
    ahora = datetime.now().isoformat(timespec="seconds")
    pico = m.get("hora_pico_almuerzo") or [None, None]
    con.execute(
        "INSERT INTO organizaciones VALUES (?,?,?,?,?,?,?,?)",
        (org["id"], org["nombre"], org.get("ciudad"), m.get("moneda", "COP"), pico[0], pico[1],
         m.get("fecha_consulta"), m.get("descripcion")),
    )
    zonas = {}
    for e in datos["establecimientos"]:
        if e["zona"] not in zonas:
            cur = con.execute("INSERT INTO zonas (organizacion_id, nombre) VALUES (?,?)", (org["id"], e["zona"]))
            zonas[e["zona"]] = cur.lastrowid
    for cat_id, prods in datos.get("catalogos", {}).items():
        con.execute("INSERT INTO catalogos VALUES (?,?,?)", (cat_id, org["id"], cat_id))
        for p in prods:
            _insertar_producto(con, p, None, cat_id, ahora)
    for e in datos["establecimientos"]:
        a = e.get("afluencia") or {}
        con.execute(
            "INSERT INTO establecimientos VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)",
            (e["id"], org["id"], e["nombre"], e.get("marca"), e.get("tipo"), e.get("ubicacion"), zonas[e["zona"]],
             json.dumps(e.get("referencias", []), ensure_ascii=False),
             json.dumps(e["horario"], ensure_ascii=False) if e.get("horario") else None,
             a.get("nivel"), a.get("cuando"), a.get("nota"), e.get("descripcion"),
             e.get("advertencia_vegetariana"), e.get("catalogo")),
        )
        for p in e.get("productos", []):
            _insertar_producto(con, p, e["id"], None, ahora)
    return org["id"]


def _insertar_producto(con, p: dict, est_id, cat_id, ahora: str) -> None:
    con.execute(
        "INSERT INTO productos VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (p["id"], est_id, cat_id, p["nombre"], p["categoria"], p.get("precio_min"), p.get("precio_max"),
         p.get("vegetariano", "por_confirmar"), p.get("nota"),
         json.dumps(p["opciones"], ensure_ascii=False) if p.get("opciones") else None,
         p.get("disponible_hasta"), 0 if p.get("disponible") is False else 1, ahora),
    )
    con.execute("INSERT INTO productos_fts (producto_id, texto) VALUES (?,?)", (p["id"], _texto_busqueda(p)))


def listar_organizaciones(ruta: Path = RUTA_BD) -> list[dict]:
    """Organizaciones disponibles en la base de datos (para el selector de la interfaz)."""
    if not ruta.exists():
        crear_bd(ruta)
    con = sqlite3.connect(ruta)
    con.row_factory = sqlite3.Row
    with con:
        filas = con.execute("SELECT id, nombre, ciudad FROM organizaciones ORDER BY rowid").fetchall()
    con.close()
    return [dict(f) for f in filas]


# ---------------------------------------------------------------------- repositorio
def _migrar(ruta: Path) -> None:
    """Aplica el esquema (todo es CREATE ... IF NOT EXISTS): agrega tablas nuevas a una base
    existente sin tocar sus datos."""
    con = sqlite3.connect(ruta)
    con.executescript(ESQUEMA.read_text(encoding="utf-8"))
    con.close()


class Repositorio:
    def __init__(self, ruta: Path = RUTA_BD, organizacion: str = ORG_POR_DEFECTO):
        if not ruta.exists():
            crear_bd(ruta)
        _migrar(ruta)
        self.ruta, self.org = ruta, organizacion

    def _con(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.ruta)
        con.row_factory = sqlite3.Row
        return con

    # --- lectura (lo que usa el servidor MCP) ---------------------------------
    def organizacion(self) -> dict:
        with self._con() as con:
            fila = con.execute("SELECT * FROM organizaciones WHERE id = ?", (self.org,)).fetchone()
        if not fila:
            raise ValueError(f"No existe la organización '{self.org}' en {self.ruta}")
        return dict(fila)

    def cargar(self) -> dict:
        """Devuelve los datos de la organización con la misma forma que usa el servidor MCP."""
        with self._con() as con:
            org = self.organizacion()
            ests = con.execute(
                "SELECT e.*, z.nombre AS zona FROM establecimientos e LEFT JOIN zonas z ON z.id = e.zona_id "
                "WHERE e.organizacion_id = ? AND e.activo = 1 ORDER BY e.rowid", (self.org,)).fetchall()
            prods = con.execute(
                "SELECT p.* FROM productos p LEFT JOIN establecimientos e ON e.id = p.establecimiento_id "
                "LEFT JOIN catalogos c ON c.id = p.catalogo_id "
                "WHERE e.organizacion_id = ? OR c.organizacion_id = ? ORDER BY p.rowid", (self.org, self.org)).fetchall()

        catalogos, propios = {}, {}
        for p in prods:
            d = {"id": p["id"], "nombre": p["nombre"], "categoria": p["categoria"],
                 "precio_min": p["precio_min"], "precio_max": p["precio_max"], "vegetariano": p["vegetariano"],
                 "disponible": bool(p["disponible"])}
            if p["nota"]:
                d["nota"] = p["nota"]
            if p["opciones"]:
                d["opciones"] = json.loads(p["opciones"])
            if p["disponible_hasta"]:
                d["disponible_hasta"] = p["disponible_hasta"]
            if p["catalogo_id"]:
                catalogos.setdefault(p["catalogo_id"], []).append(d)
            else:
                propios.setdefault(p["establecimiento_id"], []).append(d)

        establecimientos = []
        for e in ests:
            d = {"id": e["id"], "nombre": e["nombre"], "marca": e["marca"], "tipo": e["tipo"],
                 "ubicacion": e["ubicacion"], "zona": e["zona"],
                 "referencias": json.loads(e["referencias"] or "[]"),
                 "horario": json.loads(e["horario"]) if e["horario"] else None,
                 "afluencia": {"nivel": e["afluencia_nivel"], "cuando": e["afluencia_cuando"],
                               "nota": e["afluencia_nota"]} if e["afluencia_nivel"] else None,
                 "catalogo": e["catalogo_id"], "productos": propios.get(e["id"], [])}
            for k in ("descripcion", "advertencia_vegetariana"):
                if e[k]:
                    d[k] = e[k]
            establecimientos.append(d)

        return {
            "metadatos": {"organizacion": org["nombre"], "fecha_consulta": org["fecha_datos"],
                          "moneda": org["moneda"],
                          "hora_pico": (org["hora_pico_inicio"], org["hora_pico_fin"]),
                          "zonas": list(dict.fromkeys(e["zona"] for e in establecimientos))},
            "catalogos": catalogos,
            "establecimientos": establecimientos,
        }

    def buscar_texto(self, palabras: list[str]) -> set[str]:
        """Ids de productos que contienen TODAS las palabras (por prefijo), usando el índice invertido FTS5."""
        limpias = [re.sub(r"[^\w]", "", unicodedata.normalize("NFKC", w)) for w in palabras]
        limpias = [w for w in limpias if w]
        if not limpias:
            return set()
        consulta = " AND ".join(f'"{w}"*' for w in limpias)
        with self._con() as con:
            filas = con.execute("SELECT producto_id FROM productos_fts WHERE productos_fts MATCH ?",
                                (consulta,)).fetchall()
        return {f["producto_id"] for f in filas}

    # --- escritura (solo el panel de vendedores; el chatbot es de solo lectura) ---
    def listar_para_panel(self) -> list[dict]:
        with self._con() as con:
            filas = con.execute(
                "SELECT p.id, p.nombre, p.categoria, p.precio_min, p.precio_max, p.vegetariano, p.disponible, "
                "p.actualizado, p.establecimiento_id, p.catalogo_id, COALESCE(e.nombre, 'Todas las sedes de ' || "
                "(SELECT COALESCE(MAX(e2.marca), c.nombre) FROM establecimientos e2 WHERE e2.catalogo_id = c.id)) AS lugar "
                "FROM productos p LEFT JOIN establecimientos e ON e.id = p.establecimiento_id "
                "LEFT JOIN catalogos c ON c.id = p.catalogo_id "
                "WHERE e.organizacion_id = ? OR c.organizacion_id = ? ORDER BY lugar, p.categoria, p.nombre",
                (self.org, self.org)).fetchall()
        return [dict(f) for f in filas]

    def actualizar_producto(self, producto_id: str, precio_min, precio_max, disponible: bool) -> dict:
        def entero(v):
            return None if v in (None, "") else int(v)
        pmin, pmax = entero(precio_min), entero(precio_max)
        if pmin is not None and pmax is not None and pmin > pmax:
            raise ValueError("El precio mínimo no puede ser mayor que el máximo.")
        if any(v is not None and v < 0 for v in (pmin, pmax)):
            raise ValueError("Los precios no pueden ser negativos.")
        ahora = datetime.now().isoformat(timespec="seconds")
        with self._con() as con:
            cur = con.execute(
                "UPDATE productos SET precio_min = ?, precio_max = ?, disponible = ?, actualizado = ? WHERE id = ?",
                (pmin, pmax, 1 if disponible else 0, ahora, producto_id))
            if cur.rowcount == 0:
                raise ValueError(f"No existe el producto '{producto_id}'.")
        return {"ok": True, "id": producto_id, "actualizado": ahora}





# ------------------------------------------------------------------ control de accesos
ITERACIONES_PBKDF2 = 200_000
DURACION_SESION = timedelta(hours=8)
ROLES = ("admin", "vendedor")


def _hash_contrasena(contrasena: str, sal: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", contrasena.encode("utf-8"), bytes.fromhex(sal),
                               ITERACIONES_PBKDF2).hex()


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class Cuentas:
    """Usuarios, sesiones y permisos del panel de vendedores.

    - admin: ve y edita todas las organizaciones, y administra usuarios.
    - vendedor: solo edita los productos de los establecimientos que tiene asignados. Un producto
      de un catálogo compartido (p. ej. el menú común de las 5 sedes Latte) solo lo puede editar
      quien tenga asignadas TODAS las sedes que usan ese catálogo.
    """

    def __init__(self, ruta: Path = RUTA_BD):
        if not ruta.exists():
            crear_bd(ruta)
        _migrar(ruta)
        self.ruta = ruta

    def _con(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.ruta)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys = ON")
        return con

    # --- usuarios ----------------------------------------------------------------
    def hay_usuarios(self) -> bool:
        with self._con() as con:
            return con.execute("SELECT COUNT(*) FROM usuarios").fetchone()[0] > 0

    def crear_usuario(self, usuario: str, nombre: str, contrasena: str, rol: str,
                      establecimientos: list[str] | None = None) -> dict:
        usuario = (usuario or "").strip().lower()
        nombre = (nombre or "").strip()
        if not re.fullmatch(r"[a-z0-9._-]{3,32}", usuario):
            raise ValueError("El usuario debe tener de 3 a 32 caracteres: letras, números, punto, guion o guion bajo.")
        if not nombre:
            raise ValueError("Escribe el nombre de la persona.")
        if len(contrasena or "") < 8:
            raise ValueError("La contraseña debe tener al menos 8 caracteres.")
        if rol not in ROLES:
            raise ValueError("Rol inválido.")
        establecimientos = sorted(set(establecimientos or []))
        if rol == "vendedor" and not establecimientos:
            raise ValueError("Asigna al menos un establecimiento al vendedor.")
        sal = secrets.token_hex(16)
        with self._con() as con:
            if establecimientos:
                validos = {r[0] for r in con.execute(
                    f"SELECT id FROM establecimientos WHERE id IN ({','.join('?' * len(establecimientos))})",
                    establecimientos)}
                if validos != set(establecimientos):
                    raise ValueError("Algún establecimiento no existe.")
            try:
                cur = con.execute(
                    "INSERT INTO usuarios (usuario, nombre, hash, sal, rol, activo, creado) VALUES (?,?,?,?,?,1,?)",
                    (usuario, nombre, _hash_contrasena(contrasena, sal), sal, rol,
                     datetime.now().isoformat(timespec="seconds")))
            except sqlite3.IntegrityError:
                raise ValueError(f"Ya existe el usuario '{usuario}'.") from None
            if rol == "vendedor":
                con.executemany("INSERT INTO usuario_establecimientos VALUES (?,?)",
                                [(cur.lastrowid, e) for e in establecimientos])
        return {"ok": True, "id": cur.lastrowid, "usuario": usuario}

    def listar_usuarios(self) -> list[dict]:
        with self._con() as con:
            filas = con.execute("SELECT id, usuario, nombre, rol, activo, creado FROM usuarios ORDER BY id").fetchall()
            asig = con.execute("SELECT ue.usuario_id, e.nombre FROM usuario_establecimientos ue "
                               "JOIN establecimientos e ON e.id = ue.establecimiento_id ORDER BY e.rowid").fetchall()
        por_usuario: dict[int, list[str]] = {}
        for a in asig:
            por_usuario.setdefault(a["usuario_id"], []).append(a["nombre"])
        return [{**dict(f), "activo": bool(f["activo"]), "establecimientos": por_usuario.get(f["id"], [])}
                for f in filas]

    def cambiar_estado(self, usuario_id: int, activo: bool) -> dict:
        with self._con() as con:
            if not activo:
                fila = con.execute("SELECT rol FROM usuarios WHERE id = ?", (usuario_id,)).fetchone()
                admins = con.execute("SELECT COUNT(*) FROM usuarios WHERE rol = 'admin' AND activo = 1").fetchone()[0]
                if fila and fila["rol"] == "admin" and admins <= 1:
                    raise ValueError("No puedes desactivar al último administrador activo.")
                con.execute("DELETE FROM sesiones WHERE usuario_id = ?", (usuario_id,))
            cur = con.execute("UPDATE usuarios SET activo = ? WHERE id = ?", (1 if activo else 0, usuario_id))
            if cur.rowcount == 0:
                raise ValueError("No existe ese usuario.")
        return {"ok": True}

    def cambiar_contrasena(self, usuario_id: int, nueva: str) -> dict:
        if len(nueva or "") < 8:
            raise ValueError("La contraseña debe tener al menos 8 caracteres.")
        sal = secrets.token_hex(16)
        with self._con() as con:
            cur = con.execute("UPDATE usuarios SET hash = ?, sal = ? WHERE id = ?",
                              (_hash_contrasena(nueva, sal), sal, usuario_id))
            if cur.rowcount == 0:
                raise ValueError("No existe ese usuario.")
            con.execute("DELETE FROM sesiones WHERE usuario_id = ?", (usuario_id,))  # cierra sus sesiones
        return {"ok": True}

    # --- sesiones ----------------------------------------------------------------
    def iniciar_sesion(self, usuario: str, contrasena: str) -> str | None:
        """Devuelve un token de sesión si usuario y contraseña son correctos (y el usuario está activo)."""
        with self._con() as con:
            fila = con.execute("SELECT * FROM usuarios WHERE usuario = ?",
                               ((usuario or "").strip().lower(),)).fetchone()
            # Se calcula el hash aunque el usuario no exista, para no revelar cuáles existen por el tiempo.
            sal = fila["sal"] if fila else "00" * 16
            correcto = hmac.compare_digest(_hash_contrasena(contrasena or "", sal), fila["hash"] if fila else "x")
            if not (fila and correcto and fila["activo"]):
                return None
            token = secrets.token_urlsafe(32)
            con.execute("DELETE FROM sesiones WHERE expira < ?", (datetime.now().isoformat(),))
            con.execute("INSERT INTO sesiones VALUES (?,?,?)",
                        (_hash_token(token), fila["id"], (datetime.now() + DURACION_SESION).isoformat()))
        return token

    def cerrar_sesion(self, token: str) -> None:
        with self._con() as con:
            con.execute("DELETE FROM sesiones WHERE token_hash = ?", (_hash_token(token),))

    def usuario_de_sesion(self, token: str | None) -> dict | None:
        if not token:
            return None
        with self._con() as con:
            fila = con.execute(
                "SELECT u.id, u.usuario, u.nombre, u.rol FROM sesiones s JOIN usuarios u ON u.id = s.usuario_id "
                "WHERE s.token_hash = ? AND s.expira > ? AND u.activo = 1",
                (_hash_token(token), datetime.now().isoformat())).fetchone()
            if not fila:
                return None
            ests = [r[0] for r in con.execute(
                "SELECT establecimiento_id FROM usuario_establecimientos WHERE usuario_id = ?", (fila["id"],))]
            orgs = [r[0] for r in con.execute(
                "SELECT DISTINCT organizacion_id FROM establecimientos WHERE id IN "
                f"({','.join('?' * len(ests))})", ests)] if ests else []
        return {**dict(fila), "establecimientos": ests, "organizaciones": orgs}

    # --- permisos ----------------------------------------------------------------
    def catalogos_completos(self, usuario: dict) -> set[str]:
        """Catálogos compartidos cuyas sedes están TODAS asignadas al usuario."""
        with self._con() as con:
            filas = con.execute("SELECT catalogo_id, id FROM establecimientos WHERE catalogo_id IS NOT NULL").fetchall()
        sedes: dict[str, set[str]] = {}
        for f in filas:
            sedes.setdefault(f["catalogo_id"], set()).add(f["id"])
        propias = set(usuario["establecimientos"])
        return {c for c, s in sedes.items() if s <= propias}

    def puede_editar(self, usuario: dict, producto: dict) -> bool:
        if usuario["rol"] == "admin":
            return True
        if producto.get("establecimiento_id"):
            return producto["establecimiento_id"] in usuario["establecimientos"]
        return producto.get("catalogo_id") in self.catalogos_completos(usuario)

    def producto(self, producto_id: str) -> dict | None:
        with self._con() as con:
            fila = con.execute("SELECT id, establecimiento_id, catalogo_id FROM productos WHERE id = ?",
                               (producto_id,)).fetchone()
        return dict(fila) if fila else None

    def establecimientos(self, organizacion: str | None = None) -> list[dict]:
        with self._con() as con:
            filas = con.execute(
                "SELECT e.id, e.nombre, e.organizacion_id, o.nombre AS organizacion FROM establecimientos e "
                "JOIN organizaciones o ON o.id = e.organizacion_id "
                "WHERE ? IS NULL OR e.organizacion_id = ? ORDER BY e.rowid", (organizacion, organizacion)).fetchall()
        return [dict(f) for f in filas]


if __name__ == "__main__":
    import getpass
    import sys as _sys

    if len(_sys.argv) == 3 and _sys.argv[1] == "clave":
        c = Cuentas()
        fila = next((u for u in c.listar_usuarios() if u["usuario"] == _sys.argv[2].lower()), None)
        if not fila:
            _sys.exit(f"No existe el usuario '{_sys.argv[2]}'.")
        nueva = getpass.getpass("Nueva contraseña (no se ve mientras escribes): ")
        if nueva != getpass.getpass("Repítela: "):
            _sys.exit("Las contraseñas no coinciden.")
        c.cambiar_contrasena(fila["id"], nueva)
        c.cambiar_estado(fila["id"], True)
        print(f"Contraseña de '{fila['usuario']}' actualizada.")
    elif len(_sys.argv) == 1:
        if RUTA_BD.exists() and input("Esto BORRA la base actual (usuarios y cambios del panel). ¿Seguir? (s/n) ").lower() != "s":
            _sys.exit("Cancelado.")
        creadas = crear_bd()
        print(f"Base de datos creada en {RUTA_BD} con las organizaciones: {', '.join(creadas)}")
    else:
        print(__doc__)
