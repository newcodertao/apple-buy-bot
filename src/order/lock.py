from src.storage.database import Database


class OrderLock:
    """A SQLite transaction arbitrates both concurrent tasks and separate processes."""

    def __init__(self, database: Database):
        self.database = database

    def acquire(self, owner: str) -> bool:
        return self.database.claim_order(owner)

    def release(self, owner: str) -> bool:
        return self.database.release_order(owner)

    def mark_submission(self, owner: str, status: str) -> None:
        self.database.mark_submission(owner, status)

    def status(self) -> dict | None:
        return self.database.guard_status()
