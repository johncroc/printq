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
# The ticket format, in one place: validation, `printq blank` and the error messages all read this table.
# name -> (kinds it belongs to, required, empty value, what to put there)
KINDS = ("print", "macro")
FIELDS = {
    "kind": (KINDS, False, "print", "'print' slices a model and prints it. 'macro' runs G-code commands or macros "
                                    "on the printer (a calibration, a PID tune) with nothing to slice"),
    "title": (KINDS, True, "", "Shown in every message to the operator"),
    "printer": (KINDS, True, "", "A printer name from printers.json"),
    "model": (("print",), True, "", "Path to the exported STL, 3MF or STEP. Hashed at submit: do not change the "
                                    "file afterwards"),
    "presets": (("print",), True, "", "A preset set: a directory with machine.json, process.json and filament.json. "
                                      "A relative name resolves against the printer's presets_root"),
    "material": (("print",), False, "", "For example ASA. Checked against the filament preset and the loaded spool"),
    "copies": (("print",), False, 1, "Number of copies to arrange on the plate"),
    "overrides": (("print",), False, {}, "OrcaSlicer settings to change for this job only, by Orca's own setting "
                                         "names. An unknown name fails the job"),
    "expect": (("print",), False, {}, "Settings that must have these values in the sliced G-code, without changing "
                                      "anything. Pin at least layer_height and nozzle_temperature"),
    "gcode": (("macro",), True, [], "List of commands, one per string, run in order; each must finish before the "
                                    "next is sent and the first error stops the job. SAVE_CONFIG, RESTART and "
                                    "FIRMWARE_RESTART are waited out until Klipper is ready again"),
    "bed": (("macro",), False, "clear", "'clear': run only after the camera bed check, like a print (anything that "
                                        "homes, probes or moves over the bed). 'any': the bed may hold a part "
                                        "(a hotend PID tune with the toolhead where it is)"),
    "timeout_sec": (("macro",), False, 1800, "Longest one command may take before the job is reported as stuck"),
    "heaters_off": (("macro",), False, True, "Send TURN_OFF_HEATERS when the commands have finished. Heaters are "
                                             "always turned off when a command fails"),
    "notes": (KINDS, False, "", "Plain sentences for the local agent: what is unusual, what to tell the operator"),
}
TICKET_REQUIRED = tuple(k for k, v in FIELDS.items() if v[1] and v[0] == KINDS)


def fields(kind: str) -> dict:
    return {k: v for k, v in FIELDS.items() if kind in v[0]}


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
    t = {k: v for k, v in t.items() if not k.startswith("_")}  # "_help" and the like: for the writer only
    kind = t.get("kind") or "print"
    if kind not in KINDS:
        raise JobError(f"ticket: unknown kind '{kind}' (kinds: {', '.join(KINDS)})")
    mine = fields(kind)
    problems = [f"missing '{k}'" for k, v in mine.items() if v[1] and not t.get(k)]
    # A misspelt optional field would otherwise be dropped without a word.
    problems += [f"unknown field '{k}' (fields: {', '.join(FIELDS)})" for k in t if k not in FIELDS]
    problems += [f"'{k}' does not belong in a {kind} ticket" for k in t
                 if k in FIELDS and k not in mine and t[k] not in (None, FIELDS[k][2])]
    if problems:
        raise JobError("ticket: " + "; ".join(problems))
    out = {k: t.get(k, v[2]) for k, v in mine.items()}
    out["kind"] = kind
    config.printer(t["printer"])
    if kind == "macro":
        lines = t["gcode"]
        if not isinstance(lines, list) or not all(isinstance(x, str) and x.strip() and "\n" not in x for x in lines):
            problems.append("'gcode' must be a list of commands, one command per string")
        else:
            out["gcode"] = [x.strip() for x in lines]
        if out["bed"] not in ("clear", "any"):
            problems.append("'bed' must be 'clear' or 'any'")
        if not isinstance(out["timeout_sec"], (int, float)) or isinstance(out["timeout_sec"], bool) or out["timeout_sec"] <= 0:
            problems.append("'timeout_sec' must be a number of seconds")
        if not isinstance(out["heaters_off"], bool):
            problems.append("'heaters_off' must be true or false")
        if problems:
            raise JobError("ticket: " + "; ".join(problems))
        return out
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
    out.update(model=str(model), presets=str(presets), model_sha256=sha256(model), copies=int(out["copies"]))
    return out


def blank(printer: str | None = None, kind: str = "print") -> dict:
    """An empty ticket with the current format and this site's choices described under "_help".

    The format is described by the program, not by documentation that can fall behind it: whoever
    writes a ticket asks for a blank one first. Keys starting with "_" are ignored at submit.
    """
    known = config.printers()
    if printer is not None:
        config.printer(printer)  # raises for an unknown name
    last = {}
    for job in all_jobs():
        try:
            if ticket(job["id"]).get("presets"):
                last[job["printer"]] = ticket(job["id"])["presets"]
        except (OSError, ValueError, KeyError):
            pass
    if kind not in KINDS:
        raise JobError(f"unknown kind '{kind}' (kinds: {', '.join(KINDS)})")
    mine = fields(kind)
    site = {}
    for name in ([printer] if printer else known):
        root = Path(known[name].get("presets_root", "")).expanduser()
        sets = sorted(d.name for d in root.iterdir()
                      if all((d / f"{part}.json").is_file() for part in ("machine", "process", "filament"))
                      ) if root.is_dir() else []
        site[name] = {"start_mode": known[name].get("start_mode", "confirm")}
        if kind == "print":
            site[name].update(presets_root=str(root), preset_sets=sets, presets_of_last_job=last.get(name, ""))
    out = {k: v[2] for k, v in mine.items()}
    out["kind"] = kind
    if printer:
        out["printer"] = printer
    other = "printq blank --macro" if kind == "print" else "printq blank"
    out["_help"] = {
        "how": "Fill in the fields above. Leave an optional field as it is, or delete it, to take the default. "
               "Then: printq check FILE (validates, queues nothing), printq submit FILE (prints the job id). "
               "Everything under _help is ignored. A queued ticket is never edited: drop the job and submit again.",
        "kinds": f"This is a {kind} ticket. '{other}' gives the blank ticket of the other kind.",
        "required": [k for k, v in mine.items() if v[1]],
        "optional": [k for k, v in mine.items() if not v[1]],
        "fields": {k: v[3] for k, v in mine.items()},
        "printers": site,
    }
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
    for path in config.JOBS.iterdir():
        try:
            t = json.loads((path / "ticket.json").read_text())
            s = json.loads((path / "state.json").read_text())
        except (OSError, ValueError):
            continue
        out.append({"id": path.name, "title": t["title"], "printer": t["printer"], "kind": t.get("kind", "print"),
                    "submitted": t.get("submitted", ""), **s})
    # Order of submission. Not the directory's timestamps: those move every time a file is added to a job.
    digits = lambda j: int(re.sub(r"\D", "", j["id"]) or 0)  # noqa: E731
    return sorted(out, key=lambda j: (j["submitted"], digits(j), j["id"]))


def active(printer: str | None = None) -> list[dict]:
    return [j for j in all_jobs() if j["state"] in ACTIVE and (printer is None or j["printer"] == printer)]
