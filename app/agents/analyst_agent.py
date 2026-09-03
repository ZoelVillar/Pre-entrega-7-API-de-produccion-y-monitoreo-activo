"""Agente especialista: Analista.

Construido con `create_react_agent` y una única tool propia y determinística:
`analizar_sentimiento`. A diferencia del Investigador, este especialista NO
vuelve a buscar nada -- toma exclusivamente lo que trajo el Investigador y
calcula una métrica de sentimiento sobre esas fuentes.

Anti-contaminación de contexto: la instrucción que recibe este nodo se arma
solo con el `detalle` de la última `Contribucion` del investigador y con
`state["motivo_ruteo"]`, nunca con el `AgentState` completo ni con las
contribuciones de rondas de investigación anteriores que ya fueron
superadas.
"""
from __future__ import annotations

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph.state import CompiledStateGraph
from langgraph.prebuilt import create_react_agent

from app.config import settings
from app.state import AgentState, Contribucion
from app.tools import analizar_sentimiento

PROMPT_ANALISTA = (
    "Sos un agente analista especializado en sentimiento de cobertura sobre "
    "tendencias tecnológicas. NO buscás información nueva: tu única entrada "
    "es el texto que te pasan, que ya trae las fuentes que encontró el "
    "Investigador. Tu única herramienta es 'analizar_sentimiento'.\n\n"
    "Cuando recibas el hallazgo del investigador:\n"
    "1. Separá el texto en fuentes individuales (una string por fuente, con "
    "su resumen de contenido).\n"
    "2. Llamá a 'analizar_sentimiento' pasando esa lista de fuentes.\n"
    "3. Con el resultado, escribí una conclusión breve: la clasificación "
    "(positivo/neutral/negativo), el score, y una justificación de 2 a 3 "
    "líneas basada en los términos detectados y el contenido real de las "
    "fuentes.\n\n"
    "Si el resultado de la herramienta trae 'fuentes_analizadas'=0, o si "
    "considerás que las fuentes que te pasaron son insuficientes o "
    "demasiado genéricas para una conclusión confiable (por ejemplo, menos "
    "de 3 fuentes con contenido real y URL), NO fuerces una conclusión: "
    "empezá tu respuesta con la frase exacta 'FUENTES INSUFICIENTES' seguida "
    "del motivo puntual."
)

_react_agent: CompiledStateGraph | None = None


def _obtener_agente() -> CompiledStateGraph:
    """Compila el sub-agente ReAct una sola vez y lo cachea a nivel módulo."""
    global _react_agent
    if _react_agent is None:
        _react_agent = create_react_agent(
            model=ChatAnthropic(model=settings.anthropic_model),
            tools=[analizar_sentimiento],
            prompt=PROMPT_ANALISTA,
            name="analista",
        )
    return _react_agent


def _obtener_ultima_investigacion(state: AgentState) -> str:
    """Devuelve el `detalle` de la contribución más reciente del investigador."""
    for contribucion in reversed(state["contribuciones"]):
        if contribucion["agente"] == "investigador":
            return contribucion["detalle"]
    return ""


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


async def nodo_analista(state: AgentState) -> dict:
    """Nodo async: invoca al sub-agente ReAct del Analista y registra su aporte."""
    hallazgo_investigador = _obtener_ultima_investigacion(state)
    instruccion = (
        f"Hallazgo del investigador a analizar:\n\n{hallazgo_investigador}\n\n"
        f"Instrucción del Supervisor: {state['motivo_ruteo']}"
    )
    resultado = await _obtener_agente().ainvoke(
        {"messages": [HumanMessage(content=instruccion)]}
    )
    analisis = _extraer_texto(resultado["messages"][-1])

    resumen = analisis[:200] + ("..." if len(analisis) > 200 else "")
    contribucion = Contribucion(
        agente="analista",
        paso=state["step_count"],
        resumen=resumen,
        detalle=analisis,
    )

    return {
        "messages": [AIMessage(content=analisis, name="analista")],
        "contribuciones": [contribucion],
    }
