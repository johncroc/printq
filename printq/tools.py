"""The tools an agent may call, and the gates inside them.

This module is the safety boundary of the whole system. The agent that
drives a job is a small local language model; it may call these tools in any
order with any arguments. Each tool therefore checks for itself that the job
is in the right state and that the physical preconditions hold *now*, and
refuses with a plain-language reason otherwise. Nothing the model says can
start a print: only `start_job` can, and only after its own checks pass.

Every tool returns a JSON-serialisable dict. Refusals are {"refused": reason}.
"""
from __future__ import annotations

import time
from pathlib import Path

from . import bedcheck, config, jobs, moonraker, notify, slicer

IDLE_HINT = "Stop here; the queue will run the launch again when the printer is idle."


def _gcode_name(t: dict) -> str:
    return f"printq_{t['id']}.gcode"


def _macro(t: dict) -> bool:
    return t.get("kind") == "macro"


def _summary(t: dict, report: dict) -> str:
    if _macro(t):
        lines = t["gcode"]
        shown = "; ".join(lines[:4]) + (f"; and {len(lines) - 4} more" if len(lines) > 4 else "")
        bed = "runs after the bed check" if t["bed"] == "clear" else "runs with whatever is on the bed"
        return f"{t['title']} for {t['printer']}: macro job, {bed}\n{shown}"
    e = report["estimate"]
    return (f"{t['title']} for {t['printer']}: {e['filament_g']:.1f} g / {e['filament_mm'] / 1000:.2f} m, "
            f"{e['time']}, {e['layers']} layers, {e.get('height_mm', '?')} mm tall")


def get_job(job_id: str) -> dict:
    """The job's ticket (what to print, where, with which settings, and the designer's notes) and its state."""
    return {"ticket": jobs.ticket(job_id), "state": jobs.state(job_id)}


def slice_job(job_id: str) -> dict:
    """Slice the job's model with the ticket's presets and overrides, then verify the G-code against the ticket.
    Returns OrcaSlicer's filament and time estimate. Allowed while the job is 'submitted'."""
    st = jobs.state(job_id)
    if st["state"] != "submitted":
        return {"refused": f"job is '{st['state']}'; slicing happens only from 'submitted'"}
    t = jobs.ticket(job_id)
    if _macro(t):
        jobs.transition(job_id, "sliced", slice_report={"ok": True, "macro": True})
        return {"sliced": True, "summary": _summary(t, {}), "next": "a macro job has nothing to slice; go on to "
                "printer_status and announce_job"}
    if jobs.sha256(Path(t["model"])) != t["model_sha256"]:
        return {"refused": "the model file changed after the design was approved; escalate"}
    report = slicer.slice_ticket(t, jobs.job_dir(job_id) / "slice")
    if not report["ok"]:
        jobs.update(job_id, slice_report=report)
        return {"sliced": False, "problems": report["problems"],
                "next": "these are not fixable from here; call escalate with the problems"}
    jobs.transition(job_id, "sliced", slice_report=report)
    return {"sliced": True, "summary": _summary(t, report), "estimate": report["estimate"],
            "settings": report["settings"], "warnings": report["warnings"]}


def printer_status(printer: str) -> dict:
    """Live state of a printer: idle or printing, current file, temperatures, and the loaded spool
    (material and metres left) when Spoolman is configured."""
    try:
        return moonraker.status(config.printer(printer))
    except (moonraker.PrinterError, config.ConfigError) as exc:
        return {"printer": printer, "reachable": False, "error": str(exc)}


def announce_job(job_id: str, note: str = "") -> dict:
    """Tell the operator the job is in the queue. The message always carries the slicer's estimate; `note` is
    one or two sentences of your own (spool too short, printer busy until when, warnings). Allowed from 'sliced'."""
    st = jobs.state(job_id)
    if st["state"] != "sliced":
        return {"refused": f"job is '{st['state']}'; announce comes right after a successful slice"}
    t = jobs.ticket(job_id)
    text = f"🧾 Queued [{job_id}]\n{_summary(t, st['slice_report'])}"
    if note.strip():
        text += f"\n{note.strip()[:600]}"
    text += f"\n/hold_{job_id} to keep it from starting, /drop_{job_id} to remove it."
    notify.send(text)
    jobs.transition(job_id, "queued")
    return {"announced": True, "next": "prepare is complete; stop here"}


def check_bed(job_id: str) -> dict:
    """Look at the printer's bed through its camera (moving the bed into view where the printer needs that).
    Returns verdict 'clear', 'occupied' or 'cannot_tell'. Allowed from 'queued' while the printer is idle."""
    st = jobs.state(job_id)
    if st["state"] != "queued":
        return {"refused": f"job is '{st['state']}'; the bed check runs from 'queued'"}
    if st.get("held"):
        return {"refused": "the operator put this job on hold"}
    t = jobs.ticket(job_id)
    cfg = config.printer(t["printer"])
    ahead = [j["id"] for j in jobs.active(t["printer"])
             if j["id"] != job_id and j["state"] in ("bed_ok", "uploaded", "awaiting_go")]
    if ahead:
        return {"refused": f"job {ahead[0]} is already being launched on {t['printer']}. {IDLE_HINT}"}
    try:
        if _macro(t) and t["bed"] == "any":
            if not moonraker.status(cfg)["idle"]:
                return {"refused": f"printer is not idle. {IDLE_HINT}"}
            jobs.transition(job_id, "bed_ok", bed={"verdict": "not_needed", "time": time.time(),
                                                   "reason": "this job does not need a clear bed"})
            return {"verdict": "not_needed", "reason": "this job does not need a clear bed", "next": "upload_job"}
        if st.get("vouched_until", 0) > time.time():
            if not moonraker.status(cfg)["idle"]:
                return {"refused": f"printer is not idle. {IDLE_HINT}"}
            jobs.transition(job_id, "bed_ok", bed={"verdict": "clear", "reason": "operator vouched", "time": time.time()})
            return {"verdict": "clear", "reason": "the operator vouched for the bed with /go", "next": "upload_job"}
        if not moonraker.status(cfg)["idle"]:
            return {"refused": f"printer is not idle. {IDLE_HINT}"}
        result = bedcheck.check(cfg, job_id)
    except moonraker.PrinterError as exc:
        return {"refused": f"printer unreachable: {exc}. {IDLE_HINT}"}
    result["time"] = time.time()
    if result["verdict"] == "clear":
        jobs.transition(job_id, "bed_ok", bed=result)
        return {"verdict": "clear", "reason": result["reason"], "next": "upload_job"}
    jobs.update(job_id, bed=result)
    return {"verdict": result["verdict"], "reason": result["reason"],
            "next": "do not upload or start; call escalate with this reason"}


def upload_job(job_id: str) -> dict:
    """Upload the verified G-code to the printer (it does not start). Allowed from 'bed_ok'."""
    st = jobs.state(job_id)
    if st["state"] != "bed_ok":
        return {"refused": f"job is '{st['state']}'; upload comes after a clear bed check"}
    t = jobs.ticket(job_id)
    if _macro(t):
        jobs.transition(job_id, "uploaded")
        return {"uploaded": "nothing to upload for a macro job", "next": "start_job"}
    try:
        stored = moonraker.upload(config.printer(t["printer"]), Path(st["slice_report"]["gcode"]), _gcode_name(t))
    except moonraker.PrinterError as exc:
        return {"refused": f"upload failed: {exc}"}
    jobs.transition(job_id, "uploaded", remote_file=stored)
    return {"uploaded": stored, "next": "start_job"}


def _start_gates(job_id: str) -> str | None:
    """Everything that must be true at the instant a print starts. Returns the reason it is not, or None."""
    st = jobs.state(job_id)
    if st.get("held"):
        return "the operator put this job on hold"
    bed = st.get("bed") or {}
    t = jobs.ticket(job_id)
    any_bed = _macro(t) and t["bed"] == "any"
    if not any_bed and (bed.get("verdict") != "clear" or time.time() - bed.get("time", 0) > config.BED_OK_TTL):
        return "the bed-clear verdict is missing or older than allowed"
    cfg = config.printer(t["printer"])
    live = moonraker.status(cfg)
    if not live["idle"]:
        return "printer is cooling down after the last print" if live.get("cooling") else f"printer is {live['state']}"
    spool = live.get("spool")
    if isinstance(spool, dict) and t.get("material") and spool["material"] and spool["material"].upper() != t["material"].upper():
        return f"loaded spool {spool['id']} is {spool['material']}, the job needs {t['material']}"
    return None


def start_job(job_id: str) -> dict:
    """Start the print, subject to the printer's start mode: 'auto' starts at once, 'veto' posts the bed photo
    and starts after a countdown unless the operator sends /hold, 'confirm' waits for the operator's /go.
    Allowed from 'uploaded'."""
    st = jobs.state(job_id)
    if st["state"] != "uploaded":
        return {"refused": f"job is '{st['state']}'; start comes after upload"}
    t = jobs.ticket(job_id)
    cfg = config.printer(t["printer"])
    try:
        why_not = _start_gates(job_id)
        if why_not:
            jobs.transition(job_id, "queued", reason=why_not)
            return {"refused": f"{why_not}. The job is back in the queue. {IDLE_HINT}"}
        mode = cfg.get("start_mode", "confirm")
        vouched = st.get("vouched_until", 0) > time.time()
        photo = (st.get("bed") or {}).get("frame")
        summary = _summary(t, st["slice_report"])
        seen = "Bed check: clear." if (st.get("bed") or {}).get("verdict") == "clear" else "No bed check: not needed."
        if mode == "confirm" and not vouched:
            notify.send(f"🟡 Ready [{job_id}]\n{summary}\n{seen} Send /go_{job_id} to start, "
                        f"/hold_{job_id} to keep it.", photo)
            jobs.transition(job_id, "awaiting_go")
            return {"started": False, "waiting_for": "the operator's /go", "next": "stop here"}
        if mode == "veto" and not vouched:
            wait = int(cfg.get("veto_sec", 120))
            notify.send(f"🟢 Starting [{job_id}] in {wait} s\n{summary}\n{seen} /hold_{job_id} stops it.", photo)
            deadline = time.time() + wait
            while time.time() < deadline:
                time.sleep(2)
                if jobs.state(job_id).get("held"):
                    break
            why_not = _start_gates(job_id)
            if why_not:
                jobs.transition(job_id, "queued", reason=why_not)
                return {"refused": f"{why_not}. The job is back in the queue. {IDLE_HINT}"}
        if not _macro(t):
            moonraker.start(cfg, st["remote_file"])
    except moonraker.PrinterError as exc:
        return {"refused": f"printer error: {exc}"}
    # A macro job has no file to start: the queue sends its commands once the job is 'printing'.
    jobs.transition(job_id, "printing", started=time.time())
    notify.send(f"▶️ Started [{job_id}] {t['title']} on {t['printer']}")
    return {"started": True, "next": "launch is complete; stop here"}


def escalate(job_id: str, reason: str) -> dict:
    """Stop work on the job and tell the operator why, with the last bed photo if there is one. Use this for
    anything you cannot resolve with the other tools. The job waits until the operator acts."""
    st = jobs.state(job_id)
    if st["state"] in ("printing", "done", "dropped"):
        return {"refused": f"job is '{st['state']}'"}
    t = jobs.ticket(job_id)
    photo = (st.get("bed") or {}).get("frame")
    notify.send(f"⚠️ Needs you [{job_id}] {t['title']} on {t['printer']}\n{reason.strip()[:800]}\n"
                f"/go_{job_id} = I checked, the bed is clear, carry on. /retry_{job_id} = run it again. "
                f"/drop_{job_id} = remove.", photo)
    jobs.transition(job_id, "needs_human", reason=reason.strip()[:800], resume=st["state"])
    return {"escalated": True, "next": "stop here"}


# name -> (function, JSON schema of its arguments). The MCP server and the docs are generated from this.
_JOB = {"type": "object", "properties": {"job_id": {"type": "string"}}, "required": ["job_id"]}
REGISTRY = {
    "get_job": (get_job, _JOB),
    "slice_job": (slice_job, _JOB),
    "printer_status": (printer_status, {"type": "object", "properties": {"printer": {"type": "string"}},
                                        "required": ["printer"]}),
    "announce_job": (announce_job, {"type": "object", "properties": {
        "job_id": {"type": "string"}, "note": {"type": "string"}}, "required": ["job_id"]}),
    "check_bed": (check_bed, _JOB),
    "upload_job": (upload_job, _JOB),
    "start_job": (start_job, _JOB),
    "escalate": (escalate, {"type": "object", "properties": {
        "job_id": {"type": "string"}, "reason": {"type": "string"}}, "required": ["job_id", "reason"]}),
}


def call(name: str, arguments: dict) -> dict:
    """Run a tool by name. Errors a caller can act on come back as {"refused": ...}, never as exceptions."""
    if name not in REGISTRY:
        return {"refused": f"unknown tool '{name}'"}
    try:
        return REGISTRY[name][0](**arguments)
    except (jobs.JobError, config.ConfigError, moonraker.PrinterError) as exc:
        return {"refused": str(exc)}
    except TypeError as exc:
        return {"refused": f"bad arguments for {name}: {exc}"}
