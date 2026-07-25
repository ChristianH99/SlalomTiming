import asyncio

from channels.generic.websocket import AsyncJsonWebsocketConsumer

from .services import LIVE_GROUP, TIMING_GROUP, register_server_loop


def _is_authenticated(scope):
    user = scope.get("user")
    return bool(user and user.is_authenticated)


class TimingConsumer(AsyncJsonWebsocketConsumer):
    async def connect(self):
        if not _is_authenticated(self.scope):
            await self.close()
            return
        await self.channel_layer.group_add(TIMING_GROUP, self.channel_name)
        await self.accept()

    async def disconnect(self, close_code):
        await self.channel_layer.group_discard(TIMING_GROUP, self.channel_name)

    async def timing_event(self, event):
        await self.send_json(event["event"])


class TimingLiveConsumer(AsyncJsonWebsocketConsumer):
    """Pushes a lightweight "refresh" nudge to open live-timing views when signals
    or run assignments change; the client then re-fetches the arrangement."""

    async def connect(self):
        if not _is_authenticated(self.scope):
            await self.close()
            return
        # Record the server's event loop so background threads (the CP540 reader)
        # can hand their refresh nudges to the loop the channel layer lives on.
        register_server_loop(asyncio.get_running_loop())
        await self.channel_layer.group_add(LIVE_GROUP, self.channel_name)
        await self.accept()

    async def disconnect(self, close_code):
        await self.channel_layer.group_discard(LIVE_GROUP, self.channel_name)

    async def receive_json(self, content, **kwargs):
        """The only thing a client sends is a heartbeat. A socket can die without
        a close frame — a phone that walks out of Wi-Fi range gets no TCP FIN —
        and the page would then sit there looking live while nothing arrives, so
        live_socket.js pings and treats silence as a dead link. Anything else is
        ignored rather than raising (the base class's receive_json would)."""
        if isinstance(content, dict) and content.get("action") == "ping":
            await self.send_json({"event": "pong"})

    async def timing_refresh(self, event):
        await self.send_json({"event": "refresh"})

    async def timing_competition(self, event):
        await self.send_json({"event": "competition", "name": event.get("name", "")})
