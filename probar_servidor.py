"""
Prueba el servidor MCP SIN LLM: se conecta como cliente MCP, lista las herramientas
y ejecuta algunas llamadas. Útil para verificar que la capa MCP funciona.

    python probar_servidor.py
"""

import asyncio
import json
import os
import sys
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

SERVIDOR = Path(__file__).resolve().parent / "servidor_mcp" / "servidor.py"


async def main() -> None:
    params = StdioServerParameters(command=sys.executable, args=[str(SERVIDOR)], env=dict(os.environ))
    async with stdio_client(params) as (leer, escribir):
        async with ClientSession(leer, escribir) as sesion:
            await sesion.initialize()

            herramientas = await sesion.list_tools()
            print("Herramientas expuestas por el servidor MCP:")
            for h in herramientas.tools:
                print(f"  - {h.name}: {h.description.splitlines()[0]}")

            pruebas = [
                ("buscar_opciones", {"presupuesto_max": 10000, "vegetariano": True, "zona_usuario": "Ingeniería"}),
                ("buscar_opciones", {"texto": "fresas con chocolate"}),
                ("get_opening_hours", {"cafeteria_id": "latte_central"}),
                ("get_menu", {"cafeteria_id": "no_existe"}),  # caso de error controlado
            ]
            for nombre, args in pruebas:
                r = await sesion.call_tool(nombre, args)
                print(f"\n>>> {nombre}({args})")
                texto = r.content[0].text if r.content else ""
                print(json.dumps(json.loads(texto), ensure_ascii=False, indent=2)[:1500])


if __name__ == "__main__":
    asyncio.run(main())
