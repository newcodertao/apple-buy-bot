from src.core.config import ProductPreferences
from src.core.models import SKU, Platform
from src.platforms.marketplace import parse_product


def parse_skus(data: dict, *, product_id: str, preferences: ProductPreferences) -> list[SKU]:
    return [
        parse_product(data, platform=Platform.TMALL, product_id=product_id, preferences=preferences)
    ]
