from __future__ import annotations
import heapq
from typing import Dict, List, Tuple, Optional

from src.storage.state import State
from src.utils.log import setup_logger


class RoutingDijkstraStaticService:
    """
    Dijkstra estático a partir de topo.json:
    - No intercambia INFO.
    - Carga el grafo (no dirigido, costo=1 por arista).
    - Calcula next_hop por destino y lo instala en State.
    """

    def __init__(
        self,
        state: State,
        my_id: str,
        topo_config: Dict[str, List[str]],
        logger_name: Optional[str] = None,
    ) -> None:
        self.state = state
        self.my_id = my_id
        self.topo = dict(topo_config)
        self.log = setup_logger(logger_name or f"DIJK-{my_id}")

    async def start(self) -> None:
        routes = self._compute_routes() 

        routing_table = {dst: nh for dst, nh, cost in routes}  # dst -> next_hop
        last_costs    = {dst: cost for dst, nh, cost in routes} # dst -> costo

        await self.state.set_routing_table(routing_table)
        await self.state.set_last_costs(last_costs)

        self.log.info(f"Tabla Dijkstra (estática) instalada ({len(routes)} destinos)")

    async def stop(self) -> None:
        pass

    async def on_info(self, origin: str, payload: dict) -> None:
        # No usamos INFO en el modo estático
        return

    # ---- Dijkstra sencillo (costo 1 por arista) ----

    def _compute_routes(self) -> List[Tuple[str, str, float]]:
        graph: Dict[str, List[str]] = {u: list(vs) for u, vs in self.topo.items()}
        for u, vs in list(graph.items()):
            for v in vs:
                graph.setdefault(v, [])
                if u not in graph[v]:
                    graph[v].append(u)

        dist: Dict[str, float] = {self.my_id: 0.0}
        prev: Dict[str, Optional[str]] = {self.my_id: None}
        pq = [(0.0, self.my_id)]

        while pq:
            d, u = heapq.heappop(pq)
            if d > dist.get(u, float("inf")):
                continue
            for v in graph.get(u, []):
                nd = d + 1.0
                if nd < dist.get(v, float("inf")):
                    dist[v] = nd
                    prev[v] = u
                    heapq.heappush(pq, (nd, v))

        # Construir next_hop: para cada destino, subir por prev hasta vecino inmediato
        routes: List[Tuple[str, str, float]] = []
        for dst, d in dist.items():
            if dst == self.my_id:
                continue
            nh = self._first_hop(prev, dst)
            if nh:
                routes.append((dst, nh, d))
        return routes

    def _first_hop(self, prev: Dict[str, Optional[str]], dst: str) -> Optional[str]:
        # Subir desde dst hasta mi_id; el siguiente después de my_id es el next_hop
        chain = []
        cur = dst
        while cur is not None:
            chain.append(cur)
            cur = prev.get(cur)
        chain = list(reversed(chain))
        if len(chain) >= 2 and chain[0] == self.my_id:
            return chain[1]
        return None
