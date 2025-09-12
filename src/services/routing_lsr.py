from __future__ import annotations
import asyncio
from typing import Dict, Tuple, Optional, List
from dataclasses import dataclass
import time

from src.storage.state import State
from src.transport.redis_transport import RedisTransport
from src.protocol.builders import build_info
from src.utils.log import setup_logger


def _dijkstra_table_and_costs(graph: Dict[str, Dict[str, float]], src: str) -> tuple[Dict[str, str], Dict[str, float]]:
    import math
    dist = {n: math.inf for n in graph}
    prev = {n: None for n in graph}
    vis  = {n: False     for n in graph}
    dist[src] = 0.0

    for _ in range(len(graph)):
        u, best = None, math.inf
        for n in graph:
            if not vis[n] and dist[n] < best:
                u, best = n, dist[n]
        if u is None:
            break
        vis[u] = True
        for v, w in graph[u].items():
            if vis.get(v):
                continue
            alt = dist[u] + float(w)
            if alt < dist[v]:
                dist[v] = alt
                prev[v] = u

    next_hop: Dict[str, str] = {}
    for dst in graph:
        if dst == src or dist[dst] == math.inf:
            continue
        cur = dst
        while prev[cur] is not None and prev[cur] != src:
            cur = prev[cur]
        if prev[cur] == src:
            next_hop[dst] = cur
    return next_hop, dist


@dataclass
class LSRConfig:
    hello_timeout_sec: float = 20.0
    info_interval_sec: float = 12.0
    on_change_debounce_sec: float = 0.4
    advertise_links_from_neighbors_table: bool = True


class RoutingLSRService:
    """
    LSR:
      - Mantiene LSDB
      - Recalcula rutas (Dijkstra)
      - Emite INFO periódicamente y on-change
      - Expira vecinos por timeout de HELLO
      - Interoperabilidad: además del INFO clásico, publica mensajes
        compat por-par {type:'message', from, to, hops}
    """

    def __init__(
        self,
        state: State,
        transport: RedisTransport,
        my_id: str,
        neighbor_map: Dict[str, str],
        cfg: Optional[LSRConfig] = None,
        logger_name: Optional[str] = None,
    ) -> None:
        self.state = state
        self.transport = transport
        self.my_id = my_id
        self.neighbor_map = dict(neighbor_map)
        self.cfg = cfg or LSRConfig()
        self.log = setup_logger(logger_name or f"LSR-{my_id}")

        self._stopping = asyncio.Event()
        self._loop_task: Optional[asyncio.Task] = None
        self._ticker_task: Optional[asyncio.Task] = None
        self._debounce_task: Optional[asyncio.Task] = None

        self._last_advertised_view: Dict[str, float] = {}
        self._last_recalc_ts: float = 0.0

    # ---------- lifecycle ----------

    async def start(self) -> None:
        if self._loop_task and not self._loop_task.done():
            return
        self.log.info("RoutingLSRService iniciado")
        self._ticker_task = asyncio.create_task(self._periodic_info())
        self._loop_task = asyncio.create_task(self._watchdog())

    async def stop(self) -> None:
        self._stopping.set()
        for t in (self._ticker_task, self._loop_task, self._debounce_task):
            if t:
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass

    # ---------- integración con Forwarding ----------

    async def on_info(self, origin: str, view: Dict[str, float]) -> None:
        await self.state.update_lsdb(origin, view)
        self.log.debug(f"LSDB actualizado por INFO de {origin}: {view}")
        await self._debounced_recompute_and_advertise()

    async def maybe_mark_topology_changed(self) -> None:
        await self._debounced_recompute_and_advertise()

    # ---------- internos ----------

    async def _periodic_info(self) -> None:
        try:
            while not self._stopping.is_set():
                await asyncio.sleep(self.cfg.info_interval_sec)
                try:
                    await self._advertise_info()
                except Exception as e:
                    self.log.error(f"Error en periodic_info: {e}")
        except asyncio.CancelledError:
            return

    async def _watchdog(self) -> None:
        try:
            while not self._stopping.is_set():
                await asyncio.sleep(5.0)

                changed = False

                # 1) Vecinos directos sin HELLO → eliminar enlace local
                dead = await self.state.dead_neighbors(self.cfg.hello_timeout_sec)
                if dead:
                    snap = await self.state.get_lsdb_snapshot()
                    my_links = snap.get(self.my_id, {})
                    for n in dead:
                        if n in my_links:
                            await self.state.remove_neighbor(n)
                            self.log.warning(f"Retiro enlace {self.my_id}—{n} por timeout de HELLO")
                            changed = True

                # 2) LSPs viejas en la LSDB
                max_age = 3 * self.cfg.info_interval_sec
                stale = await self.state.purge_stale_lsdb(max_age)
                if stale:
                    self.log.warning(f"LSDB: expiro info de orígenes {stale}")
                    changed = True

                if changed:
                    await self._debounced_recompute_and_advertise()
        except asyncio.CancelledError:
            return

    async def _debounced_recompute_and_advertise(self) -> None:
        if self._debounce_task and not self._debounce_task.done():
            self._debounce_task.cancel()
            try:
                await self._debounce_task
            except asyncio.CancelledError:
                pass

        async def _job():
            await asyncio.sleep(self.cfg.on_change_debounce_sec)
            await self._recompute_routes()
            await self._advertise_info()

        self._debounce_task = asyncio.create_task(_job())

    async def _recompute_routes(self) -> None:
        graph = await self.state.build_graph(self.cfg.hello_timeout_sec)
        if self.my_id not in graph:
            graph[self.my_id] = {}
        table, costs = _dijkstra_table_and_costs(graph, self.my_id)
        await self.state.set_routing_table(table)
        self._last_recalc_ts = time.time()
        self.log.info(f"Tabla de ruteo actualizada ({len(table)} destinos)")
        await self.state.print_routing_table()

    async def _advertise_info(self) -> None:
        """
        INFO clásico (LSP) + compat por-par {type:'message', from, to, hops}.
        """
        if self.cfg.advertise_links_from_neighbors_table:
            view = await self.state.get_alive_links(self.cfg.hello_timeout_sec)
        else:
            routing = await self.state.get_routing_snapshot()
            view = {dst: 1.0 for dst in routing.keys()}

        # Evitar ruido si no cambió
        if view == self._last_advertised_view:
            return
        self._last_advertised_view = dict(view)

        # 1) INFO LSP
        pkt = build_info(self.my_id, view).to_publish_dict()
        channels = [self.neighbor_map[nid] for nid in self.neighbor_map.keys() if nid != self.my_id]
        if channels:
            await self.transport.broadcast(channels, pkt)
            self.log.debug(f"[LSR-INFO] anunciado: {view}")

        # 2) Compat: por cada enlace emite {type:'message', from, to, hops}
        for neigh, w in view.items():
            compat = {
                "proto": "lsr",
                "type": "message",
                "from": self.my_id,
                "to": neigh,
                "hops": float(w),
                "ttl": 8,
                "headers": [],
            }
            if channels:
                await self.transport.broadcast(channels, compat)
