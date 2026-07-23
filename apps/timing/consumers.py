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

    async def timing_refresh(self, event):
        await self.send_json({"event": "refresh"})
