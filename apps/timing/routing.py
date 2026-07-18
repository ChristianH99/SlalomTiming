from django.urls import re_path

from . import consumers

websocket_urlpatterns = [
    re_path(r"ws/timing/$", consumers.TimingConsumer.as_asgi()),
    re_path(r"ws/timing/live/$", consumers.TimingLiveConsumer.as_asgi()),
]
