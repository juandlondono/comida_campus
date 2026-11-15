"""
Capa de datos de Comida Campus sobre SQLite (nivel 5 de la arquitectura).

Es la ÚNICA parte del sistema que sabe que hay una base de datos. El servidor MCP y el
panel de vendedores la usan; el agente y el LLM nunca la tocan. Cambiar a PostgreSQL
o MySQL significa reescribir solo este archivo.

    python servidor_mcp/repositorio.py          # (re)crea data/campus.db desde los JSON semilla
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import unicodedata
from datetime import datetime
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


# ---------------------------------------------------------------------- repositorio
class Repositorio:
    def __init__(self, ruta: Path = RUTA_BD, organizacion: str = ORG_POR_DEFECTO):
        if not ruta.exists():
            crear_bd(ruta)
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
                "p.actualizado, COALESCE(e.nombre, 'Todas las sedes de ' || "
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


if __name__ == "__main__":
    creadas = crear_bd()
    print(f"Base de datos creada en {RUTA_BD} con las organizaciones: {', '.join(creadas)}")
