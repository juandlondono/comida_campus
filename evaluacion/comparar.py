"""
Compara todas las evaluaciones guardadas (evaluacion/resultados_*.json) en una tabla.
Corre evaluar.py una vez por modelo (cambiando el .env) y luego:

    python evaluacion/comparar.py
"""

import json
from pathlib import Path

AQUI = Path(__file__).resolve().parent


def main() -> None:
    archivos = sorted(AQUI.glob("resultados_*.json"))
    if not archivos:
        print("No hay resultados todavía. Corre primero: python evaluacion/evaluar.py")
        return
    filas = []
    for a in archivos:
        d = json.loads(a.read_text(encoding="utf-8"))
        r = d.get("resumen", {})
        filas.append([d.get("modelo", "?"), d.get("proveedor", "?"),
                      f"{r.get('seleccion_correcta', 0):.0%}",
                      f"{(r.get('precision_parametros_promedio') or 0):.0%}",
                      str(r.get("parametros_inventados", "-")),
                      f"{r.get('latencia_promedio_ms', 0) / 1000:.1f} s",
                      str(r.get("fallos_llm", "-")), a.name])
    titulos = ["Modelo", "Proveedor", "Herramienta correcta", "Precisión parámetros",
               "Parám. inventados", "Latencia media", "Fallos", "Archivo"]
    anchos = [max(len(str(x)) for x in col) for col in zip(titulos, *filas)]
    linea = lambda c: " | ".join(str(x).ljust(w) for x, w in zip(c, anchos))
    print(linea(titulos))
    print("-+-".join("-" * w for w in anchos))
    for f in filas:
        print(linea(f))


if __name__ == "__main__":
    main()
