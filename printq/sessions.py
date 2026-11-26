"""One record per print: what was printed, with what, when, how it ended and what anyone did to it.

A model that is to learn from prints needs each one described the same way, with times that can be
joined to whatever else was being measured (temperatures, camera frames) and an outcome somebody stated.
The record is written when the print starts and finished when it ends:

    session_id      "<printer>_<YYYYMMDD>_<HHMMSS>", local time of the start
    filament, profile, settings, gcode_hash, model_sha256, estimate
    bed_check       the check that let it start: its id in the dataset, the verdict, who gave it
    metrics_start, extrusion_end, metrics_end      UTC, ISO 8601
    printer_result  how the printer says it ended
    outcome         success | failure | partial, as the operator judged the part; None until they say
    anomalies       things that happened to the print, with times (a pause, an abnormal end)
    interventions   things people or the queue did, with times (hold, go, clear, occupied, ...)

Nothing here decides anything. Records live in $PRINTQ_DATA/sessions/, one JSON file each.
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from . import config, jobs, moonraker

OUTCOMES = ("success", "failure", "partial")


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _folder() -> Path:
    return config.DATA / "sessions"


def _path(session_id: str) -> Path:
    return _folder() / f"{session_id}.json"


def _load(job_id: str) -> dict | None:
    sid = jobs.state(job_id).get("session_id")
    try:
        return json.loads(_path(sid).read_text()) if sid else None
    except (OSError, ValueError):
        return None


def _save(rec: dict) -> None:
    _folder().mkdir(parents=True, exist_ok=True)
    tmp = _path(rec["session_id"]).with_suffix(".tmp")
    tmp.write_text(json.dumps(rec, indent=1) + "\n")
    tmp.replace(_path(rec["session_id"]))


def note(job_id: str, kind: str, what: str, by: str = "operator", detail: str = "") -> None:
    """Record an intervention or an anomaly (`kind`) against a job. Before the print starts it is kept
    with the job and copied into the record when that is opened; afterwards it goes straight in."""
    entry = {"t": now(), "what": what, "by": by, **({"detail": detail} if detail else {})}
    st = jobs.state(job_id)
    jobs.update(job_id, **{kind: [*st.get(kind, []), entry]})
    rec = _load(job_id)
    if rec:
        rec[kind].append(entry)
        _save(rec)


def open_(job_id: str) -> str | None:
    """Start the record for a print that has just been started. Never raises: a record that cannot be
    written must not stop a print."""
    try:
        t, st = jobs.ticket(job_id), jobs.state(job_id)
        if t.get("kind", "print") != "print":
            return None
        report = st.get("slice_report") or {}
        settings = report.get("settings") or {}
        sid = f"{t['printer'].replace('-', '')}_{time.strftime('%Y%m%d_%H%M%S')}"
        gcode = Path(report.get("gcode", ""))
        bed = st.get("bed") or {}
        clean = lambda v: v.strip('"') if isinstance(v, str) else v  # noqa: E731
        rec = {
            "session_id": sid, "job": job_id, "printer": t["printer"], "title": t["title"],
            "filament": {"type": settings.get("filament_type") or t.get("material", ""),
                         "preset": clean(settings.get("filament_settings_id")),
                         "spool": moonraker.spool(config.printer(t["printer"]))},
            "profile": {"preset_set": Path(t.get("presets", "")).name,
                        "printer": clean(settings.get("printer_settings_id")),
                        "process": clean(settings.get("print_settings_id"))},
            "settings": settings, "overrides": t.get("overrides", {}),
            "gcode_hash": "sha256:" + hashlib.sha256(gcode.read_bytes()).hexdigest() if gcode.is_file() else None,
            "model_sha256": t.get("model_sha256"), "estimate": report.get("estimate"),
            "bed_check": {"check": bed.get("check"), "verdict": bed.get("verdict"),
                          "by": "operator" if bed.get("reason") == "operator vouched" else "camera",
                          "frame": bed.get("frame")},
            "metrics_start": now(), "extrusion_end": None, "metrics_end": None,
            "printer_result": None, "outcome": None, "outcome_note": "",
            "anomalies": list(st.get("anomalies", [])), "interventions": list(st.get("interventions", [])),
        }
        _save(rec)
        jobs.update(job_id, session_id=sid)
        return sid
    except Exception:  # noqa: BLE001
        return None


def mark(job_id: str, **fields) -> None:
    """Set fields of a job's record, where it has one. A time field given as True is set to now."""
    try:
        rec = _load(job_id)
        if rec:
            rec.update({k: now() if v is True else v for k, v in fields.items()})
            _save(rec)
    except Exception:  # noqa: BLE001
        pass


def outcome(job_id: str, value: str, text: str = "") -> str:
    if value not in OUTCOMES:
        return f"The outcome is one of: {', '.join(OUTCOMES)}."
    rec = _load(job_id)
    if not rec:
        return f"[{job_id}] has no print record (it was not started by the queue, or not yet)."
    rec.update(outcome=value, outcome_note=text, outcome_time=now())
    _save(rec)
    return f"[{job_id}] recorded as {value}" + (f": {text}" if text else "") + "."
