from __future__ import annotations
import asyncio
from pathlib import Path
from typing import Optional

import typer
from rich import print
from rich.panel import Panel
from rich.table import Table

from src.nodo import Node
from src.storage.state import State

app = typer.Typer(add_completion=False, help="Redes - Laboratorio 3: Algoritmos de Enrutamiento")


def _banner(node_id: str, channel: str, neighbors, proto: str, transport: str) -> None:
    body = f"[bold]Nodo {node_id}[/bold]\nCanal: {channel}\nVecinos: {neighbors}\nPROTO: {proto.upper()}\nTRANSPORT: {transport.upper()}"
    print(Panel(body, expand=False))


async def _run_node(env: Optional[str], show_table: bool, wait: float, send_to: Optional[str], body: Optional[str]) -> None:
    node = Node(env)
    await node.start()

    # banner
    _banner(node.my_id, node._my_channel(), node.neighbor_ids, node.proto, node.transport_kind)
    print(Panel.fit(f"[green]Nodo {node.my_id} iniciado.[/green]", title="Listo"))

    if send_to:
        await node.send_message(send_to, body or "hola")

    # opcional: mostrar tabla periódicamente por 'wait' segundos
    if show_table:
        async def _print_table():
            for _ in range(int(wait) if wait > 0 else 1):
                await asyncio.sleep(1.0)
                await _show_rib(node.state, node.my_id, node.proto)
        await _print_table()
    else:
        if wait > 0:
            await asyncio.sleep(wait)

    await node.stop()


async def _show_rib(state: State, my_id: str, proto: str) -> None:
    tbl = Table(title=f"Tabla de ruteo de {my_id} ({proto.upper()})", show_lines=True)
    tbl.add_column("Destino")
    tbl.add_column("NextHop")
    tbl.add_column("Costo", justify="right")
    routes = await state.dump_routes()
    if not routes:
        tbl.add_row("—", "—", "—")
    else:
        for dst in sorted(routes.keys()):
            nh = routes[dst]["next_hop"]
            cost = routes[dst]["cost"]
            tbl.add_row(dst, nh, f"{cost:.4f}")
        print(tbl)


@app.command()
def run(
    env: Optional[Path] = typer.Option(None, "--env", help="Ruta a archivo .env"),
    show_table: bool = typer.Option(False, "--show-table", help="Mostrar tabla de ruteo"),
    wait: float = typer.Option(0, "--wait", help="Segundos a esperar antes de salir (0 = espera stdin/ctrl+c)"),
    send: Optional[str] = typer.Option(None, "--send", help="Enviar mensaje a destino (id de nodo)"),
    body: Optional[str] = typer.Option(None, "--body", help="Payload del mensaje de usuario"),
):
    """
    Arranca un nodo con la configuración del .env, opcionalmente envía un mensaje, y muestra la tabla de ruteo.
    """
    try:
        asyncio.run(_run_node(str(env) if env else None, show_table, wait, send, body))
    except KeyboardInterrupt:
        # un shutdown limpio
        pass


if __name__ == "__main__":
    app()
