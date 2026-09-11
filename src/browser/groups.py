"""Logical channels share a fixed set of existing persistent browser profiles."""

from src.core.models import Platform


def profile_platform(platform: Platform) -> Platform:
    platform = Platform(platform)
    return Platform.TMALL if platform == Platform.TAOBAO else platform


def session_key(platform: Platform) -> str:
    physical = profile_platform(platform)
    return "alibaba" if physical == Platform.TMALL else physical.value


def unique_profile_platforms() -> tuple[Platform, ...]:
    return tuple(dict.fromkeys(profile_platform(platform) for platform in Platform))
