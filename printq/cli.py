"""Command line: submit jobs, run the daemon, and everything the operator can do from Telegram."""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import urllib.request
from pathlib import Path

from . import bedcheck, config, daemon, jobs, moonraker


def doctor() -> int:
    """Check the installation and say what is missing. Returns the number of problems."""
    bad = 0

    def report(ok: bool, text: str) -> None:
        nonlocal bad
        bad += not ok
        print(("ok    " if ok else "FAIL  ") + text)

    report(bool(shutil.which(config.ORCA_BIN)), f"OrcaSlicer binary: {config.ORCA_BIN}")
    report(bool(shutil.which(config.env("PRINTQ_AGENT_BIN", "claude"))), "agent harness (claude) on PATH")
    try:
        with urllib.request.urlopen(config.OLLAMA_URL + "/api/tags", timeout=5) as r:
            models = [m["name"] for m in json.load(r)["models"]]
        report(any(m.split(":latest")[0] == config.AGENT_MODEL or m == config.AGENT_MODEL for m in models),
               f"agent model {config.AGENT_MODEL} on {config.OLLAMA_URL}")
    except OSError as exc:
        report(False, f"model server {config.OLLAMA_URL}: {exc}")
    report(bool(config.env("PRINTQ_ANTHROPIC_API_KEY")), "PRINTQ_ANTHROPIC_API_KEY (bed-check vision judge)")
    report(bool(config.env("TELEGRAM_BOT_TOKEN") and config.env("TELEGRAM_CHAT_ID")),
           "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID (without them messages are only logged)")
    try:
        for name in config.printers():
            cfg = config.printer(name)
            try:
                st = moonraker.status(cfg)
                moonraker.snapshot(cfg)
                report(True, f"printer {name}: {st['state']}, camera ok, start mode {cfg.get('start_mode', 'confirm')}")
            except moonraker.PrinterError as exc:
                report(False, f"printer {name}: {exc}")
    except config.ConfigError as exc:
        report(False, str(exc))
    return bad


def main() -> None:
    ap = argparse.ArgumentParser(prog="printq", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("submit", help="queue a job from a ticket JSON file").add_argument("ticket")
    sub.add_parser("check", help="validate a ticket without queueing it").add_argument("ticket")
    bl = sub.add_parser("blank", help="print an empty ticket describing the current format and this site's choices")
    bl.add_argument("printer", nargs="?")
    bl.add_argument("--macro", action="store_true", help="a macro job: G-code commands, nothing to slice")
    sub.add_parser("daemon", help="run the queue")
    sub.add_parser("doctor", help="check the installation")
    sub.add_parser("queue", help="list jobs")
    ut = sub.add_parser("util", help="share of time each printer spent printing, and what the rest went on")
    ut.add_argument("days", nargs="?", default="1")
    ut.add_argument("printer", nargs="?", default="")
    for name, text in (("show", "one job's details"), ("hold", "keep a job from starting"),
                       ("release", "undo hold"), ("drop", "remove a job"), ("retry", "run the failed step again"),
                       ("go", "you checked the bed yourself: start it")):
        sub.add_parser(name, help=text).add_argument("job_id")
    bc = sub.add_parser("bedcheck", help="run the bed check on a printer now (may move the bed into view)")
    bc.add_argument("printer")
    args = ap.parse_args()

    try:
        if args.cmd in ("submit", "check"):
            ticket = json.loads(Path(args.ticket).read_text())
            if args.cmd == "check":
                print(json.dumps(jobs.validate_ticket(ticket), indent=1))
            else:
                print(jobs.submit(ticket))
        elif args.cmd == "blank":
            print(json.dumps(jobs.blank(args.printer, "macro" if args.macro else "print"), indent=1))
        elif args.cmd == "daemon":
            daemon.main()
        elif args.cmd == "doctor":
            sys.exit(1 if doctor() else 0)
        elif args.cmd == "bedcheck":
            print(json.dumps(bedcheck.check(config.printer(args.printer)), indent=1))
        elif args.cmd == "util":
            print(daemon.safe_command("util", f"{args.days} {args.printer}"))
        else:
            print(daemon.safe_command(args.cmd, getattr(args, "job_id", "")))
    except (jobs.JobError, config.ConfigError, moonraker.PrinterError, ValueError) as exc:
        sys.exit(f"printq: {exc}")


if __name__ == "__main__":
    main()
