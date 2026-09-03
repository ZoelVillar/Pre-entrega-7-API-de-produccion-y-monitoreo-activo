"""Agente especialista: Investigador.

Construido con `create_react_agent` (no un `llm.invoke()` con prompt fijo,
como el "agente_investigador" del profesor) y una única tool real:
`TavilySearch`. Es el único de los dos especialistas que consulta una fuente
externa.

Anti-contaminación de contexto (brecha explícita de la consigna): la
instrucción que arma `_construir_instruccion` sale exclusivamente de
`state["solicitud"]` y `state["motivo_ruteo"]`. Nunca se le pasa
`state["messages"]` completo ni las `contribuciones` de otros agentes -- el
sub-agente ReAct corre con su propio historial de mensajes, fresco, y solo su
hallazgo final vuelve al estado compartido.
"""
from __future__ import annotations

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph.state import CompiledStateGraph
from langgraph.prebuilt import create_react_agent

from app.config import settings
from app.state import AgentState, Contribucion
from app.tools import crear_tool_busqueda

PROMPT_INVESTIGADOR = (
    "Sos un agente investigador especializado en tendencias tecnológicas. "
    "Tu única fuente de información es la herramienta 'tavily_search'; nunca "
    "inventes datos, cifras o fuentes que no provengan de ella.\n\n"
    "Cuando recibas un tema de investigación, buscá información reciente y "
    "relevante y sintetizá un hallazgo claro para un analista que va a leerlo "
    "después. Tu respuesta final debe:\n"
    "1. Citar al menos 3 fuentes distintas, cada una con su URL exacta tal "
    "como la devolvió la herramienta.\n"
    "2. Para cada fuente, incluir un resumen breve de su contenido relevante "
    "al tema.\n"
    "3. No opinar ni evaluar el tono de las fuentes -- esa tarea es del "
    "Analista, no tuya.\n\n"
    "Si la instrucción del Supervisor pide refinar o ampliar una búsqueda "
    "anterior, hacé una nueva búsqueda más específica en vez de repetir la "
    "misma consulta."
)

_react_agent: CompiledStateGraph | None = None


def _obtener_agente() -> CompiledStateGraph:
    """Compila el sub-agente ReAct una sola vez y lo cachea a nivel módulo."""
    global _react_agent
    if _react_agent is None:
        _react_agent = create_react_agent(
            model=ChatAnthropic(model=settings.anthropic_model),
            tools=[crear_tool_busqueda()],
            prompt=PROMPT_INVESTIGADOR,
            name="investigador",
        )
    return _react_agent


def _construir_instruccion(state: AgentState) -> str:
    """Arma la instrucción puntual del especialista: solo tema + motivo_ruteo."""
    return (
        f"Tema de investigación: {state['solicitud']}\n\n"
        f"Instrucción del Supervisor: {state['motivo_ruteo']}"
    )


def _extraer_texto(mensaje: AIMessage) -> str:
    """Normaliza `mensaje.content`: Claude puede devolver un str o una lista de
    content blocks (ej. cuando el turno final mezcla bloques de texto con
    bloques de otro tipo)."""
    contenido = mensaje.content
    if isinstance(contenido, str):
        return contenido
    partes = []
    for bloque in contenido:
        if isinstance(bloque, str):
            partes.append(bloque)
        elif isinstance(bloque, dict) and bloque.get("type") == "text":
            partes.append(bloque.get("text", ""))
    return "".join(partes)


async def nodo_investigador(state: AgentState) -> dict:
    """Nodo async: invoca al sub-agente ReAct del Investigador y registra su aporte."""
    instruccion = _construir_instruccion(state)
    resultado = await _obtener_agente().ainvoke(
        {"messages": [HumanMessage(content=instruccion)]}
    )
    hallazgo = _extraer_texto(resultado["messages"][-1])

    resumen = hallazgo[:200] + ("..." if len(hallazgo) > 200 else "")
    contribucion = Contribucion(
        agente="investigador",
        paso=state["step_count"],
        resumen=resumen,
        detalle=hallazgo,
    )

    return {
        "messages": [AIMessage(content=hallazgo, name="investigador")],
        "contribuciones": [contribucion],
    }
