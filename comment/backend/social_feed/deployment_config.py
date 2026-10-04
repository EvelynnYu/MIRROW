"""Template copied into the beta package; no public host is preconfigured."""
import os

PUBLIC_SOCIAL_HOST = os.environ.get("COMMENT_PUBLIC_HOST", "").strip().lower()
if not PUBLIC_SOCIAL_HOST:
    raise RuntimeError("COMMENT_PUBLIC_HOST must be configured before mounting the public wall")
if ":" in PUBLIC_SOCIAL_HOST or "/" in PUBLIC_SOCIAL_HOST or PUBLIC_SOCIAL_HOST.endswith("."):
    raise RuntimeError("COMMENT_PUBLIC_HOST must be a hostname without scheme, port or path")
PUBLIC_SOCIAL_ORIGIN = "https://" + PUBLIC_SOCIAL_HOST
