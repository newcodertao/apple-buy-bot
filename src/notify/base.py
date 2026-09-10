from abc import ABC, abstractmethod

from src.core.models import Platform


class Notifier(ABC):
    """Implement this interface to add an explicitly configured notification service."""

    @abstractmethod
    async def notify(self, event: str, message: str, platform: Platform | None = None) -> None: ...
