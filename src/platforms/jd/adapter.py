from src.core.models import Platform
from src.platforms.apple_cn.adapter import AppleCNAdapter


class JDAdapter(AppleCNAdapter):
    """Explicit Phase 4 placeholder; all transaction selectors are UNKNOWN."""

    platform = Platform.JD
    allowed_hosts = ("jd.com",)
    phase = "Phase 4"
