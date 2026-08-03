#!/usr/bin/env python3
"""Shared loguru setup for the trading pipeline agents (2026-06-28). One rotating,
retained file per agent (logs/<name>.log) -- daily rotation, 7-day retention -- so log
volume can never grow unbounded regardless of how often a cron-fired script runs.

This does NOT remove loguru's default stderr sink, so interactive runs and the existing
wrapper-script stdout/stderr redirects keep working exactly as before -- get_logger()
only ADDS a durable, bounded file sink on top.

Per-process safe even when multiple agent modules get imported into the same process
(e.g. walkforward_search.py imports mean_reversion_scanner): each sink is filtered to
its own agent name via logger.bind(), so cross-imports never leak log lines into the
wrong file.
"""
from pathlib import Path
from loguru import logger

ROOT = Path(__file__).resolve().parent
LOG_DIR = ROOT / "logs"
_configured = set()


def get_logger(name):
    """Configure (once per process) a rotating/retained sink at logs/<name>.log and
    return a logger bound to that agent name. Safe to call repeatedly with the same
    name (e.g. on module re-import) -- the sink is only added once."""
    if name not in _configured:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        logger.add(
            LOG_DIR / f"{name}.log",
            rotation="00:00",       # new file at UTC midnight (box system tz = UTC)
            retention="7 days",     # hard cap on history -- never silently fills disk
            enqueue=True,           # thread/process-write-safe
            backtrace=False,
            diagnose=False,
            level="DEBUG",
            format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {message}",
            filter=lambda record, _n=name: record["extra"].get("agent") == _n,
        )
        _configured.add(name)
    return logger.bind(agent=name)
