"""Esquema de estado compartido del orquestador multi-agente.

`AgentState` hereda de `MessagesState` (en vez de reimplementar el reducer de
mensajes con un TypedDict propio) y suma los campos que la topología
jerárquica necesita: a qué especialista rutear a continuación, el contador de
pasos del Supervisor y, sobre todo, `contribuciones` -- un registro explícito
de qué agente aportó qué información, en qué paso.

`contribuciones` es una lista con reducer `operator.add`, no un
`dict[str, str]` keyed por nombre de agente: cuando el Supervisor devuelve la
tarea al investigador para refinar (ver `supervisor.py`), el investigador
contribuye una segunda vez. Un dict pisaría la primera contribución con la
segunda y se perdería justo la trazabilidad que la consigna pide proteger
("evitar la pérdida de contexto en la comunicación asíncrona"). La lista
preserva orden y atribución completa.
"""
from __future__ import annotations

import operator
from typing import Annotated, Literal, TypedDict

from langgraph.graph import MessagesState

NombreAgente = Literal["investigador", "analista"]
DestinoRuteo = Literal["investigador", "analista", "FINISH"]


class Contribucion(TypedDict):
    """Un aporte atómico de un especialista, tal como lo ve el Supervisor."""

    agente: NombreAgente
    paso: int
    resumen: str
    detalle: str


class AgentState(MessagesState):
    """Estado compartido: `messages` (heredado) + control de ruteo jerárquico.

    `motivo_ruteo` es la instrucción puntual que el Supervisor le da al
    especialista que va a intervenir a continuación -- es lo único que ese
    especialista recibe además del `detalle` de la contribución previa
    relevante, nunca el AgentState completo (ver notas de anti-contaminación
    en `agents/research_agent.py` y `agents/analyst_agent.py`).

    `publicado` y `confirmacion_publicacion` son el resultado del nodo HITL
    (`hitl.nodo_publicador`): quedan en el checkpoint para que
    `GET /tasks/{job_id}` pueda reportar si el reporte llegó a publicarse sin
    tener que duplicar ese dato en el hash de job de Redis.

    Nota: un `TypedDict` no admite valores por defecto declarativos. Los
    valores iniciales de todos estos campos (`publicado=False`,
    `confirmacion_publicacion=""`, etc.) los fija `worker.estado_inicial()`.
    """

    solicitud: str
    next_agent: DestinoRuteo
    motivo_ruteo: str
    contribuciones: Annotated[list[Contribucion], operator.add]
    step_count: int
    task_completed: bool
    respuesta_final: str
    publicado: bool
    confirmacion_publicacion: str
