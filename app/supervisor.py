"""Nodo Supervisor: router jerárquico y controlador de flujo.

Es el único "cerebro" del grafo. Decide en cada turno si la tarea pasa al
Investigador, al Analista, o si ya se puede finalizar -- usando una rúbrica
de Suficiencia explícita en su propio prompt (brecha "Validación" de la
consigna: no hay un nodo separado, la validación vive acá).

Doble cinturón contra el "Supervisor Infinito":
1. `nodo_supervisor` lleva su propio `step_count` (independiente de
   cualquier contador interno de un especialista) y, al alcanzar
   `settings.max_supervisor_steps`, fuerza FINISH con una respuesta parcial
   marcada como tal -- sin siquiera llamar al LLM en ese paso.
2. `enrutar_supervisor` (la función de aristas condicionales, tipada con
   `Literal` como pide la consigna) repite el mismo chequeo de forma pura,
   como red de seguridad si por algún motivo `next_agent` quedara
   inconsistente.
"""
from __future__ import annotations

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from app.config import settings
from app.state import AgentState, DestinoRuteo

RUBRICA_SUPERVISOR = """\
Sos el Supervisor de un sistema multi-agente de investigación de tendencias \
tecnológicas. En cada turno decidís si la tarea pasa al Investigador, al \
Analista, o si ya se puede finalizar con una respuesta para el usuario.

Especialistas disponibles:
- "investigador": busca fuentes reales y recientes sobre el tema (usa \
Tavily). No opina ni analiza tono.
- "analista": NO busca nada nuevo. Toma exclusivamente las fuentes que trajo \
el investigador y calcula un análisis de sentimiento (positivo/neutral/\
negativo + score + justificación).

Reglas de decisión, en orden de prioridad:
1. Si todavía no hay ninguna contribución del investigador, rutealo a él \
primero: el analista no tiene nada que analizar sin datos.
2. Si hay contribución del investigador pero ninguna del analista, o la \
última contribución del analista empieza con "FUENTES INSUFICIENTES", \
rutealo al analista (o de vuelta al investigador si el problema es \
específicamente falta de fuentes -- ver regla 4).
3. Rúbrica de Suficiencia para elegir "FINISH" -- las tres condiciones \
deben cumplirse:
   a) El investigador aportó al menos 3 fuentes con URL identificable.
   b) El analista entregó una clasificación de sentimiento con score y \
justificación (no un "FUENTES INSUFICIENTES").
   c) No queda una contradicción abierta entre lo que reportó el \
investigador y lo que concluyó el analista.
   Si las tres se cumplen, elegí "FINISH" y escribí en 'respuesta_final' \
una síntesis clara en español para el usuario, combinando los hallazgos del \
investigador con la conclusión de sentimiento del analista en un párrafo.
4. Si el analista reportó "FUENTES INSUFICIENTES", rutealo de nuevo al \
investigador con una instrucción concreta en 'motivo' sobre qué buscar \
distinto (más específico, más reciente, otra región, otro ángulo) -- nunca \
le pidas repetir la misma búsqueda.
5. No rutees al mismo especialista dos veces seguidas sin que el otro haya \
intervenido en el medio, salvo el caso de la regla 4.

Para 'motivo': siempre una instrucción puntual y accionable para el \
especialista elegido, nunca una descripción genérica ("segui investigando" \
no sirve; "buscá datos de adopción empresarial en Europa en 2026, no solo \
en EE.UU." sí).

Si 'siguiente' es "investigador" o "analista", dejá 'respuesta_final' vacío. \
Si 'siguiente' es "FINISH", 'respuesta_final' es obligatorio y no puede \
quedar vacío.
"""


class DecisionSupervisor(BaseModel):
    """Salida estructurada del Supervisor: mapea 1:1 a los nodos del grafo."""

    siguiente: DestinoRuteo = Field(
        description="Nodo del grafo al que rutear a continuación."
    )
    motivo: str = Field(
        description="Instrucción puntual y accionable para el especialista elegido."
    )
    respuesta_final: str = Field(
        default="",
        description="Síntesis para el usuario. Obligatoria solo si siguiente=='FINISH'.",
    )


def _digest_contribuciones(state: AgentState) -> str:
    """Arma el resumen que ve el Supervisor: solicitud + resúmenes, nunca `messages`."""
    lineas = [f"Solicitud original: {state['solicitud']}"]
    contribuciones = state.get("contribuciones", [])
    if not contribuciones:
        lineas.append("Todavía no hay contribuciones de ningún especialista.")
    else:
        for c in contribuciones:
            lineas.append(f"[paso {c['paso']}] {c['agente']}: {c['resumen']}")
    return "\n".join(lineas)


def _sintetizar_respuesta_parcial(state: AgentState) -> str:
    """Respuesta de emergencia cuando se agota `step_count` sin cerrar la rúbrica."""
    encabezado = (
        f"[Respuesta parcial: se alcanzó el tope de {settings.max_supervisor_steps} "
        "pasos del Supervisor antes de completar la rúbrica de suficiencia.]"
    )
    contribuciones = state.get("contribuciones", [])
    if not contribuciones:
        return f"{encabezado} Ningún especialista llegó a aportar información."

    partes = [encabezado, "Último aporte disponible de cada especialista:"]
    vistos: set[str] = set()
    for c in reversed(contribuciones):
        if c["agente"] not in vistos:
            vistos.add(c["agente"])
            partes.append(f"- {c['agente']} (paso {c['paso']}): {c['detalle']}")
    return "\n".join(partes)


async def nodo_supervisor(state: AgentState) -> dict:
    """Nodo async: decide el próximo destino o cierra con una síntesis final."""
    paso_actual = state.get("step_count", 0) + 1

    if paso_actual >= settings.max_supervisor_steps:
        return {
            "step_count": paso_actual,
            "next_agent": "FINISH",
            "motivo_ruteo": "",
            "task_completed": True,
            "respuesta_final": _sintetizar_respuesta_parcial(state),
        }

    digest = _digest_contribuciones(state)
    llm = ChatAnthropic(model=settings.anthropic_model).with_structured_output(
        DecisionSupervisor
    )
    decision: DecisionSupervisor = await llm.ainvoke(
        [SystemMessage(content=RUBRICA_SUPERVISOR), HumanMessage(content=digest)]
    )

    return {
        "step_count": paso_actual,
        "next_agent": decision.siguiente,
        "motivo_ruteo": decision.motivo,
        "task_completed": decision.siguiente == "FINISH",
        "respuesta_final": (
            decision.respuesta_final
            if decision.siguiente == "FINISH"
            else state.get("respuesta_final", "")
        ),
    }


def enrutar_supervisor(state: AgentState) -> DestinoRuteo:
    """Arista condicional tipada con `Literal`: mapea la decisión a un nodo del grafo.

    Red de seguridad sync y pura (sin I/O): repite el chequeo de tope que ya
    aplicó `nodo_supervisor`, para que un `next_agent` inconsistente nunca
    pueda mantener el grafo corriendo más allá de `max_supervisor_steps`.
    """
    if state["step_count"] >= settings.max_supervisor_steps:
        return "FINISH"
    return state["next_agent"]
