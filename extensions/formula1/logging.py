"""Run-scoped Formula 1 detail logging isolated from MetaFusion core logs."""

import logging
from datetime import datetime
from pathlib import Path


def _prune(directory, retention):
    files = sorted(Path(directory).glob("formula1-*.log"), reverse=True)
    for path in files[int(retention) :]:
        path.unlink(missing_ok=True)


def create_formula1_logger(config, run_id):
    name = f"metafusion.formula1.{run_id}"
    logger = logging.getLogger(name)
    logger.handlers.clear()
    logger.propagate = False
    logger.setLevel(getattr(logging, config["logging"]["level"]))
    formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    log_path = None
    if not config["dry_run"]:
        directory = config["paths"]["logs"]
        directory.mkdir(parents=True, exist_ok=True)
        log_path = directory / f"formula1-{run_id}.log"
        file_handler = logging.FileHandler(log_path, encoding="utf-8")
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
        _prune(directory, config["logging"]["retention"])
    if config["logging"]["console"] == "full":
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(formatter)
        logger.addHandler(stream_handler)
    if not logger.handlers:
        logger.addHandler(logging.NullHandler())
    return logger, log_path


def run_identifier(now=None):
    current = now or datetime.now().astimezone()
    return current.strftime("%Y%m%d-%H%M%S-%f")
