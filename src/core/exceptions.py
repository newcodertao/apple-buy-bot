class BotError(Exception):
    """Only static, non-sensitive messages should be exposed to logs."""


class HumanRequired(BotError):
    pass


class SelectorNotFound(HumanRequired):
    pass


class RetryableError(BotError):
    def __init__(self, message: str = "Temporary request failure", retry_after: float = 0):
        super().__init__(message)
        self.retry_after = retry_after


class OrderRejected(BotError):
    """Confirmed rejection before/after submit; no order was created."""


class SubmissionUnknown(HumanRequired):
    """Submission outcome is ambiguous. Retain lock and require reconciliation."""


class ConfigurationError(BotError):
    pass
