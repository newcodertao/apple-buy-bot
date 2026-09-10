import logging

from src.core.models import Platform
from src.notify.base import Notifier


class ConsoleNotifier(Notifier):
    async def notify(self, event: str, message: str, platform: Platform | None = None) -> None:
        logging.getLogger("engine").info(
            "platform=%s action=%s message=%s", platform or "all", event, message
        )
