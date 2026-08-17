import os


def build_info(environ=None):
    environ = os.environ if environ is None else environ
    return {
        "version": str(environ.get("METAFUSION_VERSION") or "development"),
        "commit": str(environ.get("METAFUSION_COMMIT") or "unknown"),
    }
