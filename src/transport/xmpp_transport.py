from __future__ import annotations
import asyncio
import json
from typing import AsyncGenerator, Iterable, Optional

from slixmpp import ClientXMPP
from slixmpp.exceptions import IqError, IqTimeout

from src.utils.log import setup_logger


class _XmppClient(ClientXMPP):
    def __init__(self, jid: str, password: str, queue: asyncio.Queue, logger_name: str = "XMPP"):
        super().__init__(jid, password)
        self.log = setup_logger(logger_name)
        self._queue = queue

        self.add_event_handler("session_start", self._on_session_start)
        self.add_event_handler("message", self._on_message)

        # Menos verboso
        self.auto_reconnect = True
        self.auto_authorize = True
        self.auto_subscribe = True

    async def _on_session_start(self, event):
        try:
            self.send_presence()
            await self.get_roster()
            self.log.info("XMPP sesión iniciada")
        except (IqError, IqTimeout) as e:
            self.log.error(f"Error roster: {e}")

    def _on_message(self, msg):
        # Solo chat/normal
        if msg['type'] in ('chat', 'normal'):
            body = str(msg['body'] or "")
            if not body:
                return
            # Empujamos el JSON crudo (o lo que sea) a la queue
            self._queue.put_nowait(body)


class XmppTransport:
    """
    Transporte XMPP minimalista con interfaz compatible a RedisTransport:

        await connect()
        async for raw in read_loop(): ...
        await publish_json(jid, obj)
        await broadcast([jid1, jid2], obj)
        await close()
    """
    def __init__(
        self,
        jid: str,
        password: str,
        my_channel: str,
        host: Optional[str] = None,
        port: Optional[int] = None,
        logger_name: Optional[str] = None,
    ) -> None:
        self.jid = jid
        self.password = password
        self.host = host
        self.port = port
        self.my_channel = my_channel  # aquí el "canal" es el propio JID
        self.log = setup_logger(logger_name or f"XMPP-{jid}")
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._client = _XmppClient(jid, password, self._queue, logger_name=self.log.name)

    async def connect(self) -> None:
        # Conectar al servidor XMPP
        if self.host and self.port:
            await self._client.connect((self.host, self.port))
        else:
            await self._client.connect()
        # Correr el loop del cliente en segundo plano
        asyncio.create_task(self._client.wait_until_disconnected())
        self._client.loop = asyncio.get_event_loop()
        self._client.process(forever=False)  # integra al loop actual
        self.log.info(f"Conectado a XMPP como {self.jid}")

    async def close(self) -> None:
        try:
            self._client.disconnect()
        except Exception:
            pass
        self.log.info("XMPP desconectado")

    async def publish_json(self, channel: str, obj: dict) -> None:
        raw = json.dumps(obj, ensure_ascii=False)
        self._client.send_message(mto=channel, mbody=raw, mtype='chat')

    async def broadcast(self, channels: Iterable[str], obj: dict) -> None:
        raw = json.dumps(obj, ensure_ascii=False)
        for ch in channels:
            self._client.send_message(mto=ch, mbody=raw, mtype='chat')

    async def read_loop(self) -> AsyncGenerator[str, None]:
        while True:
            raw = await self._queue.get()
            yield raw
