"""Application-owned provider credentials bundled with MetaFusion."""

# Fanart.tv project keys identify the integrating application, rather than an
# individual installation. Fanart.tv directs application developers to ship
# their project key so end users are not required to create one.
FANART_PROJECT_API_KEY = "e5e1ba5021da792b8ca729ee332420a4"


def fanart_project_api_key():
    """Return MetaFusion's bundled Fanart.tv project integration key."""
    return FANART_PROJECT_API_KEY
