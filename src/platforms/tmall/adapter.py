from src.core.models import Platform
from src.platforms.inspection import InspectionAdapter


class TmallAdapter(InspectionAdapter):
    """Explicit Phase 5 placeholder; all transaction selectors are UNKNOWN."""

    platform = Platform.TMALL
    allowed_hosts = ("tmall.com", "taobao.com")
    phase = "Phase 5"
