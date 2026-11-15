"""
Evaluación preliminar (sección VI del artículo): corre los escenarios y mide
  - selección correcta de herramienta
  - precisión de parámetros (fracción de parámetros esperados que coinciden)
  - latencia
Opcionalmente compara contra la línea base sin herramientas.

    python evaluacion/evaluar.py
    python evaluacion/evaluar.py --con-linea-base

Resultados: evaluacion/resultados_<fecha>.json  (+ resumen en pantalla)
"""

import argparse
import asyncio
import json
import sys
import unicodedata
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from agente import AgenteComida  # noqa: E402
from agente import config  # noqa: E402

AQUI = Path(__file__).resolve().parent


def _n(v):
    if isinstance(v, str):
        v = unicodedata.normalize("NFKD", v.lower().strip())
        return "".join(c for c in v if not unicodedata.combining(c))
    return v


def _coincide(enviado, esperado) -> bool:
    """Textos: basta con que uno contenga al otro ("Latte" ~ "latte_central"). Números/booleanos: iguales."""
    a, b = _n(enviado), _n(esperado)
    if isinstance(b, str):
        return isinstance(a, str) and bool(a) and (b in a or a in b)
    if isinstance(b, bool):
        return a is b
    try:
        return a is not None and float(a) == float(b)
    except (TypeError, ValueError):
        return False


def puntuar(esc: dict, trazas: list) -> dict:
    usadas = [t.herramienta for t in trazas]
    aceptadas = esc["herramientas_aceptadas"]
    elegida = next((h for h in usadas if h in aceptadas), None)
    precision = None
    params = {}
    if elegida:
        params = next(t.argumentos for t in trazas if t.herramienta == elegida)
        esperados = aceptadas[elegida]
        if esperados:
            aciertos = sum(1 for k, v in esperados.items() if _coincide(params.get(k), v))
            precision = round(aciertos / len(esperados), 2)
    return {
        "herramientas_usadas": usadas,
        "seleccion_correcta": elegida is not None,
        "precision_parametros": precision if elegida else 0.0,
        "parametros_enviados": params,
        "errores_de_herramienta": sum(1 for t in trazas if not t.ok),
        "descartados": [t.descartados for t in trazas if t.descartados],
    }


async def correr(usar_herramientas: bool, escenarios: list) -> list:
    filas = []
    async with AgenteComida(usar_herramientas=usar_herramientas, guardar_log=False) as agente:
        for esc in escenarios:
            agente.reiniciar()  # cada escenario es independiente
            r = await agente.responder(esc["pregunta"])
            fila = {"id": esc["id"], "tipo": esc["tipo"], "pregunta": esc["pregunta"], "respuesta": r.texto,
                    "error": r.error, "latencia_ms": r.latencia_total_ms, "pasos": r.pasos}
            if usar_herramientas:
                fila.update(puntuar(esc, r.trazas))
            filas.append(fila)
            marca = "" if not usar_herramientas else ("✅" if fila["seleccion_correcta"] else "❌")
            print(f"  {esc['id']} {marca} {r.latencia_total_ms} ms  {fila.get('herramientas_usadas', '')}")
    return filas


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--con-linea-base", action="store_true", help="también correr el LLM sin herramientas")
    args = ap.parse_args()
    escenarios = json.loads((AQUI / "escenarios.json").read_text(encoding="utf-8"))

    print(f"Modelo: {config.LLM_MODEL}\n== Agente LLM + MCP ==")
    con = await correr(True, escenarios)
    proveedor = "local (Ollama)" if "localhost" in config.LLM_BASE_URL or "127.0.0.1" in config.LLM_BASE_URL \
        else config.LLM_BASE_URL.split("/")[2]
    salida = {"fecha": datetime.now().isoformat(timespec="seconds"), "modelo": config.LLM_MODEL,
              "proveedor": proveedor, "con_mcp": con}

    n = len(con)
    sel = sum(f["seleccion_correcta"] for f in con) / n
    precs = [f["precision_parametros"] for f in con if f["precision_parametros"] is not None]
    lat = sum(f["latencia_ms"] for f in con) / n
    salida["resumen"] = {"seleccion_correcta": round(sel, 2),
                         "parametros_inventados": sum(len(t) for f in con for t in f.get("descartados", [])),
                         "fallos_llm": sum(1 for f in con if f["error"]),
                         "precision_parametros_promedio": round(sum(precs) / len(precs), 2) if precs else None,
                         "latencia_promedio_ms": round(lat)}

    if args.con_linea_base:
        print("== Línea base: LLM sin herramientas ==")
        salida["sin_mcp"] = await correr(False, escenarios)

    archivo = AQUI / f"resultados_{datetime.now():%Y%m%d_%H%M%S}.json"
    archivo.write_text(json.dumps(salida, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nResumen: {salida['resumen']}\nGuardado en {archivo}")


if __name__ == "__main__":
    asyncio.run(main())
