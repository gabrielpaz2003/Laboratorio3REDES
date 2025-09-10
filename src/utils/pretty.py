from __future__ import annotations
from typing import Dict, Any, Optional
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.rule import Rule
from rich.text import Text

from src.utils.log import get_console


def banner(title: str, subtitle: Optional[str] = None, style: str = "service") -> None:
    """
    Muestra un banner bonito con título/subtítulo.
    """
    console: Console = get_console()
    body = f"[{style}]{title}[/]"
    if subtitle:
        body += f"\n[dim]{subtitle}[/]"
    console.print(Panel(body, border_style=style, expand=False))


def rule(text: str, style: str = "proto") -> None:
    console: Console = get_console()
    console.print(Rule(Text(text, style=style)))


def render_routing_table(node_id: str, proto: str, table: Dict[str, Dict[str, Any]]) -> None:
    """
    Renderiza la tabla de ruteo con Rich.
    Espera: table = { dst: {"next_hop": str, "cost": float|inf}, ... }
    """
    console: Console = get_console()
    title = f"Tabla de ruteo de [node]{node_id}[/node] ([proto]{proto.upper()}[/proto])"
    t = Table(title=title, title_style="bold white", header_style="bold", show_lines=False, expand=False)
    t.add_column("Destino", style="bold white")
    t.add_column("NextHop", style="white")
    t.add_column("Costo", justify="right")

    if not table:
        t.add_row("—", "—", "—")
    else:
        import math
        for dst in sorted(table.keys()):
            nh = table[dst].get("next_hop", "-") or "-"
            c = table[dst].get("cost", math.inf)
            cost_str = f"{c:.4f}" if c != math.inf else "[bold red]inf[/]"
            t.add_row(dst, nh, cost_str)

    console.print(t)
