import json
import os
import shutil
import tempfile
from pathlib import Path

import yaml


def _fsync_directory(path):
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def backup_path_for(path):
    path = Path(path)
    return path.with_name(f"{path.name}.bak")


def read_json_with_backup(path, default=None):
    """Read JSON and transparently recover from its last known-good backup."""
    path = Path(path)
    for candidate in (path, backup_path_for(path)):
        try:
            return json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            continue
    return default


def atomic_write_yaml(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temp_file:
            temp_path = temp_file.name
            yaml.dump(
                data,
                temp_file,
                allow_unicode=True,
                default_flow_style=False,
                sort_keys=False,
            )
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.replace(temp_path, path)
        _fsync_directory(path.parent)
    finally:
        if temp_path and os.path.exists(temp_path):
            os.unlink(temp_path)


def atomic_write_json(path, data, backup=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if backup and path.exists():
        backup_temp = None
        try:
            json.loads(path.read_text(encoding="utf-8"))
            backup_path = backup_path_for(path)
            backup_temp = backup_path.with_name(f".{backup_path.name}.tmp")
            shutil.copy2(path, backup_temp)
            os.replace(backup_temp, backup_path)
            _fsync_directory(path.parent)
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            pass
        finally:
            if backup_temp and backup_temp.exists():
                backup_temp.unlink()
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temp_file:
            temp_path = temp_file.name
            json.dump(data, temp_file, indent=2, sort_keys=True)
            temp_file.write("\n")
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.replace(temp_path, path)
        _fsync_directory(path.parent)
    finally:
        if temp_path and os.path.exists(temp_path):
            os.unlink(temp_path)


def atomic_write_bytes(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            "wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temp_file:
            temp_path = temp_file.name
            temp_file.write(data)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.replace(temp_path, path)
        _fsync_directory(path.parent)
    finally:
        if temp_path and os.path.exists(temp_path):
            os.unlink(temp_path)
