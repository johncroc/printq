"""Job tickets and the job state machine.

A job is a directory: jobs/<id>/ticket.json (immutable after submit) and
jobs/<id>/state.json (mutated only through `transition` and `update`).

States
    submitted    ticket accepted, nothing done yet
    sliced       G-code produced and verified against the ticket
    queued       announced to the operator, waiting for the printer
    bed_ok       a fresh bed-clear verdict exists (or the operator vouched)
    uploaded     G-code is on the printer
    awaiting_go  everything is ready, waiting for the operator's /go
    printing     the print was started
    done         the printer finished the job
    needs_human  the pipeline stopped and told the operator why
    dropped      removed from the queue by the operator

The allowed moves are listed in TRANSITIONS. Tools can only move a job along
those edges, which is what keeps an LLM-driven agent from skipping a step.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import time
from pathlib import Path

from . import config

TRANSITIONS = {
    "submitted": {"sliced", "needs_human", "dropped"},
    "sliced": {"queued", "needs_human", "dropped"},
    "queued": {"bed_ok", "needs_human", "dropped"},
    "bed_ok": {"uploaded", "queued", "needs_human", "dropped"},
    "uploaded": {"printing", "awaiting_go", "queued", "needs_human", "dropped"},
    "awaiting_go": {"uploaded", "queued", "needs_human", "dropped"},
    "printing": {"done", "needs_human"},
    "needs_human": {"submitted", "queued", "dropped"},
    "done": set(),
    "dropped": set(),
}
ACTIVE = ("submitted", "sliced", "queued", "bed_ok", "uploaded", "awaiting_go")
TICKET_REQUIRED = ("title", "printer", "model", "presets")


class JobError(Exception):
    """Raised for anything the caller (agent or operator) should be told verbatim."""


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def job_dir(job_id: str) -> Path:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,60}", job_id or ""):
        raise JobError(f"invalid job id '{job_id}'")
    path = config.JOBS / job_id
    if not path.is_dir():
        raise JobError(f"no such job '{job_id}'")
    return path


def _write(path: Path, data: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1))
    os.replace(tmp, path)


def ticket(job_id: str) -> dict:
    return json.loads((job_dir(job_id) / "ticket.json").read_text())


def state(job_id: str) -> dict:
    return json.loads((job_dir(job_id) / "state.json").read_text())


def update(job_id: str, **fields) -> dict:
    """Merge fields into the job's state. Locked, because the daemon and the tool server are two processes."""
    with open(job_dir(job_id) / ".lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        st = state(job_id)
        st.update(fields)
        st["updated"] = time.time()
        _write(job_dir(job_id) / "state.json", st)
    return st


def event(job_id: str, text: str) -> None:
    with open(job_dir(job_id) / "events.log", "a") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {text}\n")


def transition(job_id: str, new: str, **fields) -> dict:
    st = state(job_id)
    old = st["state"]
    if new not in TRANSITIONS[old]:
        raise JobError(f"job {job_id} is '{old}'; it cannot go to '{new}' from there")
    event(job_id, f"{old} -> {new}" + (f" ({fields.get('reason')})" if fields.get("reason") else ""))
    return update(job_id, state=new, **fields)


def validate_ticket(t: dict) -> dict:
    """Check a ticket and return a normalised copy. Raises JobError with every problem found."""
    problems = [f"missing '{k}'" for k in TICKET_REQUIRED if not t.get(k)]
    if problems:
        raise JobError("ticket: " + "; ".join(problems))
    out = dict(t)
    cfg = config.printer(t["printer"])
    model = Path(t["model"]).expanduser()
    if not model.is_file():
        problems.append(f"model file not found: {model}")
    elif model.suffix.lower() not in (".stl", ".3mf", ".step", ".stp"):
        problems.append(f"unsupported model type: {model.suffix}")
    presets = Path(t["presets"]).expanduser()
    if not presets.is_absolute():
        presets = Path(cfg.get("presets_root", "")).expanduser() / presets
    for part in ("machine", "process", "filament"):
        if not (presets / f"{part}.json").is_file():
            problems.append(f"preset set {presets} has no {part}.json")
    for key in ("overrides", "expect"):
        if not isinstance(t.get(key, {}), dict):
            problems.append(f"'{key}' must be an object of Orca setting -> value")
    if problems:
        raise JobError("ticket: " + "; ".join(problems))
    out.update(model=str(model), presets=str(presets), model_sha256=sha256(model),
               overrides=t.get("overrides", {}), expect=t.get("expect", {}),
               copies=int(t.get("copies", 1)), material=t.get("material", ""),
               notes=t.get("notes", ""))
    return out


def submit(t: dict) -> str:
    """Validate a ticket, create the job and return its id."""
    t = validate_ticket(t)
    # Short ids (j1, j2, ...) so that Telegram commands like /go_j12 are one tap on a phone.
    config.JOBS.mkdir(parents=True, exist_ok=True)
    taken = [int(p.name[1:]) for p in config.JOBS.iterdir() if re.fullmatch(r"j\d+", p.name)]
    job_id = f"j{max(taken, default=0) + 1}"
    path = config.JOBS / job_id
    path.mkdir()
    t["id"] = job_id
    t["submitted"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _write(path / "ticket.json", t)
    _write(path / "state.json", {"state": "submitted", "held": False, "updated": time.time()})
    event(job_id, "submitted")
    return job_id


def all_jobs() -> list[dict]:
    """Every job, oldest first, as {id, ticket fields..., state fields...}."""
    out = []
    if not config.JOBS.is_dir():
        return out
    for path in sorted(config.JOBS.iterdir(), key=lambda p: p.stat().st_ctime):
        try:
            t = json.loads((path / "ticket.json").read_text())
            s = json.loads((path / "state.json").read_text())
        except (OSError, ValueError):
            continue
        out.append({"id": path.name, "title": t["title"], "printer": t["printer"], **s})
    return out


def active(printer: str | None = None) -> list[dict]:
    return [j for j in all_jobs() if j["state"] in ACTIVE and (printer is None or j["printer"] == printer)]
