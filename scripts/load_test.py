"""Prueba de carga: 5 peticiones concurrentes contra POST /tasks.

    python scripts/load_test.py

Dispara las 5 solicitudes con `asyncio.gather` e imprime los `job_id`
devueltos. El wall-clock total del batch es la evidencia de que el encolado no
bloquea: cinco investigaciones que tardan minutos cada una tienen que aceptarse
en milisegundos.

El seguimiento de los resultados es manual y está documentado en el README:
`GET /tasks/{job_id}` para el estado, y el dashboard de LangSmith para costo y
latencia de la corrida.
"""
from __future__ import annotations

import asyncio
import os
import time

import httpx

API_URL = os.environ.get("API_URL", "http://localhost:8000")

SOLICITUDES: list[str] = [
    "Adopción empresarial de agentes de IA en 2026",
    "Estado del mercado de chips para inferencia en 2026",
    "Impacto de los modelos open-weight en el mercado cloud",
    "Regulación de IA en la Unión Europea y su efecto en startups",
    "Tendencias de inversión en infraestructura de IA en Latinoamérica",
]


async def _enviar(cliente: httpx.AsyncClient, solicitud: str) -> tuple[str, float]:
    """Manda un POST /tasks y devuelve la línea a imprimir y su latencia."""
    inicio = time.perf_counter()
    try:
        respuesta = await cliente.post("/tasks", json={"solicitud": solicitud})
        transcurrido = time.perf_counter() - inicio
        respuesta.raise_for_status()
        job_id = respuesta.json()["job_id"]
        return f"  [{respuesta.status_code}] {job_id}  {solicitud}", transcurrido
    except httpx.HTTPError as exc:
        transcurrido = time.perf_counter() - inicio
        return f"  [ERROR] {type(exc).__name__}: {exc}  ({solicitud})", transcurrido


async def main() -> None:
    print(f"Disparando {len(SOLICITUDES)} peticiones concurrentes contra {API_URL}\n")

    async with httpx.AsyncClient(base_url=API_URL, timeout=30.0) as cliente:
        inicio = time.perf_counter()
        resultados = await asyncio.gather(
            *(_enviar(cliente, solicitud) for solicitud in SOLICITUDES)
        )
        total = time.perf_counter() - inicio

    for linea, latencia in resultados:
        print(f"{linea}  ({latencia * 1000:.0f} ms)")

    print(f"\nBatch completo en {total * 1000:.0f} ms (encolado, no ejecución).")
    print(
        "Seguimiento: GET /tasks/{job_id} para el estado; LangSmith para costo "
        "y latencia por ejecución."
    )


if __name__ == "__main__":
    asyncio.run(main())
