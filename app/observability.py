"""Capa de observabilidad: LangSmith vía variables de entorno.

Módulo deliberadamente delgado. LangChain/LangGraph instrumentan de forma
automática cuando `LANGCHAIN_TRACING_V2=true` y `LANGCHAIN_API_KEY` están en
el entorno: cada `ainvoke` del grafo, cada llamada al LLM y cada tool call se
reportan solos. Escribir callbacks o wrappers a mano acá sería duplicar esa
instrumentación y arriesgarse a trazas partidas.

Lo que sí hace falta es fallar temprano y ruidosamente si la configuración no
va a producir trazas, porque el síntoma natural (un dashboard vacío) recién se
descubre cuando ya se corrió la prueba de carga y se gastaron tokens.

Nota sobre nombres de variables: el SDK de LangSmith resuelve cada variable
buscando primero `LANGSMITH_*` y después `LANGCHAIN_*`
(`langsmith.utils.get_env_var`, con `namespaces=("LANGSMITH", "LANGCHAIN")`).
Este proyecto usa el prefijo `LANGCHAIN_`; si además hubiera un `LANGSMITH_*`
seteado en la máquina, ése gana.
"""
from __future__ import annotations

import logging

from app.config import settings

logger = logging.getLogger(__name__)


def verificar_observabilidad() -> None:
    """Valida la configuración de LangSmith al arrancar la app.

    Levanta `RuntimeError` si el tracing está pedido pero no puede funcionar.
    Si el tracing está apagado, avisa fuerte pero deja arrancar: la API sigue
    siendo utilizable sin observabilidad, sólo que no genera evidencia.
    """
    if not settings.langchain_tracing_v2:
        logger.warning(
            "LANGCHAIN_TRACING_V2 no está en 'true': la API va a funcionar pero "
            "NO se van a enviar trazas a LangSmith. Seteala en .env para poder "
            "capturar costo y latencia del dashboard."
        )
        return

    if not settings.langchain_api_key:
        raise RuntimeError(
            "LANGCHAIN_TRACING_V2=true pero falta LANGCHAIN_API_KEY. Generá una "
            "clave en https://smith.langchain.com/settings y completala en .env, "
            "o poné LANGCHAIN_TRACING_V2=false para arrancar sin observabilidad."
        )

    logger.info(
        "Observabilidad activa: trazas hacia LangSmith en el proyecto '%s'.",
        settings.langchain_project,
    )
