"""Ensamblado del grafo: topología jerárquica Supervisor + 2 especialistas + HITL.

A diferencia de la cadena lineal del profesor (`add_edge` fijo,
investigador -> redactor), acá el único `add_edge` fijo es el que entra al
Supervisor; la salida del Supervisor es siempre una arista condicional
(`add_conditional_edges`), y ambos especialistas vuelven al Supervisor -- es
el Supervisor, no el flujo, quien decide cuándo terminar.

Cambios de la Entrega 7:
- `build_graph` recibe el checkpointer (un `AsyncRedisSaver` inyectado desde
  el lifespan de FastAPI). Sin él no hay HITL posible: `interrupt()` necesita
  persistencia para poder reanudar el thread más tarde.
- "FINISH" ya no va directo a END, sino al nodo `publicador`, que pide la
  aprobación humana antes de ejecutar la acción con efecto secundario.
"""
from __future__ import annotations

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Checkpointer

from app.agents.analyst_agent import nodo_analista
from app.agents.research_agent import nodo_investigador
from app.hitl import nodo_publicador
from app.state import AgentState
from app.supervisor import enrutar_supervisor, nodo_supervisor


def build_graph(checkpointer: Checkpointer = None) -> CompiledStateGraph:
    """Construye y compila el StateGraph jerárquico.

    `checkpointer` es opcional sólo para poder inspeccionar la topología sin
    Redis (ver `exportar_mermaid`); para ejecutar el grafo vía la API es
    obligatorio, porque el nodo `publicador` interrumpe y el thread tiene que
    poder reanudarse en otra request.
    """
    builder = StateGraph(AgentState)

    builder.add_node("supervisor", nodo_supervisor)
    builder.add_node("investigador", nodo_investigador)
    builder.add_node("analista", nodo_analista)
    builder.add_node("publicador", nodo_publicador)

    builder.add_edge(START, "supervisor")
    builder.add_conditional_edges(
        "supervisor",
        enrutar_supervisor,
        {
            "investigador": "investigador",
            "analista": "analista",
            "FINISH": "publicador",
        },
    )
    builder.add_edge("investigador", "supervisor")
    builder.add_edge("analista", "supervisor")
    builder.add_edge("publicador", END)

    return builder.compile(checkpointer=checkpointer)


def exportar_mermaid(app: CompiledStateGraph) -> str:
    """Devuelve el diagrama Mermaid en texto (`draw_mermaid`, sin dependencias gráficas)."""
    return app.get_graph().draw_mermaid()
