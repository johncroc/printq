"""Paths, settings and printer definitions.

Everything site-specific lives outside the repo:

    $PRINTQ_HOME (default ~/.config/printq)
        printers.json   printer definitions (see config/printers.example.json)
        env             KEY=value secrets, never committed
    $PRINTQ_DATA (default ~/.local/share/printq)
        jobs/<id>/      ticket, state, sliced G-code, frames, agent logs
        dataset/        every bed-check frame with its verdicts (training data)
"""
from __future__ import annotations

import json
import os
from pathlib import Path

HOME = Path(os.environ.get("PRINTQ_HOME", "~/.config/printq")).expanduser()
DATA = Path(os.environ.get("PRINTQ_DATA", "~/.local/share/printq")).expanduser()
JOBS = DATA / "jobs"
DATASET = DATA / "dataset"
REPO = Path(__file__).resolve().parent.parent


def load_env() -> None:
    """Read $PRINTQ_HOME/env into os.environ without overriding what is already set."""
    path = HOME / "env"
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


load_env()


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


class ConfigError(Exception):
    pass


def printers() -> dict:
    path = HOME / "printers.json"
    if not path.exists():
        raise ConfigError(f"{path} is missing; copy config/printers.example.json there and edit it")
    return json.loads(path.read_text())


def printer(name: str) -> dict:
    all_ = printers()
    if name not in all_:
        raise ConfigError(f"unknown printer '{name}' (known: {', '.join(all_)})")
    cfg = dict(all_[name])
    cfg["name"] = name
    return cfg


# Settings with defaults, overridable in $PRINTQ_HOME/env.
ORCA_BIN = env("PRINTQ_ORCA_BIN", "orca-slicer")
OLLAMA_URL = env("PRINTQ_OLLAMA_URL", "http://localhost:11434")
AGENT_MODEL = env("PRINTQ_AGENT_MODEL", "gpt-oss:20b")
VISION_MODEL = env("PRINTQ_VISION_MODEL", "claude-opus-5-5")
SHADOW_VISION_MODEL = env("PRINTQ_SHADOW_VISION_MODEL", "")  # e.g. ollama:gemma4:12b; logged, never acted on
SPOOLMAN_URL = env("PRINTQ_SPOOLMAN_URL", "")
BED_OK_TTL = int(env("PRINTQ_BED_OK_TTL", "300"))  # seconds a clear verdict stays valid
