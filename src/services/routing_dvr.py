from __future__ import annotations
import asyncio
import time
from typing import Dict, Tuple, Optional

from src.storage.state import State
from src.transport.redis_transport import RedisTransport  # usamos solo tipo; puede ser XmppTransport también
from src.protocol.builders import build_info
from src.utils.log import setup_logger


INF = 1e9


class DVRConfig:
    def __init__(
        self,
        advertise_interval_sec: float = 5.0,
        entry_timeout_sec: float = 30.0,
        split_horizon_poison: bool = True,
    ) -> None:
        self.advertise_interval_sec = advertise_interval_sec
        self.entry_timeout_sec = entry_timeout_sec
        self.split_horizon_poison = split_horizon_poison


class RoutingDVRService:
    """
    Distance Vector Routing con Bellman-Ford incremental:

    - DV local: dest -> (cost, next_hop)
    - Vecinos directos cost=1
    - Recibe INFO de vecinos con su vector de distancias
    - Aplica Bellman-Ford: dist(u,v) = min(dist(u,n) + dist(n,v))
    - Split-horizon con poison reverse opcional
    - Expira entradas con timeout si dejan de anunciarse
    """

    def __init__(
        self,
        state: State,
        transport: RedisTransport,  # interfaz con publish/broadcast/read_loop
        my_id: str,
        neighbor_map: Dict[str, str],
        cfg: Optional[DVRConfig] = None,
        logger_name: Optional[str] = None,
    ) -> None:
        self.state = state
        self.transport = transport
        self.my_id = my_id
        self.neighbor_map = dict(neighbor_map)
        self.cfg = cfg or DVRConfig()
        self.log = setup_logger(logger_name or f"DVR-{my_id}")

        # DV: dest -> (cost, next_hop)
        self.dv: Dict[str, Tuple[float, Optional[str]]] = {}
        # Última vez que vimos info de cada origen (para expirar)
        self.last_seen_from: Dict[str, float] = {}

        self._task_adv: Optional[asyncio.Task] = None
        self._stopping = asyncio.Event()

    async def start(self) -> None:
        # Inicializar DV con vecinos directos
        neighbors = await self.state.get_neighbors()
        for n, cost in neighbors:
            self.dv[n] = (cost, n)
            self.last_seen_from.setdefault(n, time.time())

        # Yo mismo a costo 0
        self.dv[self.my_id] = (0.0, None)

        # Publicar primer vector
        await self._advertise_all()

        # Tarea periódica
        self._task_adv = asyncio.create_task(self._periodic())

        self.log.info("RoutingDVRService iniciado")

    async def stop(self) -> None:
        self._stopping.set()
        if self._task_adv:
            self._task_adv.cancel()
            try:
                await self._task_adv
            except asyncio.CancelledError:
                pass

    # ---- Integración con Forwarding ----

    async def on_info(self, origin: str, payload: dict) -> None:
        """
        Recibe el DV de 'origin': payload = {"dv": {"X": costo, ...}}
        """
        if not isinstance(payload, dict) or "dv" not in payload:
            return

        now = time.time()
        self.last_seen_from[origin] = now

        # El costo a 'origin' (vecino) lo tomamos de tabla de vecinos
        neigh_cost = await self._cost_to_neighbor(origin)
        if neigh_cost is None:
            # INFO que llegó por flooding de alguien no vecino: se ignora en DVR
            return

        changed = False
        dv_neighbor: Dict[str, float] = payload.get("dv", {})
        for dest, cost_via_origin in dv_neighbor.items():
            if dest == self.my_id:
                continue

            new_cost = neigh_cost + cost_via_origin
            old = self.dv.get(dest, (INF, None))
            if new_cost < old[0] - 1e-9:
                self.dv[dest] = (new_cost, origin)
                changed = True

            # Si el next_hop actual es origin y ahora origin "poison"ó (INF),
            # subimos a INF, esperando nueva mejor ruta en próximas iteraciones.
            if old[1] == origin and cost_via_origin >= INF:
                self.dv[dest] = (INF, None)
                changed = True

        if changed:
            await self._install_into_state()
            await self._advertise_all()

    # ---- Internos ----

    async def _periodic(self) -> None:
        try:
            while not self._stopping.is_set():
                await asyncio.sleep(self.cfg.advertise_interval_sec)
                # Expirar entradas antiguas
                self._expire_old()
                # Re-anunciar
                await self._advertise_all()
        except asyncio.CancelledError:
            pass

    def _expire_old(self) -> None:
        now = time.time()
        expired_origins = []
        for origin, ts in list(self.last_seen_from.items()):
            if now - ts > self.cfg.entry_timeout_sec:
                expired_origins.append(origin)

        if expired_origins:
            for dest, (cost, nh) in list(self.dv.items()):
                if nh in expired_origins:
                    self.dv[dest] = (INF, None)

            for o in expired_origins:
                del self.last_seen_from[o]

            self.log.warning(f"DV: expiran anuncios de {expired_origins}")

    async def _advertise_all(self) -> None:
        """
        Anuncia a cada vecino nuestro DV; con split-horizon + poison reverse opcional.
        """
        dv_base = {d: c for d, (c, _) in self.dv.items()}
        # yo a 0
        dv_base[self.my_id] = 0.0

        for neigh in self.neighbor_map.keys():
            out = dict(dv_base)
            if self.cfg.split_horizon_poison:
                # Envenenar destinos que usan 'neigh' como next_hop
                for dest, (_, nh) in self.dv.items():
                    if nh == neigh:
                        out[dest] = INF
            info = build_info(self.my_id, {"dv": out}).to_publish_dict()
            ch = self.neighbor_map[neigh]
            await self.transport.publish_json(ch, info)

    async def _install_into_state(self) -> None:
        """
        Instala la tabla DV como routing_table en State.
        """
        entries = []
        for dest, (cost, nh) in self.dv.items():
            if dest == self.my_id:
                continue
            if cost < INF and nh is not None:
                entries.append((dest, nh, float(cost)))
        await self.state.set_routes(entries)
        self.log.info(f"Tabla DVR actualizada ({len(entries)} destinos)")

    async def _cost_to_neighbor(self, neigh: str) -> Optional[float]:
        for n, c in await self.state.get_neighbors():
            if n == neigh:
                return c
        return None
