import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


def safe_url(url: str) -> str:
    """Queries, fragments, credentials and opaque URL path tokens are not log data."""
    try:
        parts = urlsplit(url)
        path = re.sub(r"[A-Za-z0-9_-]{24,}", "[REDACTED]", parts.path)
        path = re.sub(r"\d{7,}", "[REDACTED]", path)
        return urlunsplit((parts.scheme, parts.hostname or "", path, "", ""))
    except ValueError:
        return "[INVALID_URL]"


def redact(value: str) -> str:
    text = re.sub(r"https?://[^\s<>\"']+", lambda m: safe_url(m[0]), str(value))
    text = re.sub(
        r"(?i)\b(password|passwd|cookie|token|authorization|secret|otp|sessionid)\b"
        r"\s*[:=]\s*[^\n,;]+",
        r"\1=[REDACTED]",
        text,
    )
    text = re.sub(r"(?<!\w)\d{17}[0-9Xx](?!\w)", "[ID_REDACTED]", text)
    text = re.sub(r"(?<!\d)(?:\+?86[- ]?)?1[3-9][0-9 -]{9,13}(?!\d)", "[PHONE_REDACTED]", text)
    text = re.sub(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b", "[EMAIL_REDACTED]", text)
    return text


class SafeFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        # Exception bodies can contain HTML, request headers or credentials.
        safe = logging.makeLogRecord(record.__dict__.copy())
        safe.msg = redact(record.getMessage())
        safe.args = ()
        if safe.exc_info:
            safe.msg += " exception=" + safe.exc_info[0].__name__
            safe.exc_info = None
            safe.exc_text = None
        return super().format(safe)


def setup_logging(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    formatter = SafeFormatter(
        "%(asctime)s.%(msecs)03d %(levelname)s %(name)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
    for name in ("engine", "apple", "jd", "tmall", "taobao"):
        logger = logging.getLogger(name)
        logger.setLevel(logging.INFO)
        logger.propagate = False
        for handler in logger.handlers[:]:
            logger.removeHandler(handler)
            handler.close()
        handler = RotatingFileHandler(
            directory / f"{name}.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8"
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)
