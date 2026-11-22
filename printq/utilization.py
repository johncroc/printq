"""Record what every printer is doing, all the time, and report how much of it was printing.

A printer earns only while it prints. This module keeps a timeline per printer: one line each
time its situation changes, saying what it was doing and, when it was not printing, why not.
The reasons are sorted by who can do something about them:

    active     printing
    process    time the workflow itself costs: heating and calibrating before a print, the
               cool-down after it, the bed check and upload, slicing, a macro job
    operator   waiting for a person: a paused print (spool change), a bed that needs clearing,
               a job waiting for /go, held, or stopped with a question
    no_work    idle with nothing in the queue for this printer
    down       unreachable or in an error state

Timeline files are data/utilization/<printer>.jsonl, lines of {"t": unix time, "state": ..., "job": ...}.
A state lasts until the next line. Time during which the queue was not running is 'not_recorded'.
"""
from __future__ import annotations

import json
import time

from . import config, jobs, moonraker

SAMPLE = 30  # seconds between looks
GROUPS = {
    "active": ("printing",),
    "process": ("starting", "cooldown", "launching", "slicing", "macro"),
    "operator": ("paused", "bed_not_clear", "waiting_go", "held", "needs_operator"),
    "no_work": ("no_work",),
    "down": ("offline", "error"),
    "not_recorded": ("not_recorded",),
}
MEANING = {
    "printing": "printing", "starting": "heating / calibrating before a print", "cooldown": "cool-down after a print",
    "launching": "bed check, upload, start countdown", "slicing": "next job being sliced", "macro": "macro job",
    "paused": "print paused (spool change, runout)", "bed_not_clear": "waiting for the bed to be cleared",
    "waiting_go": "waiting for /go", "held": "next job on hold", "needs_operator": "next job stopped with a question",
    "no_work": "nothing queued", "offline": "printer unreachable", "error": "printer error",
    "not_recorded": "queue was not running",
}


def _dir():
    return config.DATA / "utilization"


def classify(name: str, queue: list[dict]) -> tuple[str, str]:
    """(state, job id) for one printer right now. `queue` is jobs.all_jobs()."""
    mine = [j for j in queue if j["printer"] == name]
    running = next((j for j in mine if j["state"] == "printing"), None)
    job_id = running["id"] if running else ""
    try:
        live = moonraker.status(config.printer(name))
    except (moonraker.PrinterError, KeyError, ValueError):
        return "offline", job_id
    if live["state"] == "printing":
        return ("printing" if live.get("extruding") else "starting"), job_id
    if live["state"] == "paused":
        return "paused", job_id
    if live["state"] == "error":
        return "error", job_id
    if live.get("cooling"):
        return "cooldown", job_id
    if running:
        return ("macro" if running.get("kind") == "macro" else "launching"), job_id
    # The printer is free. What is the queue waiting for?
    for state, label in (("uploaded", "launching"), ("bed_ok", "launching"), ("awaiting_go", "waiting_go")):
        job = next((j for j in mine if j["state"] == state), None)
        if job:
            return label, job["id"]
    ready = [j for j in mine if j["state"] == "queued" and not j.get("held")]
    if ready:
        return ("bed_not_clear" if ready[0].get("waiting_bed") else "launching"), ready[0]["id"]
    for state, label in (("submitted", "slicing"), ("sliced", "slicing")):
        job = next((j for j in mine if j["state"] == state), None)
        if job:
            return label, job["id"]
    held = next((j for j in mine if j["state"] == "queued"), None)
    if held:
        return "held", held["id"]
    stuck = next((j for j in mine if j["state"] == "needs_human"), None)
    if stuck:
        return "needs_operator", stuck["id"]
    return "no_work", ""


def _append(name: str, t: float, state: str, job: str) -> None:
    _dir().mkdir(parents=True, exist_ok=True)
    with open(_dir() / f"{name}.jsonl", "a") as f:
        f.write(json.dumps({"t": round(t), "state": state, "job": job}) + "\n")


def sample(last: dict) -> None:
    """Look at every printer once; write a line for each whose situation changed. `last` carries state between calls."""
    queue = jobs.all_jobs()
    now = time.time()
    for name in config.printers():
        now_is = classify(name, queue)
        seen = _dir() / f"{name}.seen"
        if name not in last:
            # First look since the queue started: the time since the previous run's last look is unknown.
            try:
                before = float(seen.read_text())
                if now - before > 3 * SAMPLE:
                    _append(name, before, "not_recorded", "")
                    last[name] = ("not_recorded", "")
                else:
                    lines = (_dir() / f"{name}.jsonl").read_text().splitlines()
                    prev = json.loads(lines[-1])
                    last[name] = (prev["state"], prev.get("job", ""))
            except (OSError, ValueError, IndexError, KeyError):
                pass
        if last.get(name) != now_is:
            _append(name, now, *now_is)
            last[name] = now_is
        _dir().mkdir(parents=True, exist_ok=True)
        seen.write_text(str(round(now)))


def loop() -> None:
    last: dict = {}
    while True:
        try:
            sample(last)
        except Exception as exc:  # noqa: BLE001 - the record must never take the queue down
            from . import notify
            notify.log(f"utilization: {exc}")
        time.sleep(SAMPLE)


def totals(name: str, since: float, until: float | None = None) -> dict:
    """Seconds per state for one printer between two times."""
    until = until or time.time()
    try:
        rows = [json.loads(x) for x in (_dir() / f"{name}.jsonl").read_text().splitlines() if x.strip()]
        until = min(until, float((_dir() / f"{name}.seen").read_text()) + SAMPLE)
    except (OSError, ValueError):
        return {}
    out: dict = {}
    for row, nxt in zip(rows, rows[1:] + [{"t": until}]):
        a, b = max(row["t"], since), min(nxt["t"], until)
        if b > a:
            out[row["state"]] = out.get(row["state"], 0) + (b - a)
    return out


def hms(sec: float) -> str:
    sec = int(round(sec))
    return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def report(days: float = 1, printer: str | None = None) -> str:
    since = time.time() - days * 86400
    out = []
    for name in ([printer] if printer else config.printers()):
        t = totals(name, since)
        known = sum(v for k, v in t.items() if k != "not_recorded")
        if not known:
            out.append(f"{name}: nothing recorded yet")
            continue
        active = sum(t.get(s, 0) for s in GROUPS["active"])
        out.append(f"{name}: {100 * active / known:.1f}% active over {hms(known)} recorded (last {days:g} d)")
        for group, states in GROUPS.items():
            got = [(s, t[s]) for s in states if t.get(s)]
            if not got or group == "not_recorded":
                continue
            total = sum(v for _, v in got)
            out.append(f"  {group:<9}{100 * total / known:5.1f}%  {hms(total)}")
            if group != "active":
                out += [f"    {MEANING[s]}: {hms(v)}" for s, v in sorted(got, key=lambda x: -x[1])]
        if t.get("not_recorded"):
            out.append(f"  (not recorded: {hms(t['not_recorded'])})")
    return "\n".join(out)
