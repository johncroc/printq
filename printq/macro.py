"""Run a macro job: a list of G-code commands or macros, with nothing to slice.

A macro job goes through the same queue and the same gates as a print (idle printer, bed check unless
the ticket says the bed does not matter, the printer's start mode). What differs is the end: there is
no file to start, so the queue sends the commands itself, one at a time, and reports what the printer
printed to its console.

Klipper keeps running a command after an HTTP client stops waiting for it. A command that outlasts
`timeout_sec` is therefore reported as stuck, not cancelled: the operator decides what to do.
"""
from __future__ import annotations

import time

from . import config, jobs, moonraker, notify

RESTARTS = ("SAVE_CONFIG", "RESTART", "FIRMWARE_RESTART")
READY_WAIT, READY_POLL = 180, 3  # seconds to wait for Klipper after a restart, and between looks


def _wait_ready(cfg: dict) -> None:
    deadline = time.time() + READY_WAIT
    while time.time() < deadline:
        time.sleep(READY_POLL)  # also gives the restart time to begin
        try:
            if moonraker.klipper_state(cfg) == "ready":
                return
        except moonraker.PrinterError:
            pass
    raise moonraker.PrinterError(f"Klipper was not ready {READY_WAIT} s after the restart")


def _console(cfg: dict, since: float) -> str:
    try:
        lines = [x for x in moonraker.responses(cfg, since - 5) if x.strip()]
    except (moonraker.PrinterError, KeyError):
        return ""
    return "\n".join(lines[-12:])[-900:]


def run(job_id: str) -> None:
    """Send the job's commands in order and move the job to 'done' or 'needs_human'."""
    t = jobs.ticket(job_id)
    cfg = config.printer(t["printer"])
    began, sent, failed = time.time(), "", ""
    try:
        for sent in t["gcode"]:
            jobs.event(job_id, f"macro: {sent}")
            if sent.split()[0].upper() in RESTARTS:
                try:
                    moonraker.gcode(cfg, sent, timeout=30)
                except moonraker.PrinterError:
                    pass  # Klipper drops the connection as it goes down
                _wait_ready(cfg)
            else:
                moonraker.gcode(cfg, sent, timeout=t["timeout_sec"])
    except moonraker.PrinterError as exc:
        failed = str(exc)
    if failed or t["heaters_off"]:
        try:
            moonraker.gcode(cfg, "TURN_OFF_HEATERS", timeout=20)
        except moonraker.PrinterError:
            pass
    said = _console(cfg, began)
    said = f"\nThe printer said:\n{said}" if said else ""
    took = time.strftime("%H:%M:%S", time.gmtime(time.time() - began))
    if failed:
        reason = f"'{sent}' failed: {failed}"
        jobs.transition(job_id, "needs_human", reason=reason, resume="queued", macro_started=0)
        notify.send(f"⚠️ Needs you [{job_id}] {t['title']} on {t['printer']}\n{reason}\n"
                    "If it timed out, the printer may still be running that command. Heaters were told to turn off."
                    f"{said}\n/retry_{job_id} = run all of it again. /drop_{job_id} = remove.")
    else:
        jobs.transition(job_id, "done")
        notify.send(f"✅ Finished [{job_id}] {t['title']} on {t['printer']} in {took}.{said}")
