from __future__ import annotations
import asyncio
import contextlib
import json
from typing import Any, Dict, Callable, Awaitable, Optional, Set

from src.protocol.schema import (
    PacketFactory, HelloPacket, InfoPacket, UserMessagePacket, BasePacket
)
from src.storage.state import State
from src.transport.redis_transport import RedisTransport
from src.utils.log import setup_logger


class ForwardingService:
    """
    Forwarding (flooding/lsr/dvr/dijkstra) + COMPAT:
      - Acepta mensajes de otros grupos:
        * HELLO con 'to' distinto de broadcast → se fuerza a 'broadcast'
        * message {from,to,hops} → se traduce a INFO (LSP) {to: hops}
      - De-dupe por msg_id, anti-ciclo (headers), TTL decreciente.
    """

    def __init__(
        self,
        state: State,
        transport: RedisTransport,
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
        self.neighbor_by_channel: Dict[str, str] = {ch: nid for nid, ch in self.neighbor_map.items()}

        self.on_info_async = on_info_async
        self.hello_timeout_sec = hello_timeout_sec
        self.mode = mode
        self.log = setup_logger(logger_name or f"FWD-{my_id}")

        self._runner_task: Optional[asyncio.Task] = None
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

    def _coerce_compat(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Compat con otros grupos:
          - HELLO con 'to' != broadcast -> forzar broadcast
          - message {from,to,hops} -> traducir a INFO (LSP) {'to': hops}
          - Normalizar 'from'/'to' si vienen como canal (solo vecinos)
        """
        t = str(data.get("type", "")).lower()

        # Normalizar IDs si enviaron el canal
        frm = data.get("from")
        if isinstance(frm, str) and frm in self.neighbor_by_channel:
            data["from"] = self.neighbor_by_channel[frm]
        to = data.get("to")
        if isinstance(to, str) and to in self.neighbor_by_channel:
            data["to"] = self.neighbor_by_channel[to]

        # HELLO: forzar broadcast y limpiar ruido
        if t == "hello":
            if data.get("to") != "broadcast":
                data["to"] = "broadcast"
            if not isinstance(data.get("headers"), (list, dict)):
                data["headers"] = []
            if "payload" in data and not isinstance(data["payload"], (str, dict)):
                data["payload"] = "hello"
            return data

        # message {from,to,hops} -> INFO (LSP) de 'from'
        if t == "message":
            hops = data.get("hops", None)
            if isinstance(hops, (int, float)) and isinstance(data.get("to"), str) and isinstance(data.get("from"), str):
                origin = data["from"]
                neigh = data["to"]
                coerced = {
                    "proto": data.get("proto", "lsr"),
                    "type": "info",
                    "from": origin,
                    "to": "broadcast",
                    "ttl": int(data.get("ttl", 8)),
                    "headers": data.get("headers", []),
                    "payload": {str(neigh): float(hops)},
                }
                if "msg_id" in data:
                    coerced["msg_id"] = data["msg_id"]
                if "trace_id" in data:
                    coerced["trace_id"] = data["trace_id"]
                return coerced

        return data

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
                data = self._coerce_compat(data)
            except Exception as e:
                self.log.debug(f"Compat coercion falló: {e}; raw={data}")

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
        if pkt.msg_id and self.state.is_seen(pkt.msg_id):
            self.log.debug(f"VISTO (de-dupe) {pkt.type} id={pkt.msg_id}")
            return
        if pkt.msg_id:
            self.state.mark_seen(pkt.msg_id)

        if pkt.seen_cycle(self.my_id):
            self.log.debug(f"CICLO detectado: {pkt.type} trace={pkt.trace_id}")
            return

        if pkt.ttl <= 0 and pkt.type in ("info", "message"):
            self.log.debug(f"TTL=0 descartado: {pkt.type} id={pkt.msg_id}")
            return

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
        if self.on_info_async is None:
            self.log.debug("INFO recibido pero sin servicio de ruteo (ignorado)")
            return

        origin = pkt.from_
        try:
            await self.on_info_async(origin, pkt.payload)
        except Exception as e:
            self.log.error(f"Error en on_info_async: {e}")

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

        if self.mode == "flooding":
            prev_hop: Optional[str] = pkt.headers[-1] if pkt.headers else None
            pkt_out = pkt.with_decremented_ttl().with_appended_hop(self.my_id)
            if pkt_out.ttl > 0:
                await self._broadcast_to_neighbors(pkt_out, exclude={prev_hop} if prev_hop else None)
                self.log.info(f"⇉ {pkt.from_} → {dst} (flooding) trace={pkt.trace_id}")
            return

        next_hop = await self.state.get_next_hop(dst)
        if next_hop:
            ch = self.neighbor_map.get(next_hop)
            if ch:
                pkt_out = pkt.with_decremented_ttl().with_appended_hop(self.my_id)
                if pkt_out.ttl > 0:
                    await self.transport.publish_json(ch, pkt_out.to_publish_dict())
                    self.log.info(f"[MSG] {pkt.from_}→{dst} via {next_hop} trace={pkt.trace_id}")
                    return

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
