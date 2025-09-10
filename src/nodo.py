from __future__ import annotations
import os
import json
import asyncio
from pathlib import Path
from typing import Dict, List, Tuple, Any, Optional

from dotenv import load_dotenv

from src.storage.state import State
from src.transport.redis_transport import RedisTransport, RedisSettings
from src.transport.xmpp_transport import XmppTransport
from src.services.fowarding import ForwardingService
from src.services.routing_lsr import RoutingLSRService, LSRConfig
from src.services.routing_dvr import RoutingDVRService, DVRConfig
from src.services.routing_dijkstra_static import RoutingDijkstraStaticService
from src.protocol.builders import build_hello, build_info, build_message
from src.utils.log import setup_logger


def _load_json(path: str) -> Dict[str, Any]:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"No existe el archivo: {path}")
    with p.open("r", encoding="utf-8") as f:
        return json.load(f)


class Node:
    """
    Nodo con transporte Redis/XMPP y servicios de Forwarding + (LSR|DVR|Dijkstra|Flooding).
    """

    def __init__(self, env_path: Optional[str] = None) -> None:
        if env_path:
            load_dotenv(env_path)
        else:
            load_dotenv()

        # ── Env ──────────────────────────────────────────────────────────────
        self.section = os.getenv("SECTION", "sec10")
        self.topo_id = os.getenv("TOPO", "topo1")
        self.my_id = os.getenv("NODE", "A")
        self.names_path = os.getenv("NAMES_PATH", "./configs/names.json")
        self.topo_path = os.getenv("TOPO_PATH", "./configs/topo.json")
        self.hello_interval = float(os.getenv("HELLO_INTERVAL_SEC", "5"))
        self.info_interval = float(os.getenv("INFO_INTERVAL_SEC", "12"))
        self.hello_timeout = float(os.getenv("HELLO_TIMEOUT_SEC", "20"))
        self.log_level = os.getenv("LOG_LEVEL", "INFO")
        self.proto = os.getenv("PROTO", "lsr").lower()  # lsr|dvr|dijkstra|flooding
        self.transport_kind = os.getenv("TRANSPORT", "redis").lower()  # redis|xmpp

        # ── Redis settings ───────────────────────────────────────────────────
        self.redis_settings = RedisSettings(
            host=os.getenv("REDIS_HOST", "localhost"),
            port=int(os.getenv("REDIS_PORT", "6379")),
            password=os.getenv("REDIS_PWD", None),
            db=0,
            decode_responses=True,
        )

        # ── XMPP settings ────────────────────────────────────────────────────
        self.xmpp_jid = os.getenv("XMPP_JID", "")
        self.xmpp_pwd = os.getenv("XMPP_PWD", "")
        self.xmpp_host = os.getenv("XMPP_HOST", None)
        self.xmpp_port = int(os.getenv("XMPP_PORT", "0")) or None

        # ── Logger ───────────────────────────────────────────────────────────
        self.log = setup_logger(f"NODE-{self.my_id}", self.log_level)

        # ── State y servicios ────────────────────────────────────────────────
        self.state: Optional[State] = None
        self.transport: Optional[object] = None  # RedisTransport | XmppTransport
        self.forwarding: Optional[ForwardingService] = None

        # Routing services opcionales
        self.lsr: Optional[RoutingLSRService] = None
        self.dvr: Optional[RoutingDVRService] = None
        self.dijk: Optional[RoutingDijkstraStaticService] = None

        # ── Configs cargadas ─────────────────────────────────────────────────
        self.names_cfg: Dict[str, str] = {}          # node_id -> canal/JID
        self.topo_cfg: Dict[str, Any] = {}           # node_id -> [neighbors] | {neighbor: weight}
        self.neighbor_ids: List[str] = []
        self.neighbor_map: Dict[str, str] = {}       # neighbor_id -> canal/JID
        self.neighbor_weights: Dict[str, float] = {} # neighbor_id -> weight

        # ── Tasks locales ───────────────────────────────────────────────────
        self._hello_task: Optional[asyncio.Task] = None

    # ─────────────────────────────────────────────────────────────────────────

    def _my_channel(self) -> str:
        ch = self.names_cfg.get(self.my_id)
        if ch:
            return ch
        return f"{self.section}.{self.topo_id}.{self.my_id}"

    @staticmethod
    def _normalize_neighbor_weights(raw: Any) -> Dict[str, float]:
        """Acepta lista (peso 1.0) o dict {vecino:peso} (>0)."""
        weights: Dict[str, float] = {}
        if isinstance(raw, dict):
            for k, v in raw.items():
                w = float(v)
                if w <= 0:
                    raise ValueError(f"Peso inválido (<=0) para '{k}': {w}")
                weights[k] = w
        elif isinstance(raw, (list, tuple)):
            for k in raw:
                weights[str(k)] = 1.0
        else:
            raise ValueError("topo.json inválido: cada entrada debe ser lista o dict de pesos")
        return weights

    def _load_configs(self) -> None:
        names = _load_json(self.names_path)
        topo = _load_json(self.topo_path)

        if names.get("type") != "names" or "config" not in names:
            raise ValueError("names.json inválido: falta {type:'names', config:{...}}")
        if topo.get("type") != "topo" or "config" not in topo:
            raise ValueError("topo.json inválido: falta {type:'topo', config:{...}}")

        self.names_cfg = dict(names["config"])
        self.topo_cfg = dict(topo["config"])

        raw_neighbors = self.topo_cfg.get(self.my_id, [])
        self.neighbor_weights = self._normalize_neighbor_weights(raw_neighbors)
        self.neighbor_ids = list(self.neighbor_weights.keys())

        self.neighbor_map = {
            nid: self.names_cfg[nid]
            for nid in self.neighbor_ids
            if nid in self.names_cfg
        }

        if not self.neighbor_map:
            self.log.warning("Este nodo no tiene vecinos mapeados en names.json/topo.json")

        self.log.info(f"Vecinos de {self.my_id}: {self.neighbor_ids}")
        self.log.info(f"Pesos de enlaces: {self.neighbor_weights}")
        self.log.info(f"Canal propio: {self._my_channel()}")
        self.log.info(f"PROTO en uso: {self.proto}")
        self.log.info(f"TRANSPORT en uso: {self.transport_kind}")

    async def _bootstrap_services(self) -> None:
        self.state = State(node_id=self.my_id)
        await self.state.set_neighbors(list(self.neighbor_weights.items()))

        if self.transport_kind == "xmpp":
            self.transport = XmppTransport(
                jid=self.xmpp_jid,
                password=self.xmpp_pwd,
                my_channel=self._my_channel(),
                host=self.xmpp_host,
                port=self.xmpp_port,
                logger_name=self.my_id,
            )
        else:
            self.transport = RedisTransport(
                self.redis_settings,
                my_channel=self._my_channel(),
                logger_name=self.my_id
            )

        await self.transport.connect()

        on_info_cb = None

        if self.proto == "lsr":
            self.lsr = RoutingLSRService(
                state=self.state,
                transport=self.transport,
                my_id=self.my_id,
                neighbor_map=self.neighbor_map,
                cfg=LSRConfig(
                    hello_timeout_sec=self.hello_timeout,
                    info_interval_sec=self.info_interval,
                    on_change_debounce_sec=0.4,
                    advertise_links_from_neighbors_table=True,
                ),
                logger_name=f"LSR-{self.my_id}",
            )
            await self.lsr.start()
            on_info_cb = self.lsr.on_info

        elif self.proto == "dvr":
            self.dvr = RoutingDVRService(
                state=self.state,
                transport=self.transport,
                my_id=self.my_id,
                neighbor_map=self.neighbor_map,
                cfg=DVRConfig(
                    advertise_interval_sec=self.info_interval,
                    entry_timeout_sec=max(self.hello_timeout, 25.0),
                    split_horizon_poison=True,
                ),
                logger_name=f"DVR-{self.my_id}",
            )
            await self.dvr.start()
            on_info_cb = self.dvr.on_info

        elif self.proto == "dijkstra":
            topo_neighbors_only: Dict[str, List[str]] = {}
            for nid, raw in self.topo_cfg.items():
                if isinstance(raw, dict):
                    topo_neighbors_only[nid] = list(raw.keys())
                elif isinstance(raw, (list, tuple)):
                    topo_neighbors_only[nid] = list(map(str, raw))
                else:
                    topo_neighbors_only[nid] = []
            self.dijk = RoutingDijkstraStaticService(
                state=self.state,
                my_id=self.my_id,
                topo_config=topo_neighbors_only,
                logger_name=f"DIJK-{self.my_id}",
            )
            await self.dijk.start()
            on_info_cb = None

        elif self.proto == "flooding":
            on_info_cb = None

        else:
            raise ValueError(f"PROTO desconocido: {self.proto}")

        self.forwarding = ForwardingService(
            state=self.state,
            transport=self.transport,
            my_id=self.my_id,
            neighbor_map=self.neighbor_map,
            on_info_async=on_info_cb,
            hello_timeout_sec=self.hello_timeout,
            mode=self.proto,
            logger_name=f"FWD-{self.my_id}",
        )
        await self.forwarding.start()

        await self._emit_initial_control_packets()
        self._hello_task = asyncio.create_task(self._periodic_hello())

    async def _emit_initial_control_packets(self) -> None:
        assert self.transport is not None
        hello = build_hello(self.my_id).to_publish_dict()
        await self.transport.broadcast(self.neighbor_map.values(), hello)

        if self.proto == "lsr":
            info = build_info(self.my_id, dict(self.neighbor_weights)).to_publish_dict()
            await self.transport.broadcast(self.neighbor_map.values(), info)
        elif self.proto == "dvr":
            pass

        self.log.info("Paquetes iniciales enviados")

    async def _periodic_hello(self) -> None:
        assert self.transport is not None
        try:
            while True:
                await asyncio.sleep(self.hello_interval)
                pkt = build_hello(self.my_id).to_publish_dict()
                await self.transport.broadcast(self.neighbor_map.values(), pkt)
        except asyncio.CancelledError:
            return

    # ─────────────────────────────────────────────────────────────────────────
    # API pública
    # ─────────────────────────────────────────────────────────────────────────

    async def start(self) -> None:
        self._load_configs()
        await self._bootstrap_services()
        self.log.info(f"Nodo {self.my_id} iniciado.")

    async def stop(self) -> None:
        if self._hello_task:
            self._hello_task.cancel()
            try:
                await self._hello_task
            except asyncio.CancelledError:
                pass

        if self.lsr:
            await self.lsr.stop()
        if self.dvr:
            await self.dvr.stop()
        if self.dijk:
            await self.dijk.stop()

        if self.forwarding:
            await self.forwarding.stop()

        if self.transport:
            await self.transport.close()

        self.log.info(f"Nodo {self.my_id} detenido.")

    async def send_message(self, dst: str, body: Any = "hola") -> None:
        """Envía mensaje:
           1) si dst es vecino directo -> unicast directo
           2) si hay ruta en tabla -> unicast via next_hop
           3) si no hay ruta -> flooding a vecinos
        """
        assert self.transport is not None and self.state is not None

        pkt = build_message(self.my_id, dst, body).to_publish_dict()

        # 1) Envío directo si es vecino inmediato
        if dst in self.neighbor_map:
            await self.transport.publish_json(self.neighbor_map[dst], pkt)
            self.log.info(f"[CLI] MESSAGE {self.my_id}→{dst} via {dst} (direct)")
            return

        # 2) Intentar ruta conocida (LSR/DVR/Dijkstra)
        next_hop = await self.state.get_next_hop(dst)
        if next_hop:
            ch = self.neighbor_map.get(next_hop)
            if ch:
                await self.transport.publish_json(ch, pkt)
                self.log.info(f"[CLI] MESSAGE {self.my_id}→{dst} via {next_hop}")
                return

        # 3) Flooding controlado
        await self.transport.broadcast(self.neighbor_map.values(), pkt)
        self.log.info(f"[CLI] MESSAGE {self.my_id}→{dst}")
