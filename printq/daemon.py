"""The queue runner and the operator's commands.

The daemon owns the waiting. It runs the agent for a short, bounded phase
when there is something to do (a new job to prepare, an idle printer to
launch on), checks afterwards that the job ended where that phase may leave
it, and tells the operator when it did not.
"""
from __future__ import annotations

import threading
import time
import traceback

from . import agent, bedcheck, config, jobs, macro, moonraker, notify, tools

TICK = 10
RETRY_AFTER = 120  # seconds before a launch that ended back in 'queued' is tried again


def command(cmd: str, arg: str) -> str:
    """Carry out one operator command and return the reply text. Shared by Telegram and the CLI."""
    job_id = arg.split()[0] if arg.split() else ""
    if cmd in ("queue", "list"):
        rows = jobs.active() + [j for j in jobs.all_jobs() if j["state"] in ("printing", "needs_human")]
        if not rows:
            return "The queue is empty."
        return "\n".join(f"[{j['id']}] {j['title']} on {j['printer']}: {j['state']}"
                         + (" (held)" if j.get("held") else "")
                         + (f" - {j['reason']}" if j["state"] == "needs_human" and j.get("reason") else "")
                         for j in rows)
    if cmd in ("help", "start"):
        return ("/queue, /show ID, /hold ID, /release ID, /drop ID, /retry ID (run the step again), "
                "/go ID (you checked the bed yourself: start it)")
    if not job_id:
        return f"/{cmd} needs a job id; /queue lists them."
    st = jobs.state(job_id)
    if cmd == "show":
        t = jobs.ticket(job_id)
        est = (st.get("slice_report") or {}).get("estimate", {})
        return (f"[{job_id}] {t['title']} on {t['printer']}: {st['state']}"
                + (f"\n{est.get('filament_g')} g, {est.get('time')}" if est else "")
                + (f"\n{st.get('reason')}" if st.get("reason") else ""))
    if cmd == "hold":
        jobs.update(job_id, held=True)
        jobs.event(job_id, "held by operator")
        return f"[{job_id}] on hold. /release_{job_id} lets it run."
    if cmd == "release":
        jobs.update(job_id, held=False, next_try=0)
        return f"[{job_id}] released."
    if cmd == "drop":
        jobs.transition(job_id, "dropped", reason="dropped by operator")
        return f"[{job_id}] dropped."
    if cmd == "retry":
        if st["state"] != "needs_human":
            return f"[{job_id}] is '{st['state']}'; /retry is for jobs that asked for you."
        back = "submitted" if st.get("resume") in ("submitted", "sliced") else "queued"
        jobs.transition(job_id, back, held=False, next_try=0, reason="")
        return f"[{job_id}] will run again from '{back}'."
    if cmd == "go":
        # The operator vouches for the bed. Recorded with a short lifetime, then the normal gates apply.
        vouch = {"vouched_until": time.time() + 600, "held": False, "next_try": 0,
                 "bed": {"verdict": "clear", "reason": "operator vouched", "time": time.time(),
                         "frame": (st.get("bed") or {}).get("frame")}}
        jobs.event(job_id, f"operator vouched for the bed (/go) while '{st['state']}'")
        if st["state"] == "awaiting_go":
            jobs.transition(job_id, "uploaded", **vouch)
            out = tools.start_job(job_id)
            return f"[{job_id}] " + ("starting." if out.get("started") else out.get("refused", str(out)))
        if st["state"] == "needs_human" and st.get("resume") not in ("submitted", "sliced"):
            jobs.transition(job_id, "queued", **vouch, reason="")
            return f"[{job_id}] noted, launching."
        if st["state"] == "queued":
            jobs.update(job_id, **vouch)
            return f"[{job_id}] noted; it starts as soon as the printer is idle (within 10 minutes of now)."
        return f"[{job_id}] is '{st['state']}'; /go does not apply."
    return f"Unknown command /{cmd}. /help lists them."


def safe_command(cmd: str, arg: str) -> str:
    try:
        return command(cmd, arg)
    except (jobs.JobError, config.ConfigError, moonraker.PrinterError) as exc:
        return str(exc)


def telegram_loop() -> None:
    offset = 0
    while True:
        try:
            offset, commands = notify.poll(offset)
            for cmd, arg in commands:
                notify.send(safe_command(cmd, arg))
        except Exception as exc:  # noqa: BLE001
            notify.log(f"telegram poll failed: {exc}")
            time.sleep(15)


def _force_escalate(job_id: str, phase: str, result: dict) -> None:
    said = f' The agent said: "{result["text"][:300]}"' if result["text"] else ""
    out = tools.escalate(job_id, f"The {phase} step stopped with the job in '{result['state']}'.{said}")
    if "refused" in out:
        notify.log(f"[{job_id}] could not escalate: {out['refused']}")


RUNNING: dict[str, threading.Thread] = {}  # macro jobs this process is sending


def _run_macro(job: dict) -> None:
    """Send a started macro job's commands, once. They take minutes, so not on the queue's own thread."""
    job_id = job["id"]
    job = {**job, **jobs.state(job_id)}  # fresh: the runner may have finished since the queue was listed
    if job["state"] != "printing":
        return
    if job_id in RUNNING:
        if RUNNING[job_id].is_alive():
            return
        del RUNNING[job_id]
    if job.get("macro_started"):
        # Marked as begun, but not by this process: the queue was restarted half-way through.
        reason = "the queue restarted while this macro job was running; how far it got is unknown"
        jobs.transition(job_id, "needs_human", reason=reason, resume="queued", macro_started=0)
        notify.send(f"⚠️ Needs you [{job_id}] {job['title']} on {job['printer']}\n{reason}. Look at the printer, "
                    f"then /retry_{job_id} = run all of it again, /drop_{job_id} = remove.")
        return
    jobs.update(job_id, macro_started=time.time())

    def work() -> None:
        try:
            macro.run(job_id)
        except Exception:  # noqa: BLE001
            notify.log(f"[{job_id}] {traceback.format_exc()}")
            jobs.transition(job_id, "needs_human", reason="the macro runner failed; see notify.log",
                            resume="queued", macro_started=0)

    RUNNING[job_id] = threading.Thread(target=work, daemon=True, name=f"macro-{job_id}")
    RUNNING[job_id].start()


def _watch_printing(job: dict) -> None:
    if job.get("kind") == "macro":
        return _run_macro(job)
    t = jobs.ticket(job["id"])
    cfg = config.printer(t["printer"])
    live = moonraker.status(cfg)
    if live["state"] in ("printing", "paused") or time.time() - job.get("started", 0) < 90:
        return
    # Some printers clear the file at the end and report 'standby', never 'complete': ask the job history.
    finished = live["state"] == "complete" or (live["state"] == "standby"
                                               and moonraker.last_job_status(cfg) == "completed")
    if finished and live["cooling"]:
        # Extrusion is over; the part is not ready. The job stays 'printing', which keeps the printer taken.
        if not job.get("cooling"):
            jobs.update(job["id"], cooling=time.time())
            jobs.event(job["id"], "extrusion complete, cooling down")
            notify.send(f"🧊 [{job['id']}] {t['title']} on {t['printer']}: print extrusion complete. Entering "
                        f"cooldown phase, about {live.get('cooling_min_left', '?')} min. Do not clear the bed yet.")
        return
    if finished:
        jobs.transition(job["id"], "done")
        notify.send(f"✅ [{job['id']}] {t['title']} on {t['printer']}: print is ready to be cleared from the "
                    "build plate. The bed needs clearing before the next job.")
        try:  # a frame of a bed that certainly holds a part: free training data
            bedcheck.record(t["printer"], moonraker.snapshot(cfg),
                            {"label_hint": "occupied", "job": job["id"], "z": live["position"][2]})
        except moonraker.PrinterError:
            pass
    else:
        jobs.transition(job["id"], "needs_human", reason=f"print ended in printer state '{live['state']}'")
        notify.send(f"⚠️ [{job['id']}] {t['title']}: the print ended in state '{live['state']}'.")


def tick() -> None:
    all_ = jobs.all_jobs()
    launching = {j["printer"] for j in all_ if j["state"] in ("bed_ok", "uploaded", "awaiting_go", "printing")}
    seen_queued = set()
    for job in all_:
        try:
            if job["state"] == "submitted":
                result = agent.run("prepare", job["id"])
                if not result["ok"]:
                    _force_escalate(job["id"], "prepare", result)
            elif job["state"] == "printing":
                _watch_printing(job)
            elif job["state"] in ("bed_ok", "uploaded") and time.time() - job["updated"] > 1800:
                # a launch that died half-way; put it back rather than leave it blocking the printer
                jobs.transition(job["id"], "queued", reason="launch did not finish")
            elif job["state"] == "queued":
                if job.get("held"):
                    continue  # a held job steps aside: the next one for that printer may go
                first = job["printer"] not in seen_queued
                seen_queued.add(job["printer"])
                if not first or job["printer"] in launching or job.get("next_try", 0) > time.time():
                    continue
                if not moonraker.status(config.printer(job["printer"]))["idle"]:
                    continue
                result = agent.run("launch", job["id"])
                launching.add(job["printer"])
                if result["state"] in ("bed_ok", "uploaded"):
                    jobs.transition(job["id"], "queued", reason="launch stopped half-way")
                    result["state"] = "queued (after stopping half-way)"
                    _force_escalate(job["id"], "launch", result)
                elif result["state"] == "queued":
                    jobs.update(job["id"], next_try=time.time() + RETRY_AFTER)
        except moonraker.PrinterError as exc:
            notify.log(f"[{job['id']}] printer unreachable: {exc}")
        except Exception:  # noqa: BLE001 - one bad job must not stop the queue
            notify.log(f"[{job['id']}] {traceback.format_exc()}")


def main() -> None:
    config.JOBS.mkdir(parents=True, exist_ok=True)
    notify.log(f"printq daemon: data in {config.DATA}, agent model {config.AGENT_MODEL}")
    if config.env("TELEGRAM_BOT_TOKEN") and config.env("TELEGRAM_CHAT_ID"):
        threading.Thread(target=telegram_loop, daemon=True, name="telegram").start()
    else:
        notify.log("no TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID: messages are logged only; use the CLI for commands")
    while True:
        tick()
        time.sleep(TICK)
