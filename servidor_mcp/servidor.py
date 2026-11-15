"""
Servidor MCP "comida-campus".

Expone herramientas de SOLO LECTURA sobre los establecimientos de comida del campus
(Fig. 1 del artículo: nivel 4 "Servidor MCP" + nivel 5 "Datos").

Los datos viven en una base de datos SQLite (data/campus.db) a la que se accede solo a través
de repositorio.py; se crea sola desde data/semillas/ la primera vez.
Se ejecuta por stdio: el cliente MCP (el agente) lanza este archivo como subproceso.
Para probarlo solo:  python probar_servidor.py
"""

from __future__ import annotations

import json
import os
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

import sys

from mcp.server.mcpserver import MCPServer

sys.path.insert(0, str(Path(__file__).resolve().parent))
from repositorio import Repositorio  # noqa: E402  (capa de datos: SQLite)

# Organización que atiende este servidor (multi-organización): CAMPUS_ORG=unibague | demo_torre | ...
REPO = Repositorio()
# Colombia es UTC-5 todo el año (sin horario de verano). No se usa zoneinfo porque
# Windows no trae la base de datos de zonas horarias.
ZONA_HORARIA = timezone(timedelta(hours=-5), "COT")
DIAS = ["lunes", "martes", "miercoles", "jueves", "viernes", "sabado", "domingo"]
MAX_RESULTADOS = 6
# Orden al recomendar "algo de comer": primero comidas, luego antojos, luego bebidas.
RANGO_CATEGORIA = {"almuerzo": 0, "plato": 0, "desayuno": 0, "sándwich": 1, "comida rápida": 1,
                   "panadería": 1, "postre": 2, "fruta": 2, "snack": 3, "bebida fría": 4,
                   "bebida caliente": 4, "adicional": 5}
PALABRAS_VACIAS = {"de", "del", "la", "el", "los", "las", "con", "y", "en", "un", "una", "para", "al",
                   # genéricas: "algo de comida" no debe filtrar por nombre de producto
                   "comida", "comer", "algo", "bebida", "beber", "tomar", "producto", "opcion", "opciones",
                   "cosa", "rico", "barato", "rapido"}



def _instrucciones() -> str:
    """Contexto que el servidor le entrega al cliente MCP al conectarse: así el agente sabe
    qué organización atiende sin que nada esté escrito a mano en el prompt."""
    d = REPO.cargar()
    marcas, sueltos = {}, []
    for e in d["establecimientos"]:
        if e.get("marca"):
            marcas.setdefault(e["marca"], []).append(e)
        else:
            sueltos.append(f"{e['nombre']} ({e['ubicacion']})")
    lugares = [f"{m} ({len(es)} sedes: {', '.join(e['zona'] for e in es)})" for m, es in marcas.items()] + sueltos
    return (f"Organización: {d['metadatos']['organizacion']}. Establecimientos: {'; '.join(lugares)}. "
            f"Datos actualizados al {d['metadatos']['fecha_consulta']}. Herramientas de solo lectura.")


mcp = MCPServer(name="comida-campus", instructions=_instrucciones())


# ----------------------------------------------------------------- utilidades
def _cargar() -> dict:
    return REPO.cargar()  # se lee en cada consulta: los cambios del panel se ven de inmediato


def _norm(texto) -> str:
    """Minúsculas y sin tildes, para comparar 'Ingeniería' con 'ingenieria'."""
    if not texto:
        return ""
    t = unicodedata.normalize("NFKD", str(texto).strip().lower())
    return "".join(c for c in t if not unicodedata.combining(c))


def _raiz(palabra: str) -> str:
    """Singular aproximado: 'sandwiches' -> 'sandwich', 'fresas' -> 'fresa'."""
    for suf in ("es", "s"):
        if len(palabra) > 4 and palabra.endswith(suf):
            return palabra[: -len(suf)]
    return palabra


def _palabras(texto: str) -> list[str]:
    limpio = "".join(c if c.isalnum() else " " for c in _norm(texto))
    return [_raiz(p) for p in limpio.split() if p not in PALABRAS_VACIAS]


def _ahora() -> datetime:
    """Hora actual en Ibagué. CAMPUS_HORA_SIMULADA='2026-11-04 12:30' fija la hora para demos."""
    simulada = os.getenv("CAMPUS_HORA_SIMULADA")
    if simulada:
        for fmt in ("%Y-%m-%d %H:%M", "%H:%M"):
            try:
                dt = datetime.strptime(simulada, fmt)
                if fmt == "%H:%M":
                    hoy = datetime.now(ZONA_HORARIA)
                    dt = dt.replace(year=hoy.year, month=hoy.month, day=hoy.day)
                return dt.replace(tzinfo=ZONA_HORARIA)
            except ValueError:
                continue
    return datetime.now(ZONA_HORARIA)


def _fmt(momento: datetime) -> str:
    return f"{DIAS[momento.weekday()]} {momento:%H:%M}"


def _pesos(valor: int) -> str:
    return f"${valor:,}".replace(",", ".")


def _precio_txt(p: dict) -> str:
    mn, mx = p.get("precio_min"), p.get("precio_max")
    if mn is None and mx is None:
        return "precio no registrado"
    if mx is None:
        return f"desde {_pesos(mn)}"
    if mn is None:
        return f"hasta {_pesos(mx)}"
    return _pesos(mn) if mn == mx else f"{_pesos(mn)} - {_pesos(mx)}"


def _precio_ref(p: dict) -> int | None:
    """Precio para filtrar por presupuesto: el mínimo conocido (o el máximo si solo se conoce ese)."""
    return p["precio_min"] if p.get("precio_min") is not None else p.get("precio_max")


def _es_bebida(p: dict) -> bool:
    return p["categoria"].startswith("bebida")


def _productos(datos: dict, est: dict) -> list[dict]:
    """Productos del catálogo compartido (p. ej. todas las sedes Latte) + los propios de la sede."""
    comunes = datos.get("catalogos", {}).get(est.get("catalogo"), []) if est.get("catalogo") else []
    return comunes + est.get("productos", [])


def _disponible_ahora(p: dict, ahora: datetime) -> tuple[bool, str | None]:
    if p.get("disponible") is False:
        return False, "agotado / no disponible hoy"
    hasta = p.get("disponible_hasta")
    if hasta and ahora.strftime("%H:%M") >= hasta:
        return False, f"solo se vende hasta las {hasta}"
    return True, None


def _en_zona(est: dict, zona: str | None) -> bool:
    if not zona:
        return False
    z = _norm(zona)
    claves = [_norm(est["zona"])] + [_norm(r) for r in est.get("referencias", [])]
    return any(c and (c in z or z in c) for c in claves)


def _resolver(datos: dict, ref: str | None) -> list[dict]:
    """Encuentra establecimientos por id, nombre o marca ('latte' -> las 5 sedes)."""
    if not ref:
        return list(datos["establecimientos"])
    r = _norm(ref)
    exactos = [e for e in datos["establecimientos"] if r in (_norm(e["id"]), _norm(e["nombre"]))]
    if exactos:
        return exactos
    return [e for e in datos["establecimientos"]
            if r in _norm(e["nombre"]) or r in _norm(e.get("marca")) or r in _norm(e["id"])
            or (_norm(e.get("marca")) and _norm(e.get("marca")) in r)]


def _error_no_encontrado(datos: dict, ref: str) -> dict:
    return {
        "error": f"No hay ningún establecimiento que coincida con '{ref}'.",
        "ids_validos": [e["id"] for e in datos["establecimientos"]],
    }


def _afluencia_txt(est: dict) -> str:
    a = est.get("afluencia")
    return a["nota"] if a else "afluencia no registrada"


def _en_hora_pico(ahora: datetime, datos: dict) -> bool:
    inicio, fin = datos["metadatos"]["hora_pico"]
    return bool(inicio and fin) and inicio <= ahora.strftime("%H:%M") < fin


def _producto_compacto(p: dict) -> dict:
    d = {"producto": p["nombre"], "precio": _precio_txt(p), "vegetariano": p["vegetariano"]}
    for k in ("nota", "opciones", "disponible_hasta"):
        if p.get(k):
            d[k] = p[k]
    return d


def _variar(lista: list[dict], n: int, por_lugar: int = 2) -> list[dict]:
    """Toma n elementos respetando el orden, pero máximo `por_lugar` por establecimiento en la primera pasada."""
    elegidos, cuenta = [], {}
    for r in lista:
        if len(elegidos) < n and cuenta.get(r["_lugar"], 0) < por_lugar:
            elegidos.append(r)
            cuenta[r["_lugar"]] = cuenta.get(r["_lugar"], 0) + 1
    for r in lista:
        if len(elegidos) < n and r not in elegidos:
            elegidos.append(r)
    return elegidos


# ---------------------------------------------------------------- herramientas
@mcp.tool()
def get_zonas() -> dict:
    """Zonas del campus con establecimientos y hora actual."""
    datos = _cargar()
    return {
        "zonas": [{"zona": e["zona"], "establecimiento": e["nombre"], "ubicacion": e["ubicacion"]}
                  for e in datos["establecimientos"]],
        "hora_actual": _fmt(_ahora()),
        "fecha_datos": datos["metadatos"]["fecha_consulta"],
        "nota": "No hay distancias ni tiempos de caminata registrados entre zonas.",
    }


@mcp.tool()
def get_cafeterias(zona: str | None = None) -> dict:
    """Lista los establecimientos (id, ubicación, afluencia).

    Args:
        zona: opcional, dónde está el usuario; los de esa zona salen primero.
    """
    datos = _cargar()
    lista = []
    for e in datos["establecimientos"]:
        lista.append({
            "id": e["id"], "nombre": e["nombre"], "ubicacion": e["ubicacion"],
            "afluencia": _afluencia_txt(e), "en_tu_zona": _en_zona(e, zona),
            "ofrece": e.get("descripcion") or ("Productos comunes de Latte" +
                      (" + desayunos y almuerzos" if e["id"] == "latte_central" else "")),
        })
    lista.sort(key=lambda x: not x["en_tu_zona"])
    salida = {"establecimientos": lista, "horarios": "Horarios de atención aún no registrados."}
    if zona and not any(x["en_tu_zona"] for x in lista):
        salida["nota"] = f"No hay establecimientos registrados en '{zona}'. No hay distancias registradas."
    return salida


@mcp.tool()
def get_menu(cafeteria_id: str, categoria: str | None = None) -> dict:
    """Menú de un establecimiento con precios y vegetariano ("si"/"no"/"por_confirmar").
    "latte" devuelve el menú común de las 5 sedes Latte.

    Args:
        cafeteria_id: p. ej. "latte", "latte_central", "maria_cafe", "melo_caramelo".
        categoria: opcional, p. ej. "bebida fría", "desayuno", "postre".
    """
    datos = _cargar()
    ests = _resolver(datos, cafeteria_id)
    if not ests:
        return _error_no_encontrado(datos, cafeteria_id)
    ahora = _ahora()

    def lista(prods):
        out = []
        for p in prods:
            if categoria and _norm(categoria) not in _norm(p["categoria"]):
                continue
            item = _producto_compacto(p)
            item["categoria"] = p["categoria"]
            ok, motivo = _disponible_ahora(p, ahora)
            if not ok:
                item["no_disponible_ahora"] = motivo
            out.append(item)
        return out

    if len(ests) > 1:  # varias sedes de la misma marca (Latte)
        catalogo = ests[0].get("catalogo")
        return {
            "marca": ests[0].get("marca"),
            "sedes": [{"id": e["id"], "nombre": e["nombre"], "ubicacion": e["ubicacion"]} for e in ests],
            "productos_en_todas_las_sedes": lista(datos.get("catalogos", {}).get(catalogo, [])),
            "exclusivos_por_sede": {e["nombre"]: l for e in ests if (l := lista(e.get("productos", [])))},
            "hora_consulta": _fmt(ahora),
        }
    e = ests[0]
    salida = {"id": e["id"], "nombre": e["nombre"], "ubicacion": e["ubicacion"],
              "productos": lista(_productos(datos, e)), "hora_consulta": _fmt(ahora)}
    if e.get("advertencia_vegetariana"):
        salida["advertencia_vegetariana"] = e["advertencia_vegetariana"]
    return salida


@mcp.tool()
def get_opening_hours(cafeteria_id: str) -> dict:
    """Horario de un establecimiento y productos con hora límite.

    Args:
        cafeteria_id: id o nombre, p. ej. "latte_central".
    """
    datos = _cargar()
    ests = _resolver(datos, cafeteria_id)
    if not ests:
        return _error_no_encontrado(datos, cafeteria_id)
    ahora = _ahora()
    salida = []
    for e in ests:
        franjas = [{"producto": p["nombre"], "disponible_hasta": p["disponible_hasta"],
                    "disponible_ahora": _disponible_ahora(p, ahora)[0]}
                   for p in _productos(datos, e) if p.get("disponible_hasta")]
        salida.append({"nombre": e["nombre"],
                       "horario_atencion": e["horario"] or "no registrado todavía",
                       "franjas_de_productos": franjas})
    return {"hora_consulta": _fmt(ahora), "establecimientos": salida}


@mcp.tool()
def get_availability(cafeteria_id: str, producto: str) -> dict:
    """Dice si un producto se vende en un establecimiento.

    Args:
        cafeteria_id: id o nombre ("latte" = las 5 sedes).
        producto: nombre o parte del nombre, p. ej. "empanada".
    """
    datos = _cargar()
    ests = _resolver(datos, cafeteria_id)
    if not ests:
        return _error_no_encontrado(datos, cafeteria_id)
    ahora, encontrados = _ahora(), []
    coinciden = REPO.buscar_texto(_palabras(producto))  # índice invertido (FTS5)
    for e in ests:
        for p in _productos(datos, e):
            if p["id"] in coinciden:
                ok, motivo = _disponible_ahora(p, ahora)
                encontrados.append({"establecimiento": e["nombre"], **_producto_compacto(p),
                                    "disponible_ahora": ok, **({"motivo": motivo} if motivo else {})})
    if not encontrados:
        return {"encontrado": False, "mensaje": f"'{producto}' no aparece en el inventario de {cafeteria_id}."}
    return {"encontrado": True, "resultados": encontrados[:MAX_RESULTADOS]}


@mcp.tool()
def buscar_opciones(
    presupuesto_max: int | None = None,
    vegetariano: bool = False,
    texto: str | None = None,
    categoria: str | None = None,
    tipo: str | None = None,
    establecimiento: str | None = None,
    zona_usuario: str | None = None,
    tiempo_max_min: int | None = None,
) -> dict:
    """Busca productos en todo el campus que cumplan las condiciones dadas. Herramienta principal.
    Usa solo los parámetros que el usuario pidió.

    Args:
        presupuesto_max: dinero en pesos, p. ej. 10000.
        vegetariano: true SOLO si el usuario pide vegetariano.
        texto: nombre de un producto concreto, p. ej. "fresas con chocolate", "sándwich". No uses "comida".
        categoria: "desayuno", "almuerzo", "sándwich", "panadería", "comida rápida", "plato", "snack",
                   "fruta", "postre", "bebida fría" o "bebida caliente".
        tipo: "comida" o "bebida". Omítelo si quiere comer y tomar algo.
        establecimiento: p. ej. "Latte", "María Café", "Melo Caramelo".
        zona_usuario: dónde está el usuario, p. ej. "Biblioteca", "Ingeniería", "Bloque 4".
        tiempo_max_min: minutos disponibles (solo para avisos de afluencia).
    """
    datos = _cargar()
    ests = _resolver(datos, establecimiento)
    if not ests:
        return _error_no_encontrado(datos, establecimiento)
    ahora = _ahora()
    poco_tiempo_en_pico = bool(tiempo_max_min and tiempo_max_min <= 20 and _en_hora_pico(ahora, datos))
    buscadas = _palabras(texto) if texto else []
    coinciden = REPO.buscar_texto(buscadas) if buscadas else set()  # índice invertido (FTS5)
    pidio_bebida = (_norm(tipo) == "bebida") or ("bebida" in _norm(categoria)) or bool(buscadas)

    grupos: dict[str, dict] = {}   # el mismo producto en varias sedes Latte se agrupa
    no_disponibles, sin_precio = set(), set()

    for e in ests:
        for p in _productos(datos, e):
            if p["categoria"] == "adicional" and _norm(categoria) != "adicional" and not buscadas:
                continue
            if categoria and _norm(categoria) not in _norm(p["categoria"]):
                continue
            if _norm(tipo) == "comida" and _es_bebida(p):
                continue
            if _norm(tipo) == "bebida" and not _es_bebida(p):
                continue
            if buscadas and p["id"] not in coinciden:
                continue
            if vegetariano and p["vegetariano"] == "no":
                continue
            ok, motivo = _disponible_ahora(p, ahora)
            if not ok:
                no_disponibles.add(f"{p['nombre']} ({motivo})")
                continue
            ref = _precio_ref(p)
            if presupuesto_max is not None:
                if ref is None:
                    sin_precio.add(p["nombre"])
                    continue
                if ref > presupuesto_max:
                    continue

            g = grupos.setdefault(p["id"], {"p": p, "ests": []})
            g["ests"].append(e)

    resultados = []
    for g in grupos.values():
        p, es = g["p"], g["ests"]
        es.sort(key=lambda e: not _en_zona(e, zona_usuario))
        item = _producto_compacto(p)
        item["donde"] = [f"{e['nombre']} ({e['ubicacion']})" for e in es] if len(es) <= 2 else \
            [f"{len(es)} sedes de {es[0].get('marca') or es[0]['nombre']}"] + \
            ([f"la de tu zona: {es[0]['nombre']} ({es[0]['ubicacion']})"] if _en_zona(es[0], zona_usuario) else [])
        avisos = []
        if presupuesto_max is not None and p.get("precio_max") and p["precio_max"] > presupuesto_max:
            avisos.append("según la presentación puede superar el presupuesto")
        if poco_tiempo_en_pico:
            llenos = [e for e in es if (e.get("afluencia") or {}).get("nivel") == "alta"]
            principal = es[0] if (_en_zona(es[0], zona_usuario) or len(es) == 1) else None
            if principal in llenos:
                otras = [e["nombre"] for e in es if e not in llenos]
                avisos.append(f"{principal['nombre']} suele estar muy concurrida al almuerzo; con poco tiempo "
                              "puede no alcanzar" + (f" (también se vende en {', '.join(otras)})" if otras else ""))
            elif llenos and len(llenos) < len(es):
                avisos.append(f"evita {llenos[0]['nombre']} si vas con poco tiempo: suele estar llena al almuerzo")
        if vegetariano and any(e.get("advertencia_vegetariana") for e in es):
            avisos.append(es[0]["advertencia_vegetariana"])
        if avisos:
            item["avisos"] = avisos
        item["_bebida"] = _es_bebida(p)
        item["_lugar"] = es[0]["nombre"]
        item["_orden"] = (
            p["vegetariano"] != "si" if vegetariano else False,       # confirmados primero
            not any(_en_zona(e, zona_usuario) for e in es),           # cerca del usuario primero
            any("concurrida" in a for a in avisos),                   # lugares llenos después
            0 if (pidio_bebida or categoria) else RANGO_CATEGORIA.get(p["categoria"], 3),  # comida primero
            _precio_ref(p) if _precio_ref(p) is not None else 10**9,  # sin precio, al final
        )
        resultados.append(item)

    resultados.sort(key=lambda r: r.pop("_orden"))
    confirmados = [r for r in resultados if not vegetariano or r["vegetariano"] == "si"]
    por_confirmar = [r for r in resultados if vegetariano and r["vegetariano"] == "por_confirmar"]

    if not (tipo or categoria or buscadas):
        # Pregunta abierta ("comer y tomar algo"): mezcla comida y bebidas, y varios establecimientos.
        comidas = _variar([r for r in confirmados if not r["_bebida"]], MAX_RESULTADOS - 2)
        bebidas = _variar([r for r in confirmados if r["_bebida"]], 2)
        mostrados = comidas + bebidas
    else:
        mostrados = confirmados[:MAX_RESULTADOS]
    for r in resultados:
        r.pop("_bebida", None)
        r.pop("_lugar", None)
    # Avisos repetidos en varios productos se muestran una sola vez (menos texto para el LLM).
    conteo: dict[str, int] = {}
    for r in mostrados:
        for a in r.get("avisos", []):
            conteo[a] = conteo.get(a, 0) + 1
    generales = [a for a, n in conteo.items() if n > 1]
    for r in mostrados:
        if "avisos" in r:
            r["avisos"] = [a for a in r["avisos"] if a not in generales]
            if not r["avisos"]:
                del r["avisos"]

    salida = {
        "hora_consulta": _fmt(ahora),
        "total_resultados": len(confirmados),
        "resultados": mostrados,
    }
    if generales:
        salida["avisos_generales"] = generales
    if por_confirmar:
        salida["posiblemente_vegetarianos_por_confirmar"] = [r["producto"] for r in por_confirmar[:5]]
    if no_disponibles:
        salida["no_disponibles_a_esta_hora"] = sorted(no_disponibles)[:5]
    if sin_precio:
        salida["sin_precio_registrado"] = sorted(sin_precio)
    if tiempo_max_min or zona_usuario:
        salida["aviso_tiempos"] = "Tiempos de fila y de caminata no medidos; la cercanía solo se basa en la zona."
    if not confirmados:
        salida["nota"] = ("Ninguna opción cumple todas las condiciones. Dilo con honestidad y sugiere "
                          "relajar alguna (presupuesto, categoría, establecimiento).")
    return salida


if __name__ == "__main__":
    mcp.run()  # transporte stdio
