"""Nodo Human-in-the-loop: pausa obligatoria antes de publicar el reporte.

El punto de interrupción NO es la decisión "FINISH" del Supervisor. Cuando el
Supervisor da la tarea por cerrada, el grafo entra en `publicador`, un nodo
posterior cuyo único trabajo es pedir una aprobación humana antes de ejecutar
`tools.publicar_reporte` -- la acción con efecto secundario. Esto separa
limpiamente dos preguntas distintas: "¿la investigación está completa?" (la
decide el Supervisor con su rúbrica) y "¿la publicamos?" (la decide una
persona).

Mecánica: `interrupt(payload)` levanta un `GraphInterrupt`, LangGraph persiste
el checkpoint en Redis y `ainvoke` retorna con la clave `__interrupt__`. El
proceso puede morir en ese punto: el estado sobrevive en Redis y cualquier
worker que reanude el mismo `thread_id` con `Command(resume=...)` continúa
desde acá.
"""
from __future__ import annotations

import logging
from typing import Any

from langgraph.types import interrupt

from app.state import AgentState
from app.tools import publicar_reporte

logger = logging.getLogger(__name__)


def _armar_payload_aprobacion(state: AgentState) -> dict[str, Any]:
    """Arma lo que ve el humano para decidir. Función pura: se re-ejecuta al reanudar."""
    return {
        "tipo": "aprobacion_publicacion",
        "accion_pendiente": "publicar_reporte",
        "solicitud": state["solicitud"],
        "respuesta_final": state["respuesta_final"],
        "pasos_supervisor": state.get("step_count", 0),
        "contribuciones": [
            {"agente": c["agente"], "paso": c["paso"], "resumen": c["resumen"]}
            for c in state.get("contribuciones", [])
        ],
        "instrucciones": (
            "Revisá 'respuesta_final' y respondé con "
            'POST /tasks/{job_id}/approve {"aprobado": true|false, '
            '"revisor": "...", "comentario": "..."}'
        ),
    }


def _interpretar_decision(respuesta: Any) -> tuple[bool, str, str]:
    """Normaliza el valor que llegó por `Command(resume=...)`.

    El camino normal es el dict que manda `POST /tasks/{job_id}/approve`, pero
    también se acepta un booleano pelado para poder reanudar un thread a mano
    desde una consola con `Command(resume=True)`.
    """
    if isinstance(respuesta, dict):
        aprobado = bool(respuesta.get("aprobado"))
        revisor = str(respuesta.get("revisor") or "").strip()
        comentario = str(respuesta.get("comentario") or "").strip()
        return aprobado, revisor, comentario
    return bool(respuesta), "", ""


async def nodo_publicador(state: AgentState) -> dict:
    """Nodo async: pausa para aprobación humana y, si la obtiene, publica.

    Cuidado al modificar este nodo: LangGraph reanuda **re-ejecutando el nodo
    entero desde el principio**, no desde la línea del `interrupt()`. Por eso
    todo lo que va antes de `interrupt()` es puro (armar el payload) y la
    única línea con efecto secundario (`publicar_reporte`) va estrictamente
    después. Así el reporte se publica exactamente una vez, no una por cada
    pasada del nodo.
    """
    respuesta = interrupt(_armar_payload_aprobacion(state))
    aprobado, revisor, comentario = _interpretar_decision(respuesta)

    if not aprobado:
        motivo = comentario or "sin comentario"
        quien = revisor or "revisor anónimo"
        logger.info("Publicación rechazada por %s: %s", quien, motivo)
        # Rechazar no es fallar: el job termina en DONE, con el reporte sin
        # publicar y el motivo asentado en el estado.
        return {
            "publicado": False,
            "confirmacion_publicacion": f"Publicación rechazada por {quien}: {motivo}",
        }

    confirmacion = await publicar_reporte(
        solicitud=state["solicitud"],
        contenido=state["respuesta_final"],
        aprobado_por=revisor or "revisor anónimo",
    )
    return {"publicado": True, "confirmacion_publicacion": confirmacion}
