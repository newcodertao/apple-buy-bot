"""Product parsing awaits verified live Apple China markup."""

from src.core.exceptions import SelectorNotFound
from src.core.models import SKU


def parse_skus(_: object) -> list[SKU]:
    raise SelectorNotFound("UNKNOWN: Apple product/SKU parser needs real page verification")
