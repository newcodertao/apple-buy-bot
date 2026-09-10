from src.core.models import Platform
from src.platforms.inspection import InspectionAdapter


class JDAdapter(InspectionAdapter):
    """Explicit Phase 4 placeholder; all transaction selectors are UNKNOWN."""

    platform = Platform.JD
    allowed_hosts = ("jd.com",)
    phase = "Phase 4"
