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

from . import agent, bedcheck, config, jobs, macro, moonraker, notify, order, sessions, tools, utilization

TICK = 10
PREPARE_TRIES = 3  # runs of the prepare step before a job that never moved is handed to the operator
RETRY_AFTER = 120  # seconds before a launch that ended back in 'queued' is tried again


def command(cmd: str, arg: str) -> str:
    """Carry out one operator command and return the reply text. Shared by Telegram and the CLI."""
    job_id = arg.split()[0] if arg.split() else ""
    if cmd in ("queue", "list"):
        all_ = jobs.all_jobs()
        rows = [j for j in all_ if j["state"] in ("printing", "needs_human")]
        rows += [j for j in jobs.active() if j["state"] != "queued" or j.get("held")]
        for name in config.printers():  # queued jobs in the order they will run, not the order they came in
            rows += queue_order(name, all_)
        if not rows:
            return "The queue is empty."
        return "\n".join(f"[{j['id']}] {j['title']} on {j['printer']}: {j['state']}"
                         + (f", {order.describe(j)}" if order.needs(j) != (None, "") else "")
                         + (" (held)" if j.get("held") else "")
                         + (f" - {j['reason']}" if j["state"] == "needs_human" and j.get("reason") else "")
                         for j in rows)
    if cmd in ("util", "utilization"):
        words = arg.split()
        days = next((float(w) for w in words if w.replace(".", "", 1).isdigit()), 1.0)
        name = next((w for w in words if w in config.printers()), None)
        return utilization.report(days, name)
    if cmd == "dataset":
        return bedcheck.dataset_report()
    if cmd in ("help", "start"):
        return ("/queue, /util [DAYS] [PRINTER] (share of time printing, and what the rest went on), /show ID, /hold ID, /release ID, /drop ID, /retry ID (run the step again), "
                "/go ID (you checked the bed yourself: start it), /clear ID (the same, and the bed was already "
                "clear in the photo the check sent: recorded as the check's mistake), /occupied ID (the bed holds something: recorded for "
                "training, and stops a start the check allowed), /dataset (bed frames and labels collected), "
                "/success ID, /partial ID, /failure ID [NOTE] (how the part came out)")
    if not job_id:
        return f"/{cmd} needs a job id; /queue lists them."
    st = jobs.state(job_id)
    if cmd in sessions.OUTCOMES:
        return sessions.outcome(job_id, cmd, " ".join(arg.split()[1:]))
    if cmd in ("hold", "release", "drop", "retry", "go", "clear", "occupied"):
        sessions.note(job_id, "interventions", cmd, detail=f"while '{st['state']}'")
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
        jobs.update(job_id, held=False, next_try=0, waiting_bed="")
        return f"[{job_id}] released."
    if cmd == "drop":
        jobs.transition(job_id, "dropped", reason="dropped by operator")
        return f"[{job_id}] dropped."
    if cmd == "retry":
        if st["state"] != "needs_human":
            return f"[{job_id}] is '{st['state']}'; /retry is for jobs that asked for you."
        back = "submitted" if st.get("resume") in ("submitted", "sliced") else "queued"
        fresh = {"held": False, "next_try": 0, "reason": ""}
        if back == "submitted":
            # The prepare agent reads the job first; a report left over from the
            # failed slice makes it escalate again without slicing (j12).
            fresh["slice_report"] = None
        jobs.transition(job_id, back, **fresh)
        return f"[{job_id}] will run again from '{back}'."
    if cmd in ("go", "clear"):
        # The operator vouches for the bed. Recorded with a short lifetime, then the normal gates apply.
        vouch = {"vouched_until": time.time() + 600, "held": False, "next_try": 0,
                 "bed": {"verdict": "clear", "reason": "operator vouched", "time": time.time(),
                         "frame": (st.get("bed") or {}).get("frame")}}
        jobs.event(job_id, f"operator vouched for the bed (/go) while '{st['state']}'")
        if st["state"] in ("awaiting_go", "needs_human", "queued"):
            printer = jobs.ticket(job_id)["printer"]
            if cmd == "clear":
                # "It was already clear in that photo": ground truth for the frames of the last check.
                bedcheck.label_check(printer, st.get("bed"), "clear", "operator", job_id)
            try:  # either way the bed is clear now: a frame of it as it stands, wherever the bed is
                cfg = config.printer(printer)
                live = moonraker.status(cfg)["position"]
                shot = bedcheck.record(printer, moonraker.snapshot(cfg),
                                       {"job": job_id, "x": live[0], "y": live[1], "z": live[2]})
                bedcheck.label(printer, "clear", "operator", frame=str(shot), job=job_id, note=f"/{cmd}")
            except (moonraker.PrinterError, config.ConfigError):
                pass
        if st["state"] == "awaiting_go":
            jobs.transition(job_id, "uploaded", **vouch)
            out = tools.start_job(job_id)
            return f"[{job_id}] " + ("starting." if out.get("started") else out.get("refused", str(out)))
        if st["state"] == "needs_human" and st.get("resume") not in ("submitted", "sliced"):
            jobs.transition(job_id, "queued", **vouch, reason="")
            return f"[{job_id}] noted, launching."
        if st["state"] == "queued":
            jobs.update(job_id, **vouch)
            ended = _end_cooldown(jobs.ticket(job_id)["printer"], job_id)
            if ended:
                return f"[{job_id}] noted. {ended} It starts within a minute."
            return (f"[{job_id}] noted; it starts within a minute if the printer is idle, and otherwise as soon "
                    "as it is, for the next 10 minutes.")
        return f"[{job_id}] is '{st['state']}'; /go does not apply."
    if cmd == "occupied":
        # The operator says the bed holds something. Ground truth for the last check's frames; and where
        # the judge had called that bed clear, the job must not start on its word.
        bed = st.get("bed") or {}
        if not bedcheck.label_check(jobs.ticket(job_id)["printer"], bed, "occupied", "operator", job_id):
            return f"[{job_id}] has no bed check to label."
        jobs.event(job_id, "operator says the bed is occupied (/occupied)")
        if bed.get("verdict") == "clear" and st["state"] in ("queued", "bed_ok", "uploaded", "awaiting_go"):
            jobs.update(job_id, held=True, vouched_until=0)
            return (f"[{job_id}] recorded: the bed was occupied and the check called it clear. The job is on "
                    f"hold; /release_{job_id} once the bed is cleared.")
        return f"[{job_id}] recorded: the bed is occupied."
    return f"Unknown command /{cmd}. /help lists them."


def _end_cooldown(printer: str, for_job: str) -> str:
    """The operator vouched for the bed while the last print's cool-down is still running: the part is
    off, so there is nothing left to cool slowly, and waiting would only let the bed go cold before it is
    heated again. Stop the cool-down, keep the bed at the temperature it has, and let the queue go on.
    Returns what was done, or '' when no cool-down was running."""
    cfg = config.printer(printer)
    macro_name = cfg.get("cooldown_macro")
    try:
        live = moonraker.status(cfg)
        if not (macro_name and live.get("cooling")):
            return ""
        temperature, target = live["bed"]
        moonraker.gcode(cfg, f"{macro_name} CANCEL=1")
        keep = int(min(temperature, target) if target else 0)
        if keep > 0:
            moonraker.gcode(cfg, f"SET_HEATER_TEMPERATURE HEATER=heater_bed TARGET={keep}")
    except (moonraker.PrinterError, KeyError, TypeError, ValueError) as exc:
        return f"The cool-down could not be stopped ({exc}); the job waits for it to end."
    for job in jobs.all_jobs():
        if job["printer"] == printer and job["state"] == "printing":
            jobs.update(job["id"], cooldown_cancelled=time.time())
            jobs.event(job["id"], f"cool-down ended early by the operator's go-ahead for {for_job}")
            sessions.note(job["id"], "interventions", "cooldown cancelled", detail=f"go-ahead for {for_job}")
    return f"The cool-down on {printer} is stopped and the bed is held at {keep} °C."


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


def _launch(job_id: str) -> dict:
    """The launch phase: look at the bed, upload, start. Three steps in a fixed order, each behind its own
    gate, so the queue takes them itself. Handing them to the model added only ways to fail: on 2026-11-26
    it answered three launches in a row with its tool calls written out as text and "Job started", doing
    nothing, until the operator's /go had expired. PRINTQ_AGENT_LAUNCH=1 gives the launch back to the agent."""
    if config.env("PRINTQ_AGENT_LAUNCH") == "1":
        return agent.run("launch", job_id)
    said = []
    for step in (tools.check_bed, tools.upload_job, tools.start_job):
        out = step(job_id)
        said.append(f"{step.__name__}: {out.get('refused') or out.get('reason') or out.get('next') or out}")
        if "refused" in out or out.get("verdict") in ("occupied", "cannot_tell") or out.get("started") is False:
            break
    state = jobs.state(job_id)["state"]
    jobs.event(job_id, f"launch: ended in '{state}' ({'; '.join(said)[:300]})")
    return {"state": state, "ok": True, "text": "; ".join(said)}


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
    if (live["state"] == "paused") != bool(job.get("paused")):
        jobs.update(job["id"], paused=live["state"] == "paused")
        sessions.note(job["id"], "anomalies", "paused" if live["state"] == "paused" else "resumed", by="printer")
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
            sessions.mark(job["id"], extrusion_end=True)
            notify.send(f"🧊 [{job['id']}] {t['title']} on {t['printer']}: print extrusion complete. Entering "
                        f"cooldown phase, about {live.get('cooling_min_left', '?')} min. Do not clear the bed yet.")
        return
    if finished:
        bed = jobs.state(job["id"]).get("bed") or {}
        if bed.get("verdict") == "clear" and bed.get("check"):
            # It printed to the end on the bed the check called clear: weak evidence that it was.
            bedcheck.label_check(t["printer"], bed, "clear", "print_completed", job["id"])
        jobs.transition(job["id"], "done")
        sessions.mark(job["id"], metrics_end=True, printer_result="completed")
        if job.get("cooldown_cancelled"):
            # The operator took the part off and ended the cool-down: the bed is not occupied, so no
            # "clear the bed" message and no "occupied" frame for the dataset.
            notify.send(f"✅ [{job['id']}] {t['title']} on {t['printer']}: done; the cool-down was ended early "
                        f"at your go-ahead.\nHow did the part come out? /success_{job['id']} "
                        f"/partial_{job['id']} /failure_{job['id']} (kept with the print's record)")
            return
        notify.send(f"✅ [{job['id']}] {t['title']} on {t['printer']}: print is ready to be cleared from the "
                    "build plate. The bed needs clearing before the next job.\nHow did the part come out? "
                    f"/success_{job['id']} /partial_{job['id']} /failure_{job['id']} (kept with the print's record)")
        try:  # a frame of a bed that certainly holds a part: free training data
            shot = bedcheck.record(t["printer"], moonraker.snapshot(cfg),
                                   {"label_hint": "occupied", "job": job["id"], "z": live["position"][2],
                                    "last_file": live.get("file") or job.get("remote_file")})
            bedcheck.label(t["printer"], "occupied", "print_end", frame=str(shot), job=job["id"])
        except moonraker.PrinterError:
            pass
    else:
        jobs.transition(job["id"], "needs_human", reason=f"print ended in printer state '{live['state']}'")
        sessions.note(job["id"], "anomalies", f"ended in printer state '{live['state']}'", by="printer")
        sessions.mark(job["id"], metrics_end=True, printer_result=live["state"])
        notify.send(f"⚠️ [{job['id']}] {t['title']}: the print ended in state '{live['state']}'.")


def last_printed(printer: str, all_: list[dict]) -> dict | None:
    """The job that started most recently on this printer: what it needed is probably still loaded."""
    ran = [j for j in all_ if j["printer"] == printer and j.get("started") and j.get("kind") != "macro"]
    return max(ran, key=lambda j: j["started"]) if ran else None


def queue_order(printer: str, all_: list[dict], spool="ask") -> list[dict]:
    """The printer's queued jobs that may run, in running order (see order.py). Held jobs step aside."""
    queued = [j for j in all_ if j["printer"] == printer and j["state"] == "queued" and not j.get("held")]
    if spool == "ask":
        spool = None
        if any(order.needs(j) != (None, "") for j in queued):  # only then is the printer worth asking
            try:
                spool = moonraker.spool(config.printer(printer))
            except config.ConfigError:
                pass
    return order.arrange(queued, spool, last_printed(printer, all_))


def _spool_ready(job: dict, all_: list[dict]) -> bool:
    """Before the bed is moved for a job: is the filament it asks for on the printer? If not, ask the
    operator for the spool, once, and look again every minute. With the queue grouped by filament this is
    asked only when nothing that is waiting can use the loaded spool."""
    cfg = config.printer(job["printer"])
    spool = moonraker.spool(cfg)
    why = tools.spool_problem(job["id"], spool)
    fid, material = order.needs(job)
    if why is None and not isinstance(spool, dict) and fid is not None and job.get("vouched_until", 0) <= time.time():
        # Nothing to check against. Assume the spool of the last print is still there; anything else, ask.
        last = last_printed(job["printer"], all_)
        if last is None or order.needs(last) != order.needs(job):
            why = f"no spool is recorded as loaded on {job['printer']}, and the job needs {order.describe(job)}"
    if why is None:
        if job.get("waiting_spool"):
            jobs.update(job["id"], waiting_spool="")
        return True
    if job.get("waiting_spool") != why:
        same = sum(1 for j in all_ if j["printer"] == job["printer"] and j["state"] == "queued"
                   and order.needs(j) == order.needs(job))
        notify.send(f"🧵 Spool change [{job['id']}] {job['title']} on {job['printer']}\n{why}.\n"
                    f"Load {order.describe(job)} and make it the active spool"
                    + (f"; {same} queued jobs use it" if same > 1 else "") + ". The job goes ahead by itself once "
                    f"it is loaded. /go_{job['id']} = print with what is loaded now. /hold_{job['id']} = let the "
                    "next job go first.")
        jobs.event(job["id"], f"waiting for a spool: {why}")
    jobs.update(job["id"], waiting_spool=why, next_try=time.time() + 60)
    return False


def tick() -> None:
    all_ = jobs.all_jobs()
    launching = {j["printer"] for j in all_ if j["state"] in ("bed_ok", "uploaded", "awaiting_go", "printing")}
    heads: dict[str, str] = {}  # printer -> the queued job it takes next
    for job in all_:
        try:
            if job["state"] == "submitted":
                result = agent.run("prepare", job["id"])
                if result["state"] == "submitted" and job.get("prepare_tries", 0) + 1 < PREPARE_TRIES:
                    # The agent ended without moving the job at all. Small models sometimes write their
                    # tool calls out as text instead of making them; nothing happened, so run it again.
                    jobs.update(job["id"], prepare_tries=job.get("prepare_tries", 0) + 1)
                    jobs.event(job["id"], "prepare did nothing; trying again")
                elif not result["ok"]:
                    jobs.update(job["id"], prepare_tries=0)
                    _force_escalate(job["id"], "prepare", result)
            elif job["state"] == "printing":
                _watch_printing(job)
            elif job["state"] in ("bed_ok", "uploaded") and time.time() - job["updated"] > 1800:
                # a launch that died half-way; put it back rather than leave it blocking the printer
                jobs.transition(job["id"], "queued", reason="launch did not finish")
            elif job["state"] == "queued":
                if job.get("held") or job["printer"] in launching:
                    continue  # a held job steps aside: the next one for that printer may go
                if job["printer"] not in heads:
                    ordered = queue_order(job["printer"], all_)
                    heads[job["printer"]] = ordered[0]["id"] if ordered else ""
                if heads[job["printer"]] != job["id"] or job.get("next_try", 0) > time.time():
                    continue
                if not moonraker.status(config.printer(job["printer"]))["idle"]:
                    continue
                if not _spool_ready(job, all_):
                    continue
                result = _launch(job["id"])
                launching.add(job["printer"])
                if result["state"] in ("bed_ok", "uploaded"):
                    jobs.transition(job["id"], "queued", reason="launch stopped half-way")
                    result["state"] = "queued (after stopping half-way)"
                    _force_escalate(job["id"], "launch", result)
                elif result["state"] == "queued" and jobs.state(job["id"]).get("next_try", 0) <= time.time():
                    jobs.update(job["id"], next_try=time.time() + RETRY_AFTER)  # unless the bed check set its own
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
    threading.Thread(target=utilization.loop, daemon=True, name="utilization").start()
    while True:
        tick()
        time.sleep(TICK)
