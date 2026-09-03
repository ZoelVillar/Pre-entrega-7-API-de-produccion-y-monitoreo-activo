"""API REST del orquestador multi-agente.

Tres endpoints, ninguno bloqueante:

    POST /tasks                  -> encola la investigación, devuelve job_id (202)
    GET  /tasks/{job_id}         -> estado + resultado + aprobación pendiente
    POST /tasks/{job_id}/approve -> encola la reanudación del flujo HITL (202)

El grafo y las conexiones a Redis se crean una sola vez en el `lifespan` y
viven en `app.state`. El checkpointer en particular es un context manager de
vida completa: `asetup()` (que crea los índices de RediSearch) corre una vez al
arrancar, no una vez por request.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator
from uuid import uuid4

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request, status
from langgraph.checkpoint.redis.aio import AsyncRedisSaver
from langgraph.graph.state import CompiledStateGraph
from pydantic import BaseModel, Field
from redis.asyncio import Redis

from app.config import settings
from app.graph import build_graph
from app.jobs import crear_job, obtener_job
from app.observability import verificar_observabilidad
from app.worker import config_thread, ejecutar_job, reanudar_job

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)


# --- Ciclo de vida ------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Inicializa checkpointer, cliente de Redis y grafo compilado."""
    verificar_observabilidad()

    async with AsyncRedisSaver.from_conn_string(settings.redis_url) as checkpointer:
        # Crea los índices de RediSearch que usa el checkpointer. Idempotente,
        # pero una vez por arranque alcanza -- nunca dentro de un request.
        await checkpointer.asetup()
        logger.info("Checkpointer de LangGraph listo en %s", settings.redis_url)

        cliente = Redis.from_url(settings.redis_url, decode_responses=True)
        try:
            app.state.grafo = build_graph(checkpointer=checkpointer)
            app.state.redis = cliente
            logger.info("Grafo compilado con checkpointer. API lista.")
            yield
        finally:
            await cliente.aclose()
            logger.info("Cliente de Redis cerrado.")


app = FastAPI(
    title="Orquestador multi-agente de investigación",
    description=(
        "API asíncrona sobre un grafo jerárquico LangGraph (Supervisor + "
        "Investigador + Analista) con checkpoints en Redis, trazas en LangSmith "
        "y aprobación humana obligatoria antes de publicar el reporte."
    ),
    version="7.0.0",
    lifespan=lifespan,
)


def obtener_grafo(request: Request) -> CompiledStateGraph:
    return request.app.state.grafo


def obtener_redis(request: Request) -> Redis:
    return request.app.state.redis


# --- Contratos ----------------------------------------------------------------


class TareaRequest(BaseModel):
    """Cuerpo de `POST /tasks`."""

    solicitud: str = Field(
        min_length=3,
        description="Tema a investigar.",
        examples=["Adopción empresarial de agentes de IA en 2026"],
    )


class TareaAceptada(BaseModel):
    """Respuesta 202 de los endpoints que encolan trabajo."""

    job_id: str
    status: str
    solicitud: str


class AprobacionRequest(BaseModel):
    """Cuerpo de `POST /tasks/{job_id}/approve`."""

    aprobado: bool = Field(description="True publica el reporte; False lo descarta.")
    revisor: str = Field(default="", description="Quién toma la decisión.")
    comentario: str = Field(default="", description="Motivo, sobre todo si se rechaza.")


class EstadoTarea(BaseModel):
    """Respuesta de `GET /tasks/{job_id}`: hash del job + estado del grafo."""

    job_id: str
    status: str
    solicitud: str
    error: str
    creado_en: str
    actualizado_en: str
    respuesta_final: str = ""
    publicado: bool = False
    confirmacion_publicacion: str = ""
    aprobacion_pendiente: dict[str, Any] | None = None


# --- Endpoints ----------------------------------------------------------------


@app.get("/health", tags=["infra"])
async def health(cliente: Redis = Depends(obtener_redis)) -> dict[str, str]:
    """Chequeo de vida que incluye la conectividad real con Redis."""
    await cliente.ping()
    return {"status": "ok", "redis": "ok", "proyecto": settings.langchain_project}


@app.post(
    "/tasks",
    response_model=TareaAceptada,
    status_code=status.HTTP_202_ACCEPTED,
    tags=["tareas"],
)
async def crear_tarea(
    body: TareaRequest,
    background_tasks: BackgroundTasks,
    grafo: CompiledStateGraph = Depends(obtener_grafo),
    cliente: Redis = Depends(obtener_redis),
) -> TareaAceptada:
    """Encola una investigación y devuelve su `job_id` sin esperar al grafo.

    Lo único que ocurre dentro del handler es un `HSET` en Redis; la corrida
    del grafo (minutos de LLM y búsquedas) se va a un BackgroundTask. El
    `job_id` es también el `thread_id` de LangGraph.
    """
    job_id = uuid4().hex
    await crear_job(cliente, job_id, body.solicitud)
    background_tasks.add_task(ejecutar_job, grafo, cliente, job_id, body.solicitud)

    logger.info("Job %s encolado: %s", job_id, body.solicitud)
    return TareaAceptada(job_id=job_id, status="PENDING", solicitud=body.solicitud)


@app.get("/tasks/{job_id}", response_model=EstadoTarea, tags=["tareas"])
async def consultar_tarea(
    job_id: str,
    grafo: CompiledStateGraph = Depends(obtener_grafo),
    cliente: Redis = Depends(obtener_redis),
) -> EstadoTarea:
    """Devuelve el estado del job y, cuando existe, el resultado del grafo.

    El ciclo de vida sale del hash `job:{job_id}`; el contenido (respuesta
    final, publicación, aprobación pendiente) sale del checkpointer vía
    `aget_state()`. Una sola fuente de verdad para cada cosa, sin duplicar el
    resultado en dos lugares que puedan divergir.
    """
    registro = await obtener_job(cliente, job_id)
    if registro is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No existe el job {job_id}.")

    respuesta = EstadoTarea(
        job_id=job_id,
        status=registro["status"],
        solicitud=registro["solicitud"],
        error=registro["error"],
        creado_en=registro["creado_en"],
        actualizado_en=registro["actualizado_en"],
    )

    snapshot = await grafo.aget_state(config_thread(job_id))
    valores: dict[str, Any] = snapshot.values or {}
    respuesta.respuesta_final = valores.get("respuesta_final", "")
    respuesta.publicado = bool(valores.get("publicado", False))
    respuesta.confirmacion_publicacion = valores.get("confirmacion_publicacion", "")
    if snapshot.interrupts:
        respuesta.aprobacion_pendiente = snapshot.interrupts[0].value

    return respuesta


@app.post(
    "/tasks/{job_id}/approve",
    response_model=TareaAceptada,
    status_code=status.HTTP_202_ACCEPTED,
    tags=["tareas"],
)
async def aprobar_tarea(
    job_id: str,
    body: AprobacionRequest,
    background_tasks: BackgroundTasks,
    grafo: CompiledStateGraph = Depends(obtener_grafo),
    cliente: Redis = Depends(obtener_redis),
) -> TareaAceptada:
    """Registra la decisión humana y encola la reanudación del grafo.

    El `Command(resume=...)` NO corre acá: reanudar significa volver a entrar
    al nodo `publicador` y, si se aprueba, ejecutar la publicación -- trabajo
    que no puede colgarse de la request. Va al mismo BackgroundTask que usa
    `POST /tasks`.
    """
    registro = await obtener_job(cliente, job_id)
    if registro is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No existe el job {job_id}.")

    if registro["status"] != "AWAITING_APPROVAL":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"El job {job_id} está en {registro['status']}; sólo se puede aprobar "
            "uno en AWAITING_APPROVAL.",
        )

    background_tasks.add_task(reanudar_job, grafo, cliente, job_id, body.model_dump())

    logger.info(
        "Job %s reanudado por %s (aprobado=%s)",
        job_id,
        body.revisor or "revisor anónimo",
        body.aprobado,
    )
    return TareaAceptada(
        job_id=job_id, status="RUNNING", solicitud=registro["solicitud"]
    )
