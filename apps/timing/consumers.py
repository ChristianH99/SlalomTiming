from channels.generic.websocket import AsyncJsonWebsocketConsumer

from .services import TIMING_GROUP


class TimingConsumer(AsyncJsonWebsocketConsumer):
    async def connect(self):
        await self.channel_layer.group_add(TIMING_GROUP, self.channel_name)
        await self.accept()

    async def disconnect(self, close_code):
        await self.channel_layer.group_discard(TIMING_GROUP, self.channel_name)

    async def timing_event(self, event):
        await self.send_json(event["event"])
