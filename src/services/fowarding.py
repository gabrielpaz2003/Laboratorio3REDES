from __future__ import annotations
import asyncio
import contextlib
import json
from typing import Any, Dict, Callable, Awaitable, Optional, Iterable, Set

from src.protocol.schema import PacketFactory, HelloPacket, InfoPacket, UserMessagePacket, BasePacket
from src.storage.state import State
from src.transport.redis_transport import RedisTransport
from src.utils.log import setup_logger


class ForwardingService:
    """
    Servicio de forwarding (genérico para flooding/lsr/dvr/dijkstra):

      - Lee del canal propio (transport.read_loop)
      - Parsea, valida y aplica reglas por tipo
      - Reenvía según TTL, headers (anti-ciclo), routing_table y flooding controlado
      - Delega actualización de ruteo a on_info_async (LSR/DVR)

    Params clave:
      mode: "lsr" | "dvr" | "flooding" | "dijkstra"
      on_info_async: async fn(from_id: str, info_payload: dict) -> None (solo LSR/DVR)
    """

    def __init__(
        self,
        state: State,
        transport: RedisTransport,  # interfaz compatible (Redis/XMPP)
        my_id: str,
        neighbor_map: Dict[str, str],
        on_info_async: Optional[Callable[[str, Dict[str, Any]], Awaitable[None]]] = None,
        hello_timeout_sec: float = 20.0,
        mode: str = "lsr",
        logger_name: Optional[str] = None,
    ) -> None:
        self.state = state
        self.transport = transport
        self.my_id = my_id
        self.neighbor_map = dict(neighbor_map)  # id -> canal/jid
        self.on_info_async = on_info_async
        self.hello_timeout_sec = hello_timeout_sec
        self.mode = mode
        self.log = setup_logger(logger_name or f"FWD-{my_id}")

        # tarea principal
        self._runner_task: Optional[asyncio.Task] = None
        # control de apagado
        self._stopping = asyncio.Event()

    # ---------------- Lifecycle ----------------

    async def start(self) -> None:
        if self._runner_task and not self._runner_task.done():
            return
        self.log.info(f"ForwardingService iniciado (mode={self.mode})")
        self._runner_task = asyncio.create_task(self._run())
        asyncio.create_task(self._housekeeping())

    async def stop(self) -> None:
        self._stopping.set()
        if self._runner_task:
            self._runner_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._runner_task

    # ---------------- Internals ----------------

    async def _run(self) -> None:
        async for raw in self.transport.read_loop():
            if self._stopping.is_set():
                break

            try:
                data = json.loads(raw)
            except Exception:
                self.log.warning(f"Descartado (JSON inválido): {raw[:120]}…")
                continue

            try:
                pkt = PacketFactory.parse_obj(data)
            except Exception as e:
                self.log.warning(f"Descartado (schema inválido): {e} - raw={data}")
                continue

            await self._handle_packet(pkt)

    async def _housekeeping(self) -> None:
        try:
            while not self._stopping.is_set():
                await asyncio.sleep(5.0)
                self.state.purge_seen()

                # vecinos “muertos” por inactividad de HELLO
                dead = await self.state.dead_neighbors(self.hello_timeout_sec)
                for n in dead:
                    self.log.warning(f"Vecino sin HELLO: {n} (posible caída)")
        except asyncio.CancelledError:
            pass

    # ---------------- Dispatch por tipo ----------------

    async def _handle_packet(self, pkt: BasePacket) -> None:
        # de-dupe por msg_id
        if pkt.msg_id and self.state.is_seen(pkt.msg_id):
            self.log.debug(f"VISTO (de-dupe) {pkt.type} id={pkt.msg_id}")
            return
        if pkt.msg_id:
            self.state.mark_seen(pkt.msg_id)

        # anti-ciclo
        if pkt.seen_cycle(self.my_id):
            self.log.debug(f"CICLO detectado: {pkt.type} trace={pkt.trace_id}")
            return

        # TTL
        if pkt.ttl <= 0 and pkt.type in ("info", "message"):
            self.log.debug(f"TTL=0 descartado: {pkt.type} id={pkt.msg_id}")
            return

        # derivar
        if isinstance(pkt, HelloPacket):
            await self._on_hello(pkt)
        elif isinstance(pkt, InfoPacket):
            await self._on_info(pkt)
        elif isinstance(pkt, UserMessagePacket):
            await self._on_message(pkt)
        else:
            self.log.debug(f"Tipo no manejado: {pkt.type}")

    # ---------------- Handlers ----------------

    async def _on_hello(self, pkt: HelloPacket) -> None:
        await self.state.touch_hello(pkt.from_)
        self.log.info(f"[HELLO] de {pkt.from_} (trace={pkt.trace_id})")

    async def _on_info(self, pkt: InfoPacket) -> None:
        # Solo si hay callback (LSR/DVR); flood de control en otros modos se ignora
        if self.on_info_async is None:
            self.log.debug("INFO recibido pero sin servicio de ruteo (ignorado)")
            return

        origin = pkt.from_
        try:
            await self.on_info_async(origin, pkt.payload)
        except Exception as e:
            self.log.error(f"Error en on_info_async: {e}")

        # Retransmitir a vecinos (excepto prev_hop)
        pkt_out = pkt.with_decremented_ttl().with_appended_hop(self.my_id)
        if pkt_out.ttl <= 0:
            return
        prev_hop: Optional[str] = pkt.headers[-1] if pkt.headers else None
        await self._broadcast_to_neighbors(pkt_out, exclude={prev_hop} if prev_hop else None)
        self.log.debug(f"[INFO] retransmitido trace={pkt.trace_id} ttl={pkt_out.ttl}")

    async def _on_message(self, pkt: UserMessagePacket) -> None:
        dst = pkt.to
        if dst == self.my_id:
            self._deliver(pkt)
            return

        # Flooding puro → siempre flooding
        if self.mode == "flooding":
            prev_hop: Optional[str] = pkt.headers[-1] if pkt.headers else None
            pkt_out = pkt.with_decremented_ttl().with_appended_hop(self.my_id)
            if pkt_out.ttl > 0:
                await self._broadcast_to_neighbors(pkt_out, exclude={prev_hop} if prev_hop else None)
                self.log.info(f"⇉ {pkt.from_} → {dst} (flooding) trace={pkt.trace_id}")
            return

        # Para lsr/dvr/dijkstra: intentar ruteo, fallback a flooding
        next_hop = await self.state.get_next_hop(dst)
        if next_hop:
            ch = self.neighbor_map.get(next_hop)
            if ch:
                pkt_out = pkt.with_decremented_ttl().with_appended_hop(self.my_id)
                if pkt_out.ttl > 0:
                    await self.transport.publish_json(ch, pkt_out.to_publish_dict())
                    self.log.info(f"[MSG] {pkt.from_}→{dst} via {next_hop} trace={pkt.trace_id}")
                    return

        # Sin ruta → flooding controlado
        prev_hop: Optional[str] = pkt.headers[-1] if pkt.headers else None
        pkt_out = pkt.with_decremented_ttl().with_appended_hop(self.my_id)
        if pkt_out.ttl > 0:
            await self._broadcast_to_neighbors(pkt_out, exclude={prev_hop} if prev_hop else None)
            self.log.info(f"[MSG-FLOOD] {pkt.from_}→{dst} (sin ruta) trace={pkt.trace_id}")

    # ---------------- Helpers ----------------

    async def _broadcast_to_neighbors(self, pkt: BasePacket, exclude: Optional[Set[str]] = None) -> None:
        exclude = exclude or set()
        targets = [nid for nid in self.neighbor_map.keys() if nid not in exclude and nid != self.my_id]
        channels = [self.neighbor_map[nid] for nid in targets]
        if channels:
            await self.transport.broadcast(channels, pkt.to_publish_dict())

    def _deliver(self, pkt: UserMessagePacket) -> None:
        body_preview = pkt.payload if isinstance(pkt.payload, str) else json.dumps(pkt.payload, ensure_ascii=False)
        self.log.info(f"[DELIVERED] {pkt.from_} → {self.my_id} :: {body_preview[:200]}")
