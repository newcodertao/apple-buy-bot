from pathlib import Path

import pytest
from pydantic import ValidationError

from src.core.config import (
    AppConfig,
    MonitorSettings,
    OrderSettings,
    ProductPreferences,
    load_config,
    validate_platform_url,
)
from src.core.exceptions import ConfigurationError
from src.core.models import Platform


def test_example_is_safe_and_targets_unknown():
    config = load_config(Path(__file__).parents[1] / "config/config.example.yaml")
    assert config.app.dry_run and not config.order.auto_submit
    assert config.order.single_order_lock
    assert config.targets() == []
    assert config.sale.target() is None
    assert config.preferences_for("iphone18promax").model_priority == ["iPhone 18 Pro Max"]


@pytest.mark.parametrize("extra", [{"password": "never-store"}, {"app": {"token": "no"}}])
def test_unknown_or_sensitive_config_keys_rejected(extra):
    with pytest.raises(ValidationError):
        AppConfig.model_validate(extra)


@pytest.mark.parametrize(
    "url",
    [
        "https://apple.com.evil.example/buy",
        "http://www.apple.com/cn/",
        "https://jd.com/a",
        "https://user:secret@www.apple.com/cn/",
        "https://www.apple.com:1234/cn/",
    ],
)
def test_reject_wrong_platform_url(url):
    with pytest.raises(ValueError):
        validate_platform_url(Platform.APPLE, url)


def test_stock_limits_are_bounded():
    with pytest.raises(ValidationError):
        MonitorSettings(sale_interval=0.01)
    with pytest.raises(ValidationError):
        MonitorSettings(max_retries=-1)
    with pytest.raises(ValidationError):
        OrderSettings(single_order_lock=False)
    with pytest.raises(ValidationError):
        ProductPreferences(max_price="NaN")


def test_priorities_and_timezone():
    with pytest.raises(ValidationError):
        ProductPreferences(capacity_priority=["512GB", "512GB"])
    with pytest.raises(ValidationError):
        AppConfig.model_validate({"sale": {"timezone": "Invalid/Zone"}})
    config = AppConfig.model_validate({"sale": {"time": "2026-09-12 20:00:00"}})
    assert config.sale.target().isoformat() == "2026-09-12T20:00:00+08:00"


def test_product_override_and_target_order():
    config = AppConfig.model_validate(
        {
            "products": {
                "pro": {
                    "model": "iPhone 18 Pro",
                    "platforms": {
                        "apple": {"url": "https://www.apple.com.cn/shop/buy-iphone/fixture"}
                    },
                },
                "max": {
                    "model": "iPhone 18 Pro Max",
                    "max_price": 12000,
                    "capacity_priority": ["256GB"],
                    "platforms": {
                        "apple": {"url": "https://www.apple.com.cn/shop/buy-iphone/fixture"}
                    },
                },
            }
        }
    )
    assert [t[1] for t in config.targets()] == ["max", "pro"]
    assert config.preferences_for("max").capacity_priority == ["256GB"]
    assert config.preferences_for("max").max_price == 12000


def test_invalid_file_error_does_not_echo_secret(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("password: highly-private-value", encoding="utf-8")
    with pytest.raises(ConfigurationError) as exc:
        load_config(path)
    assert "highly-private" not in str(exc.value)
