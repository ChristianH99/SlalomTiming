import asyncio

from channels.generic.websocket import AsyncJsonWebsocketConsumer

from .services import LIVE_GROUP, TIMING_GROUP, register_server_loop


class TimingConsumer(AsyncJsonWebsocketConsumer):
    async def connect(self):
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
        # Record the server's event loop so background threads (the CP540 reader)
        # can hand their refresh nudges to the loop the channel layer lives on.
        register_server_loop(asyncio.get_running_loop())
        await self.channel_layer.group_add(LIVE_GROUP, self.channel_name)
        await self.accept()

    async def disconnect(self, close_code):
        await self.channel_layer.group_discard(LIVE_GROUP, self.channel_name)

    async def timing_refresh(self, event):
        await self.send_json({"event": "refresh"})
