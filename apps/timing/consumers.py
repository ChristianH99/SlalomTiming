import asyncio

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer

from .services import LIVE_GROUP, TIMING_GROUP, register_server_loop

# Which pages a live socket belongs to. The nudges it carries are about the event
# being timed — that a time landed, and which competition is now active — so it is
# for the screens that show one. A login was the only check before (SEC-12), which
# meant any account at all could listen in on a running event.
LIVE_PAGES = {"dashboard", "timing", "marshal_posts", "results"}


@database_sync_to_async
def _may_listen(user):
    """Whether this user holds any page a live view is rendered on.

    Off the event loop because it reads the user's groups — the same reason the
    HTTP gate can do this inline and a consumer cannot.
    """
    from apps.accounts import pages

    if not (user and user.is_authenticated):
        return False
    return bool(LIVE_PAGES & pages.user_pages(user))


class TimingConsumer(AsyncJsonWebsocketConsumer):
    async def connect(self):
        if not await _may_listen(self.scope.get("user")):
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
        if not await _may_listen(self.scope.get("user")):
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
