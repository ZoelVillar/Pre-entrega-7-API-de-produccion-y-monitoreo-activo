"""Ejecución del grafo en background y transiciones del estado del job.

Dos entradas públicas, una por cada forma de poner el grafo a correr:
`ejecutar_job` (arranca un thread nuevo) y `reanudar_job` (continúa uno que
quedó frenado en el nodo HITL). Las dos son `BackgroundTask` de FastAPI: ningún
handler espera a que el grafo termine.

Regla no negociable de este módulo: un job nunca queda colgado en RUNNING. Toda
excepción que escape del grafo se captura, se loguea con traceback y se
persiste como FAILED en Redis con el mensaje -- si no, el cliente se queda
haciendo polling para siempre contra un trabajo que ya murió.
"""
from __future__ import annotations

import logging
from typing import Any

from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command
from redis.asyncio import Redis

from app.config import settings
from app.jobs import actualizar_estado

logger = logging.getLogger(__name__)


def config_thread(job_id: str) -> dict[str, Any]:
    """Config de LangGraph para un job. `thread_id` es el `job_id`, no otro ID.

    Un solo identificador para "el trabajo" (hash `job:{id}` en Redis) y "el
    hilo" (checkpoints del grafo). Es lo que permite que `GET /tasks/{job_id}`
    lea el resultado directamente del checkpointer sin tener que mantener un
    mapeo aparte.
    """
    return {
        "configurable": {"thread_id": job_id},
        "recursion_limit": settings.recursion_limit,
    }


def estado_inicial(solicitud: str) -> dict[str, Any]:
    """Estado con el que arranca un thread nuevo.

    `AgentState` es un TypedDict y no admite defaults declarativos, así que
    todos los campos se inicializan explícitamente acá.
    """
    return {
        "messages": [],
        "solicitud": solicitud,
        "next_agent": "investigador",
        "motivo_ruteo": f"Investigar información reciente sobre: {solicitud}",
        "contribuciones": [],
        "step_count": 0,
        "task_completed": False,
        "respuesta_final": "",
        "publicado": False,
        "confirmacion_publicacion": "",
    }


async def _correr_grafo(
    grafo: CompiledStateGraph,
    cliente: Redis,
    job_id: str,
    entrada: Any,
) -> None:
    """Corre el grafo y mapea el desenlace al estado del job en Redis.

    Único lugar donde se transiciona a RUNNING / AWAITING_APPROVAL / DONE /
    FAILED, para que arrancar y reanudar compartan exactamente la misma
    máquina de estados.
    """
    try:
        await actualizar_estado(cliente, job_id, "RUNNING")

        # version="v1" (default): devuelve un dict plano y, si el grafo se
        # interrumpió, agrega la clave "__interrupt__" con los Interrupt
        # pendientes. Con version="v2" habría que leer GraphOutput.interrupts.
        resultado = await grafo.ainvoke(entrada, config=config_thread(job_id))

        if resultado.get("__interrupt__"):
            logger.info("Job %s pausado: espera aprobación humana.", job_id)
            await actualizar_estado(cliente, job_id, "AWAITING_APPROVAL")
        else:
            logger.info("Job %s completado.", job_id)
            await actualizar_estado(cliente, job_id, "DONE")

    except Exception as exc:  # noqa: BLE001 - la captura amplia es intencional
        # `Exception` y no `BaseException`: un `asyncio.CancelledError` durante
        # el shutdown del server tiene que seguir propagándose.
        logger.exception("Job %s falló durante la ejecución del grafo.", job_id)
        detalle = f"{type(exc).__name__}: {exc}"
        try:
            await actualizar_estado(cliente, job_id, "FAILED", error=detalle)
        except Exception:  # noqa: BLE001
            # Si además se cayó Redis no hay dónde asentar el fallo; que quede
            # al menos en el log del proceso.
            logger.exception(
                "Job %s falló y además no se pudo persistir el estado FAILED.", job_id
            )


async def ejecutar_job(
    grafo: CompiledStateGraph,
    cliente: Redis,
    job_id: str,
    solicitud: str,
) -> None:
    """Arranca un thread nuevo del grafo para una solicitud. Corre en background."""
    await _correr_grafo(grafo, cliente, job_id, estado_inicial(solicitud))


async def reanudar_job(
    grafo: CompiledStateGraph,
    cliente: Redis,
    job_id: str,
    decision: dict[str, Any],
) -> None:
    """Reanuda un job frenado en el nodo HITL con la decisión del humano.

    `decision` es el cuerpo de `POST /tasks/{job_id}/approve` ya validado; llega
    hasta `hitl.nodo_publicador` como valor de retorno de su `interrupt()`.
    """
    await _correr_grafo(grafo, cliente, job_id, Command(resume=decision))
