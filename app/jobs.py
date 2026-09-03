"""Job store en Redis: el estado del trabajo tal como lo ve el cliente de la API.

Es un store distinto del checkpointer de LangGraph, con distinto propósito y
distinto keyspace:

- `job:{job_id}` (este módulo, hash plano) responde "¿en qué estado está mi
  pedido?" -- lo que consulta el cliente mientras hace polling.
- Las claves del `AsyncRedisSaver` (`checkpoint:*`) guardan el estado interno
  del grafo, que es de LangGraph y no tiene por qué filtrarse a la API.

Ambos usan la misma librería (`redis`), pero instancias de cliente distintas:
acá `redis.asyncio.Redis`, allá el que administra el checkpointer.

Como `thread_id == job_id` (un solo identificador para "el trabajo" y "el
hilo"), el hash guarda sólo el ciclo de vida y no duplica el contenido: la
respuesta del grafo la lee `main.py` del checkpointer con `aget_state()`.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from redis.asyncio import Redis

EstadoJob = Literal["PENDING", "RUNNING", "AWAITING_APPROVAL", "DONE", "FAILED"]

ESTADOS_TERMINALES: frozenset[str] = frozenset({"DONE", "FAILED"})


def clave_job(job_id: str) -> str:
    """Clave del hash de un job en Redis."""
    return f"job:{job_id}"


def _ahora() -> str:
    return datetime.now(timezone.utc).isoformat()


async def crear_job(cliente: Redis, job_id: str, solicitud: str) -> dict[str, str]:
    """Registra un job nuevo en PENDING y devuelve el hash tal como quedó.

    Se llama desde el handler de `POST /tasks` antes de encolar el trabajo, no
    desde el background task: si el cliente recibe un `job_id`, ese `job_id`
    ya existe en Redis y un `GET` inmediato no puede dar 404.
    """
    ahora = _ahora()
    registro = {
        "status": "PENDING",
        "solicitud": solicitud,
        "error": "",
        "creado_en": ahora,
        "actualizado_en": ahora,
    }
    await cliente.hset(clave_job(job_id), mapping=registro)
    return registro


async def actualizar_estado(
    cliente: Redis,
    job_id: str,
    estado: EstadoJob,
    *,
    error: str = "",
) -> None:
    """Transiciona el estado de un job y refresca su timestamp.

    `error` se escribe siempre (vacío incluido) para que un reintento que
    termina bien no arrastre el mensaje de error del intento anterior.
    """
    await cliente.hset(
        clave_job(job_id),
        mapping={"status": estado, "error": error, "actualizado_en": _ahora()},
    )


async def obtener_job(cliente: Redis, job_id: str) -> dict[str, str] | None:
    """Devuelve el hash del job, o `None` si no existe."""
    registro = await cliente.hgetall(clave_job(job_id))
    return registro or None
