"""Configuración centralizada del orquestador y de la API.

Único punto de lectura de variables de entorno. El resto de los módulos
importan `settings`, nunca leen `os.environ` directamente: esto evita que
la configuración quede dispersa y facilita testear con valores mockeados.

El `load_dotenv()` de este módulo es también lo que hace que la
instrumentación de LangSmith funcione: LangChain lee `LANGCHAIN_TRACING_V2`,
`LANGCHAIN_API_KEY` y `LANGCHAIN_PROJECT` directo de `os.environ`, y todos los
módulos de la app importan `app.config`, así que el `.env` ya está cargado
antes de la primera traza.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()

_VALORES_VERDADEROS = {"true", "1", "yes", "y", "on"}


@dataclass(frozen=True)
class Settings:
    """Contenedor inmutable de configuración validada al arranque."""

    anthropic_api_key: str
    tavily_api_key: str
    anthropic_model: str
    max_supervisor_steps: int
    recursion_limit: int
    tavily_max_results: int
    redis_url: str
    langchain_tracing_v2: bool
    langchain_api_key: str
    langchain_project: str


def _leer_bool(nombre: str, default: str = "false") -> bool:
    return os.environ.get(nombre, default).strip().lower() in _VALORES_VERDADEROS


def _load_settings() -> Settings:
    anthropic_api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not anthropic_api_key:
        raise RuntimeError(
            "Falta ANTHROPIC_API_KEY en el entorno. Copiá .env.example a .env "
            "y completá tu clave antes de ejecutar el orquestador."
        )

    tavily_api_key = os.environ.get("TAVILY_API_KEY")
    if not tavily_api_key:
        raise RuntimeError(
            "Falta TAVILY_API_KEY en el entorno. El Agente Investigador depende "
            "de una búsqueda real vía Tavily: generá una clave gratuita en "
            "https://app.tavily.com/home, copiá .env.example a .env y completala."
        )

    # Las variables de LangSmith se leen sin validar acá a propósito: quién
    # decide si la combinación es utilizable es `observability.py`, para que
    # el mensaje de error hable de observabilidad y no de configuración.
    return Settings(
        anthropic_api_key=anthropic_api_key,
        tavily_api_key=tavily_api_key,
        anthropic_model=os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5"),
        max_supervisor_steps=int(os.environ.get("MAX_SUPERVISOR_STEPS", "6")),
        recursion_limit=int(os.environ.get("RECURSION_LIMIT", "15")),
        tavily_max_results=int(os.environ.get("TAVILY_MAX_RESULTS", "5")),
        redis_url=os.environ.get("REDIS_URL", "redis://localhost:6379"),
        langchain_tracing_v2=_leer_bool("LANGCHAIN_TRACING_V2"),
        langchain_api_key=os.environ.get("LANGCHAIN_API_KEY", ""),
        langchain_project=os.environ.get("LANGCHAIN_PROJECT", "entrega-7-orquestador"),
    )


settings = _load_settings()
