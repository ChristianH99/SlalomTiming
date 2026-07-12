import asyncio

from django.conf import settings
from django.core.management.base import BaseCommand

from apps.timing.services import run_ingestion


class Command(BaseCommand):
    help = (
        "Connects to the timing device configured via settings.TIMING_CONNECTOR "
        "and streams events into the database and the live dashboard."
    )

    def handle(self, *args, **options):
        self.stdout.write(f"Using connector: {settings.TIMING_CONNECTOR}")
        try:
            asyncio.run(run_ingestion())
        except KeyboardInterrupt:
            self.stdout.write(self.style.WARNING("Stopped."))
