# 🍽️ Comida Campus: agente LLM + MCP

Versión 1.0 del agente descrito en el artículo *"Agente inteligente basado en LLM y Model Context Protocol (MCP) para la integración y consulta de servicios gastronómicos en entornos universitarios"*: Universidad de Ibagué.

## Arquitectura

| Nivel | Qué es | Archivo |
|---|---|---|
| 1. Interfaz | Chat web, panel de vendedores y chat de terminal | `web.py`, `static/index.html`, `static/admin.html`, `chat.py` |
| 2. Agente + LLM | Bucle de *tool calling* (estilo ReAct) con validación de parámetros | `agente/agente.py`, `agente/llm.py` |
| 3. Cliente MCP | `ClientSession` por stdio; recibe del servidor el contexto de la organización | `agente/agente.py` |
| 4. Servidor MCP | 6 herramientas de solo lectura | `servidor_mcp/servidor.py` |
| 5. Datos | SQLite multi-organización con índice invertido FTS5 | `servidor_mcp/repositorio.py`, `servidor_mcp/esquema.sql` |

Herramientas MCP: `get_zonas`, `get_cafeterias(zona)`, `get_menu(cafeteria_id, categoria)`, `get_opening_hours(cafeteria_id)`, `get_availability(cafeteria_id, producto)` y `buscar_opciones(presupuesto_max, vegetariano, texto, categoria, tipo, establecimiento, zona_usuario, tiempo_max_min)`.

## Datos

La base de datos `data/campus.db` (SQLite) **se crea sola** la primera vez a partir de los JSON de `data/semillas/`:
- `unibague.json`: inventario **real** de la Universidad de Ibagué (recolección del 3 de noviembre de 2026):
  Cafeterías Latte (5 sedes), María Café y Melo Caramelo; 75 productos.
- `demo_torre.json`: organización **ficticia** para demostrar que el producto sirve en cualquier lugar.

Tablas: `organizaciones`, `zonas`, `catalogos`, `establecimientos`, `productos` y `productos_fts`
(índice invertido de texto completo que ignora tildes). Solo `repositorio.py` toca la base de datos:
pasar a PostgreSQL o MySQL implica reescribir ese archivo, no el agente ni el servidor.

- Para elegir la organización: el selector 📍 del chat y del panel (sin reiniciar), o `CAMPUS_ORG` en `.env` para la de inicio.
- Para volver a crear la base desde las semillas (borra los cambios del panel): `python servidor_mcp/repositorio.py`.
- Los vendedores editan precios y disponibilidad en `http://localhost:8000/admin`; el chatbot usa los cambios en la siguiente pregunta.

## Instalación (Windows, macOS o Linux, Python 3.10+)

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate     macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # en Windows: copy .env.example .env
```

### LLM con Ollama (por defecto)
1. Instala Ollama desde https://ollama.com y ábrelo.
2. Descarga un modelo que soporte herramientas (*tool calling*):
   ```bash
   ollama pull qwen2.5:7b      # ~4.7 GB, buen equilibrio (necesita ~8 GB de RAM)
   # PC con poca RAM:  ollama pull qwen2.5:3b   y pon LLM_MODEL=qwen2.5:3b en .env
   ```

Si el PC es muy lento, usa la **Opción B** del `.env` (un proveedor en la nube compatible con OpenAI). El código no cambia.

## Uso

```bash
python probar_servidor.py          # 1) prueba SOLO el servidor MCP (no necesita LLM)
python web.py                      # 2) chat web en http://localhost:8000
python chat.py --traza             #    o chat en terminal mostrando las herramientas usadas
python chat.py --sin-herramientas  # línea base: el LLM solo, sin MCP (para comparar)
python evaluacion/evaluar.py --con-linea-base   # 3) métricas (una vez por modelo)
python evaluacion/comparar.py                    #    tabla comparativa de todos los modelos evaluados
```

💡 La hora afecta qué se ofrece (desayunos hasta las 10:00, almuerzo hasta las 14:30, avisos de afluencia al mediodía).
Para controlar la demo pon por ejemplo `CAMPUS_HORA_SIMULADA=2026-11-04 12:30` en `.env`.

Cada conversación queda registrada en `logs/conversaciones.jsonl` (herramienta, parámetros, éxito y latencia), como insumo para la evaluación.

### Probar sin Ollama (solo para verificar la plomería)
```bash
python tests/llm_falso.py                                   # terminal 1
LLM_BASE_URL=http://127.0.0.1:8765/v1 python chat.py --traza  # terminal 2
```

## Usar el servidor MCP desde otro cliente (opcional, buena demo)
Como es MCP estándar, el mismo servidor se puede conectar a Claude Desktop u otro cliente MCP. Agrega esto a su configuración de servidores MCP:
```json
{ "mcpServers": { "comida-campus": { "command": "python", "args": ["RUTA/ABSOLUTA/servidor_mcp/servidor.py"] } } }
```

## Hoja de ruta (producto)
- Servidor MCP por HTTP con autenticación, para atender varias organizaciones y clientes a la vez.
- PostgreSQL con búsqueda semántica (pgvector) y distancias reales (PostGIS).
- Canal de WhatsApp y widget web para incrustar.
- Carga de menús desde una foto (extracción con LLM + revisión humana) y analítica de demanda para vendedores.
