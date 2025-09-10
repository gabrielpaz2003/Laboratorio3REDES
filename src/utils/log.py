from __future__ import annotations
import logging
from typing import Optional

from rich.console import Console
from rich.theme import Theme
from rich.logging import RichHandler

# Tema central de la app
_APP_THEME = Theme({
    "time": "dim",
    "level.debug": "cyan",
    "level.info": "bold green",
    "level.warning": "bold yellow",
    "level.error": "bold red",
    "service": "bold magenta",
    "node": "bold white",
    "proto": "bold blue",
})

# Consola única para toda la app
_console: Optional[Console] = None


def get_console() -> Console:
    global _console
    if _console is None:
        _console = Console(theme=_APP_THEME, highlight=False, soft_wrap=False)
    return _console


def setup_logger(name: str, level: str = "INFO") -> logging.Logger:
    """
    Logger con RichHandler (colores, tiempos, trazas bonitas).
    """
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger  # ya configurado

    lvl = getattr(logging, level.upper(), logging.INFO)
    logger.setLevel(lvl)

    handler = RichHandler(
        console=get_console(),
        show_time=True,
        show_level=True,
        show_path=False,
        rich_tracebacks=True,
        markup=True,
        log_time_format="[%H:%M:%S]",
    )
    # El mensaje principal lo muestra Rich; el Formatter solo deja el texto limpio
    handler.setFormatter(logging.Formatter("%(message)s"))

    logger.addHandler(handler)
    logger.propagate = False
    return logger
