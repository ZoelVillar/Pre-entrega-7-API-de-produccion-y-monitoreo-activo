"""Herramientas acotadas de los dos especialistas, más la acción crítica del HITL.

`crear_tool_busqueda` envuelve `TavilySearch` (paquete `langchain-tavily`,
sucesor mantenido de la `TavilySearchResults` de `langchain_community`, que
está en sunset) para el Agente Investigador.

`analizar_sentimiento` es la herramienta del Agente Analista: una función
propia, determinística y sin LLM, que clasifica el tono de un conjunto de
fuentes por léxico ponderado. Queda separada de "la opinión del LLM" a
propósito -- es lo que hace que el Analista sea una herramienta de
cómputo acotada y no solo un segundo prompt genérico.

`publicar_reporte` (Entrega 7) NO es una tool de ningún agente: es la acción
con efecto secundario que el flujo Human-in-the-loop protege. Ver su docstring
y `hitl.py`.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from uuid import uuid4

from langchain_core.tools import tool
from langchain_tavily import TavilySearch

from app.config import settings

logger = logging.getLogger(__name__)

# Léxico ES/EN con peso por término. Los positivos superan a los negativos en
# cantidad porque el dominio (adopción de tecnología) tiende a producir más
# cobertura neutral-optimista que catastrofista; el peso compensa esto en el
# score, no la cantidad de términos.
_TERMINOS_POSITIVOS: dict[str, float] = {
    "adopción": 1.0, "adoption": 1.0,
    "crecimiento": 1.2, "growth": 1.2,
    "récord": 1.3, "record": 1.3,
    "impulso": 1.0, "momentum": 1.0,
    "inversión": 0.8, "investment": 0.8,
    "éxito": 1.3, "success": 1.3,
    "innovación": 0.9, "innovation": 0.9,
    "avance": 0.9, "breakthrough": 1.3,
    "eficiencia": 0.8, "efficiency": 0.8,
    "optimista": 1.1, "optimistic": 1.1,
    "líder": 0.7, "leading": 0.7,
    "oportunidad": 0.8, "opportunity": 0.8,
}

_TERMINOS_NEGATIVOS: dict[str, float] = {
    "riesgo": 1.0, "risk": 1.0,
    "caída": 1.3, "decline": 1.3,
    "fracaso": 1.5, "failure": 1.5,
    "burbuja": 1.4, "bubble": 1.4,
    "preocupación": 1.0, "concern": 1.0,
    "despidos": 1.2, "layoffs": 1.2,
    "recorte": 1.0, "cutback": 1.0,
    "escepticismo": 1.1, "skepticism": 1.1,
    "estancamiento": 1.0, "stagnation": 1.0,
    "regulación": 0.6, "regulation": 0.6,
    "sobreestimado": 1.2, "overhyped": 1.2,
    "advertencia": 0.9, "warning": 0.9,
}

_UMBRAL_POSITIVO = 0.15
_UMBRAL_NEGATIVO = -0.15


def crear_tool_busqueda():
    """Instancia la tool de búsqueda real de Tavily para el Investigador.

    Se instancia bajo demanda (no a nivel de módulo) para que la validación
    de `TAVILY_API_KEY` en `config.py` ya haya corrido antes de construir el
    cliente.
    """
    return TavilySearch(
        max_results=settings.tavily_max_results,
        topic="news",
        search_depth="advanced",
    )


def _tokenizar(texto: str) -> list[str]:
    return re.findall(r"[a-záéíóúñ]+", texto.lower())


def _puntuar_fuente(texto: str) -> tuple[float, list[str]]:
    """Calcula el score de una fuente y los términos que lo explican."""
    tokens = _tokenizar(texto)
    score = 0.0
    detectados: list[str] = []
    for token in tokens:
        if token in _TERMINOS_POSITIVOS:
            score += _TERMINOS_POSITIVOS[token]
            detectados.append(f"+{token}")
        elif token in _TERMINOS_NEGATIVOS:
            score -= _TERMINOS_NEGATIVOS[token]
            detectados.append(f"-{token}")
    total_terminos = max(len(tokens), 1)
    score_normalizado = score / (total_terminos**0.5)
    return score_normalizado, detectados


@tool
def analizar_sentimiento(fuentes: list[str]) -> dict:
    """Clasifica el sentimiento agregado de un conjunto de fuentes textuales.

    Usá esta herramienta cuando ya tengas el texto de las fuentes que trajo
    el Investigador (título + resumen/contenido de cada resultado) y
    necesites determinar si la cobertura sobre el tema es mayormente
    positiva, negativa o neutral. NO uses esta herramienta para buscar
    información nueva: recibe texto ya obtenido, no hace ninguna consulta
    externa.

    El parámetro 'fuentes' es una lista de strings, uno por fuente (por
    ejemplo, "titulo. contenido_resumido" de cada resultado de búsqueda).
    Pasá al menos una fuente; fuentes vacías o solo espacios se descartan
    antes de puntuar.

    La clasificación es léxica y determinística (sin LLM): cada fuente se
    puntúa por presencia de términos positivos/negativos ponderados en
    español e inglés (ej. "crecimiento"/"growth" suman, "fracaso"/"failure"
    restan), normalizado por la longitud de la fuente. El score global es el
    promedio de los scores por fuente.

    Devuelve un dict con:
    - 'clasificacion': "positivo" | "neutral" | "negativo"
    - 'score_global': float, promedio de los scores individuales
    - 'por_fuente': list[dict] con 'score' y 'terminos_detectados' de cada
      fuente (términos que explican el score, prefijados con + o -)
    - 'fuentes_analizadas': int, cantidad de fuentes no vacías consideradas

    Si no hay fuentes válidas, 'clasificacion' es "neutral" con
    'fuentes_analizadas'=0 -- eso es una señal de que el Investigador no
    aportó suficiente material y hay que pedirle más.
    """
    fuentes_validas = [f.strip() for f in fuentes if f and f.strip()]

    if not fuentes_validas:
        return {
            "clasificacion": "neutral",
            "score_global": 0.0,
            "por_fuente": [],
            "fuentes_analizadas": 0,
        }

    por_fuente = []
    scores = []
    for fuente in fuentes_validas:
        score, terminos = _puntuar_fuente(fuente)
        scores.append(score)
        por_fuente.append({"score": round(score, 3), "terminos_detectados": terminos})

    score_global = sum(scores) / len(scores)

    if score_global >= _UMBRAL_POSITIVO:
        clasificacion = "positivo"
    elif score_global <= _UMBRAL_NEGATIVO:
        clasificacion = "negativo"
    else:
        clasificacion = "neutral"

    return {
        "clasificacion": clasificacion,
        "score_global": round(score_global, 3),
        "por_fuente": por_fuente,
        "fuentes_analizadas": len(fuentes_validas),
    }


# --- Acción crítica protegida por el flujo Human-in-the-loop (Entrega 7) ------

_REGISTRO_PUBLICACIONES: list[dict[str, str]] = []


async def publicar_reporte(solicitud: str, contenido: str, aprobado_por: str) -> str:
    """Publica un reporte de investigación. Efecto secundario SIMULADO.

    A diferencia de `analizar_sentimiento`, esta función deliberadamente NO
    está decorada con `@tool`: ningún agente LLM puede elegir invocarla. La
    ejecuta el nodo `hitl.nodo_publicador` y sólo después de que un humano
    aprobó explícitamente vía `POST /tasks/{job_id}/approve`. Representa la
    clase de acción que la consigna pide proteger: irreversible una vez hecha
    (un reporte publicado no se "des-publica") y con costo asociado.

    La simulación se queda dentro del proceso -- anota la publicación en un
    registro en memoria y la loguea -- para no depender de ningún servicio
    externo. Es `async` porque en un sistema real esto sería I/O de red, y
    así la firma no cambia el día que se conecte a un destino de verdad.

    Devuelve la línea de confirmación que se persiste en
    `AgentState.confirmacion_publicacion`.
    """
    registro = {
        "id_reporte": uuid4().hex[:8],
        "publicado_en": datetime.now(timezone.utc).isoformat(),
        "solicitud": solicitud,
        "contenido": contenido,
        "aprobado_por": aprobado_por or "desconocido",
    }
    _REGISTRO_PUBLICACIONES.append(registro)

    confirmacion = (
        f"Reporte {registro['id_reporte']} publicado el {registro['publicado_en']} "
        f"(aprobado por: {registro['aprobado_por']})."
    )
    logger.info("%s Caracteres publicados: %d", confirmacion, len(contenido))
    return confirmacion
