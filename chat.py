"""
Chat por terminal.

    python chat.py                      # agente con herramientas MCP
    python chat.py --sin-herramientas   # línea base: LLM solo (para comparar en el artículo)
    python chat.py --traza              # muestra qué herramientas usó y con qué parámetros

Comandos dentro del chat: /reiniciar, /salir
"""

import argparse
import asyncio
import json

from agente import AgenteComida
from agente import config


async def main() -> None:
    ap = argparse.ArgumentParser(description="Chatbot Comida Campus (LLM + MCP)")
    ap.add_argument("--sin-herramientas", action="store_true", help="LLM sin MCP (línea base)")
    ap.add_argument("--traza", action="store_true", help="mostrar llamadas a herramientas")
    args = ap.parse_args()

    async with AgenteComida(usar_herramientas=not args.sin_herramientas) as agente:
        modo = "SIN herramientas (línea base)" if args.sin_herramientas else \
               f"con MCP ({len(agente.nombres_herramientas)} herramientas)"
        print(f"🍽️  Comida Campus — modelo: {config.LLM_MODEL} — {modo}")
        print("Escribe tu pregunta. Ej: 'Tengo 10.000 pesos y 15 minutos, estoy en Ingeniería y quiero algo vegetariano'")
        print("Comandos: /reiniciar  /salir\n")

        while True:
            try:
                pregunta = (await asyncio.to_thread(input, "Tú: ")).strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not pregunta:
                continue
            if pregunta in ("/salir", "salir", "exit"):
                break
            if pregunta == "/reiniciar":
                agente.reiniciar()
                print("(conversación reiniciada)\n")
                continue

            r = await agente.responder(pregunta)
            if args.traza:
                for t in r.trazas:
                    estado = "ok" if t.ok else "ERROR"
                    extra = f"  (ignorados: {', '.join(t.descartados)})" if t.descartados else ""
                    print(f"  🔧 {t.herramienta}({json.dumps(t.argumentos, ensure_ascii=False)}) -> {estado} [{t.latencia_ms} ms]{extra}")
                print(f"  ⏱️  {r.latencia_total_ms} ms, {r.pasos} paso(s)")
            print(f"\nBot: {r.texto}\n")


if __name__ == "__main__":
    asyncio.run(main())
