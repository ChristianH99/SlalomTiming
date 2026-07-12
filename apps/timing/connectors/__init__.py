from django.conf import settings
from django.utils.module_loading import import_string

from .base import TimingDeviceConnector, TimingPulse

__all__ = ["TimingDeviceConnector", "TimingPulse", "get_connector"]


def get_connector() -> TimingDeviceConnector:
    """Instantiate the connector configured via settings.TIMING_CONNECTOR."""
    connector_class = import_string(settings.TIMING_CONNECTOR)
    return connector_class()
