from src.core.exceptions import SelectorNotFound
from src.core.models import SKU


def parse_skus(_: object) -> list[SKU]:
    raise SelectorNotFound("UNKNOWN: Phase 5 Tmall parser is not implemented")
