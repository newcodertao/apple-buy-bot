from src.core.models import Platform
from src.platforms.apple_cn.adapter import AppleCNAdapter


class TmallAdapter(AppleCNAdapter):
    """Explicit Phase 5 placeholder; all transaction selectors are UNKNOWN."""

    platform = Platform.TMALL
    allowed_hosts = ("tmall.com", "taobao.com")
    phase = "Phase 5"
