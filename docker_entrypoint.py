"""Prepare bind mounts and run MetaFusion as the requested numeric identity."""

import os
import sys
import tempfile
from pathlib import Path

DEFAULT_UID = 10001
DEFAULT_GID = 10001
MAX_ID = 2_147_483_647
CONFIG_TEMPLATE_SOURCE = Path(__file__).resolve().parent / "config_template.yml"
CONFIG_TEMPLATE_NAME = "config_template.yml"
MANAGED_FILE_MODE = 0o664


def parse_id(name, default):
    """Return a safe, non-root numeric user or group ID from the environment."""
    raw_value = os.environ.get(name, str(default)).strip()
    try:
        value = int(raw_value)
    except ValueError as error:
        raise ValueError(f"{name} must be a numeric ID, received {raw_value!r}") from error
    if not 1 <= value <= MAX_ID:
        raise ValueError(f"{name} must be between 1 and {MAX_ID}, received {raw_value!r}")
    return value


def _set_owner(path, uid, gid):
    status = os.stat(path, follow_symlinks=False)
    if (status.st_uid, status.st_gid) != (uid, gid):
        os.chown(path, uid, gid, follow_symlinks=False)


def _prepare_managed_directory(path, uid, gid):
    if path.is_symlink():
        raise RuntimeError(f"Managed runtime path cannot be a symbolic link: {path}")
    path.mkdir(parents=True, exist_ok=True)
    for root, directory_names, file_names in os.walk(path, followlinks=False):
        root_path = Path(root)
        _set_owner(root_path, uid, gid)
        for name in directory_names + file_names:
            _set_owner(root_path / name, uid, gid)


def prepare_runtime_paths(config_dir, uid, gid):
    """Prepare only MetaFusion-managed state, never the full Kometa asset tree."""
    config_dir = Path(config_dir)
    if config_dir.is_symlink():
        raise RuntimeError(f"Configuration directory cannot be a symbolic link: {config_dir}")
    config_dir.mkdir(parents=True, exist_ok=True)
    _set_owner(config_dir, uid, gid)

    for directory in (
        config_dir / "logs",
        config_dir / "cache",
        config_dir / "reports",
    ):
        _prepare_managed_directory(directory, uid, gid)

    status_file = Path(
        os.environ.get("STATUS_FILE", "/tmp/metafusion-status.json")
    )
    if status_file.parent == config_dir and status_file.exists():
        _set_owner(status_file, uid, gid)


def sync_config_template(config_dir, uid, gid, source=CONFIG_TEMPLATE_SOURCE):
    """Keep a value-free reference template in the persistent config directory."""
    config_dir = Path(config_dir)
    source = Path(source)
    destination = config_dir / CONFIG_TEMPLATE_NAME

    if config_dir.is_symlink():
        raise RuntimeError(f"Configuration directory cannot be a symbolic link: {config_dir}")
    config_dir.mkdir(parents=True, exist_ok=True)
    if destination.is_symlink():
        raise RuntimeError(f"Managed config template cannot be a symbolic link: {destination}")

    try:
        template_data = source.read_bytes()
    except OSError as error:
        raise RuntimeError(f"Unable to read packaged config template: {source}") from error

    try:
        current_data = destination.read_bytes()
    except FileNotFoundError:
        current_data = None
    except OSError as error:
        raise RuntimeError(f"Unable to read managed config template: {destination}") from error

    updated = current_data != template_data
    if updated:
        temporary = None
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                dir=config_dir,
                prefix=f".{CONFIG_TEMPLATE_NAME}.",
                suffix=".tmp",
            )
            temporary = Path(temporary_name)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(template_data)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, MANAGED_FILE_MODE, follow_symlinks=False)
            _set_owner(temporary, uid, gid)
            os.replace(temporary, destination)
        except OSError as error:
            raise RuntimeError(
                f"Unable to maintain config template at {destination}"
            ) from error
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    os.chmod(destination, MANAGED_FILE_MODE, follow_symlinks=False)
    _set_owner(destination, uid, gid)
    return updated


def drop_privileges(uid, gid):
    """Drop root and supplementary groups before starting the application."""
    os.setgroups([])
    os.setgid(gid)
    os.setuid(uid)


def main(argv=None):
    command = list(sys.argv[1:] if argv is None else argv)
    healthcheck = bool(command and command[0] == "--healthcheck")
    if healthcheck:
        command.pop(0)
    elif command and command[0].startswith("-"):
        command = ["python", "/app/metafusion.py", *command]
    if not command:
        print("MetaFusion startup error: no command was supplied", file=sys.stderr)
        return 64

    config_dir = os.environ.get("CONFIG_DIR", "/config")
    os.environ["HOME"] = config_dir

    if os.geteuid() == 0:
        try:
            uid = parse_id("PUID", DEFAULT_UID)
            gid = parse_id("PGID", DEFAULT_GID)
            if not healthcheck:
                prepare_runtime_paths(config_dir, uid, gid)
                sync_config_template(config_dir, uid, gid)
            drop_privileges(uid, gid)
        except (OSError, RuntimeError, ValueError) as error:
            print(f"MetaFusion startup error: {error}", file=sys.stderr)
            return 78
    elif not healthcheck:
        try:
            sync_config_template(config_dir, os.geteuid(), os.getegid())
        except (OSError, RuntimeError) as error:
            print(f"MetaFusion startup error: {error}", file=sys.stderr)
            return 78

    os.execvp(command[0], command)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
