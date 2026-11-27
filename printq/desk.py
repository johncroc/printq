"""The print desk: one page where a model becomes a job.

Three ways in, one confirmation:

    a model dropped on the page                (a download)
    a model exported into the watched folder   (the CAD program's export)
    G-code sent by the slicer's own Print      (the slicer's "print host" is this service)

The first two become drafts. A draft is only a file and a question: which
printer, which filament, which settings. The page answers with what is known
(the settings this model printed with last time, or the printer's last job
in that material), slices a preview so that the slicer's own estimate is on
screen, and only then offers Print. Print writes an ordinary ticket and
submits it, so every gate of the queue applies exactly as for any other job.

G-code sent with the slicer's Print button is queued directly: pressing
Print there is the confirmation. Sent with Upload, it waits here as a draft.

Standard library only. Listens on localhost unless PRINTQ_DESK_BIND says
otherwise; nothing here can start a print, it can only submit a ticket.
"""
from __future__ import annotations

import email
import email.policy
import hmac
import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import config, daemon, jobs, moonraker, notify, order, slicer

MODELS = (".stl", ".3mf", ".step", ".stp")
GCODE = (".gcode", ".gco", ".g")
PAGE = Path(__file__).with_name("desk.html")
LOCK = threading.RLock()
PAGE_SEEN = 0.0  # when a browser last asked for the state: is the page open somewhere?


def drafts_dir() -> Path:
    return config.DATA / "desk"


def port() -> int:
    return int(config.env("PRINTQ_DESK_PORT", "8766"))


# ---------------------------------------------------------------- drafts

def _draft_path(draft_id: str) -> Path:
    if not re.fullmatch(r"d\d+", draft_id or ""):
        raise jobs.JobError(f"invalid draft id '{draft_id}'")
    path = drafts_dir() / draft_id
    if not (path / "draft.json").is_file():
        raise jobs.JobError(f"no such draft '{draft_id}'")
    return path


def load(draft_id: str) -> dict:
    return json.loads((_draft_path(draft_id) / "draft.json").read_text())


def save(draft: dict) -> dict:
    path = drafts_dir() / draft["id"] / "draft.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(draft, indent=1))
    os.replace(tmp, path)
    return draft


def all_drafts(status: str | None = "open") -> list[dict]:
    out = []
    if drafts_dir().is_dir():
        for path in drafts_dir().iterdir():
            try:
                d = json.loads((path / "draft.json").read_text())
            except (OSError, ValueError):
                continue
            if status is None or d.get("status") == status:
                out.append(d)
    return sorted(out, key=lambda d: int(d["id"][1:]))


def new_draft(source: str, name: str, path: Path | None = None, data: bytes | None = None, **extra) -> dict:
    """Create a draft for a file. `data` is stored in the draft's own directory; `path` is used in place
    (an export stays where the CAD program wrote it)."""
    with LOCK:
        drafts_dir().mkdir(parents=True, exist_ok=True)
        taken = [int(p.name[1:]) for p in drafts_dir().iterdir() if re.fullmatch(r"d\d+", p.name)]
        draft_id = f"d{max(taken, default=0) + 1}"
        home = drafts_dir() / draft_id
        home.mkdir()
        if data is not None:
            path = home / name
            path.write_bytes(data)
        draft = {"id": draft_id, "source": source, "name": name, "path": str(path), "sha256": jobs.sha256(path),
                 "kind": "gcode" if path.suffix.lower() in GCODE else "print", "status": "open",
                 "created": time.strftime("%Y-%m-%d %H:%M:%S"), **extra}
        if draft["kind"] == "gcode":
            summary, used = slicer.read_gcode(path)
            draft["estimate"] = summary
            draft["material"] = slicer._norm(used.get("filament_type", "")).split(",")[0].split(";")[0]
        return save(draft)


def safe_name(name: str, kinds: tuple) -> str:
    name = Path(urllib.parse.unquote(name or "")).name.strip()
    name = re.sub(r"[^\w .()+,@=-]", "_", name)[:120].strip(" .")
    if not name or Path(name).suffix.lower() not in kinds:
        raise jobs.JobError(f"'{name or 'that file'}' is not one of: {', '.join(kinds)}")
    return name


def history(draft: dict) -> dict | None:
    """What the queue remembers about this model: the last job that printed it and how."""
    if draft["kind"] != "print":
        return None
    found = jobs.last_print_of(draft["sha256"], draft["path"], draft["name"])
    if not found:
        return None
    t, st = found["ticket"], found["state"]
    est = (st.get("slice_report") or {}).get("estimate") or {}
    return {"match": found["match"], "job": t["id"], "printer": t["printer"], "when": t.get("submitted", "")[:10],
            "state": st["state"], "presets": t.get("presets", ""), "overrides": t.get("overrides", {}),
            "material": t.get("material", ""), "filament": t.get("filament") or {}, "copies": t.get("copies", 1),
            "estimate": {"time": est.get("time"), "filament_g": est.get("filament_g")}}


# ---------------------------------------------------------------- what the page needs to know

_CACHE: dict[str, tuple[float, object]] = {}


def cached(key: str, seconds: float, make):
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < seconds:
        return hit[1]
    value = make()
    _CACHE[key] = (time.time(), value)
    return value


def _json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def _first(value) -> str:
    return str(value[0] if isinstance(value, list) and value else value or "")


def preset_sets(root: Path) -> list[dict]:
    out = []
    if root.is_dir():
        for d in sorted(root.iterdir()):
            if all((d / f"{p}.json").is_file() for p in slicer.PARTS):
                fil, proc, mach = (_json(d / f"{p}.json") for p in ("filament", "process", "machine"))
                out.append({"name": d.name, "path": str(d), "material": _first(fil.get("filament_type")).upper(),
                            "layer_height": _first(proc.get("layer_height")),
                            "nozzle": _first(fil.get("nozzle_temperature")),
                            "printer_model": _first(mach.get("printer_model")),
                            "machine": _first(mach.get("name") or mach.get("printer_settings_id"))})
    return out


def spoolman_filaments() -> list[dict]:
    """Every filament with at least one spool on hand, as the colour and material picker shows it."""
    if not config.SPOOLMAN_URL:
        return []
    try:
        spools = json.loads(moonraker._call(f"{config.SPOOLMAN_URL}/spool?limit=5000", timeout=8))
    except (moonraker.PrinterError, ValueError):
        return []
    out: dict[int, dict] = {}
    for sp in spools:
        fil = sp.get("filament") or {}
        if fil.get("id") is None:
            continue
        vendor = ((fil.get("vendor") or {}).get("name") or "").strip()
        row = out.setdefault(fil["id"], {
            "id": fil["id"], "name": (fil.get("name") or "").replace(" ", " ").strip(), "vendor": vendor,
            "material": (fil.get("material") or "").strip().upper(), "color_hex": (fil.get("color_hex") or "")[:6],
            "spools": 0, "grams": 0})
        row["label"] = " ".join(x for x in (vendor, row["name"]) if x) or f"filament {fil['id']}"
        row["spools"] += 1
        row["grams"] += round(sp.get("remaining_weight") or 0)
    return sorted(out.values(), key=lambda r: (r["material"], r["label"].lower()))


def printers_view(all_jobs: list[dict]) -> dict:
    out = {}
    for name, raw in config.printers().items():
        cfg = config.printer(name)
        root = Path(raw.get("presets_root", "")).expanduser()
        sets = cached(f"sets:{root}", 30, lambda: preset_sets(root))
        # One presets folder often serves several printers. 'preset_machine' (part of the machine preset's
        # name) says which sets are this printer's; failing that, the printer model does.
        machine, model = raw.get("preset_machine", "").lower(), raw.get("gcode_printer_model", "").lower()
        mine = [s for s in sets if (machine in s["machine"].lower() if machine
                                    else not model or model in s["printer_model"].lower())]
        by_path = {s["path"]: s for s in sets}
        last: dict[str, dict] = {}  # material -> the settings of this printer's most recent job in it
        for job in all_jobs:
            if job["printer"] != name or job["kind"] != "print" or job["state"] == "dropped":
                continue
            try:
                t = jobs.ticket(job["id"])
            except (jobs.JobError, OSError, ValueError):
                continue
            material = (t.get("material") or by_path.get(t.get("presets", ""), {}).get("material", "")).upper()
            if material and t.get("presets") in by_path:
                last[material] = {"job": job["id"], "presets": t["presets"], "overrides": t.get("overrides", {})}

        def spool():
            try:
                return moonraker.spool(cfg)
            except Exception:  # noqa: BLE001 - an unreachable printer is not the page's problem
                return None

        out[name] = {"start_mode": raw.get("start_mode", "confirm"), "preset_sets": mine, "last": last,
                     "spool": cached(f"spool:{name}", 15, spool), "orca_gui": bool(raw.get("orca_gui"))}
    return out


def queue_view(all_jobs: list[dict], printers: dict) -> dict:
    out = {}
    for name, info in printers.items():
        rows = [j for j in all_jobs if j["printer"] == name and j["state"] in
                ("printing", "needs_human", "bed_ok", "uploaded", "awaiting_go", "submitted", "sliced")]
        rows += [j for j in all_jobs if j["printer"] == name and j["state"] == "queued" and j.get("held")]
        rows += daemon.queue_order(name, all_jobs, spool=info["spool"])
        out[name] = [{"id": j["id"], "title": j["title"], "state": j["state"], "held": bool(j.get("held")),
                      "kind": j["kind"], "wants": order.describe(j) if order.needs(j) != (None, "") else "",
                      "filament_id": (j.get("filament") or {}).get("id"), "material": j.get("material", ""),
                      "time": ((j.get("slice_report") or {}).get("estimate") or {}).get("time", ""),
                      "waiting": j.get("waiting_spool") or j.get("waiting_bed") or ""} for j in rows]
    return out


def state() -> dict:
    global PAGE_SEEN
    PAGE_SEEN = time.time()
    all_jobs = jobs.all_jobs()
    printers = printers_view(all_jobs)
    drafts = []
    for d in all_drafts():
        if not Path(d["path"]).is_file():
            continue  # the export was deleted or moved: nothing left to print
        drafts.append({**d, "history": history(d)})
    recent = [d for d in all_drafts("queued") if time.time() - d.get("queued_at", 0) < 3600]
    return {"printers": printers, "filaments": cached("filaments", 20, spoolman_filaments), "drafts": drafts,
            "queued": [{"id": d["id"], "name": d["name"], "job": d.get("job")} for d in recent][-5:],
            "queue": queue_view(all_jobs, printers), "slicer_door": bool(config.env("PRINTQ_DESK_KEY")),
            "watching": config.env("PRINTQ_DESK_WATCH")}


# ---------------------------------------------------------------- from a draft to a ticket

def build_ticket(draft: dict, choice: dict) -> dict:
    """The ticket for a draft and the operator's choices. Raises JobError with what is missing."""
    printer = choice.get("printer") or draft.get("printer") or ""
    config.printer(printer)
    fil = choice.get("filament") or {}
    title = (choice.get("title") or Path(draft["name"]).stem)[:80]
    if draft["kind"] == "gcode":
        return {"kind": "gcode", "title": title, "printer": printer, "file": draft["path"],
                "material": draft.get("material", ""), "filament": fil,
                "notes": "Sent from the slicer and confirmed at the print desk."}
    if not choice.get("presets"):
        raise jobs.JobError("no settings chosen: this printer has not printed that material through the queue yet")
    presets = Path(choice["presets"]).expanduser()
    overrides = choice.get("overrides") or {}
    merged = {**_json(presets / "process.json"), **_json(presets / "filament.json"), **overrides}
    # Pin what would be expensive to get wrong; the slicer's own record of each is compared after slicing.
    expect = {k: merged[k] for k in ("layer_height", "nozzle_temperature", "nozzle_temperature_initial_layer")
              if k in merged and k not in overrides}
    return {"kind": "print", "title": title, "printer": printer, "model": draft["path"], "presets": str(presets),
            "material": choice.get("material", ""), "filament": fil, "copies": int(choice.get("copies") or 1),
            "overrides": overrides, "expect": expect,
            "notes": f"Queued at the print desk ({draft['source']})."}


def choice_key(choice: dict) -> str:
    """What a preview depends on. The filament's colour does not change the slice; its material does."""
    return json.dumps([choice.get("printer"), choice.get("presets"), choice.get("overrides") or {},
                       int(choice.get("copies") or 1), choice.get("material", "")],
                      sort_keys=True, separators=(",", ":"), ensure_ascii=False)  # the page builds the same string


def preview(draft_id: str, choice: dict) -> dict:
    """Slice the draft as it would be queued and keep the slicer's report with it."""
    draft = load(draft_id)
    if draft["kind"] == "gcode":
        return {"ok": True, "estimate": draft.get("estimate", {}), "warnings": [], "problems": []}
    ticket = jobs.validate_ticket(build_ticket(draft, choice))
    if jobs.sha256(Path(draft["path"])) != draft["sha256"]:
        raise jobs.JobError("the file changed since this draft was made; it will reappear as a new draft")
    report = slicer.slice_ticket(ticket, drafts_dir() / draft_id / "slice")
    with LOCK:
        draft = load(draft_id)
        draft["preview"] = {"key": choice_key(choice), "choice": choice, "ok": report["ok"], "estimate": report.get("estimate", {}),
                            "warnings": report.get("warnings", []), "problems": report.get("problems", []),
                            "settings": report.get("settings", {})}
        save(draft)
    return draft["preview"]


def queue_draft(draft_id: str, choice: dict) -> dict:
    with LOCK:
        draft = load(draft_id)
        if draft["status"] != "open":
            raise jobs.JobError(f"draft {draft_id} is already {draft['status']}")
        if draft["kind"] == "print":
            seen = draft.get("preview") or {}
            if not seen.get("ok") or seen.get("key") != choice_key(choice):
                raise jobs.JobError("slice a preview with these settings first, so the estimate is the slicer's")
        job_id = jobs.submit(build_ticket(draft, choice))
        draft.update(status="queued", job=job_id, queued_at=time.time())
        save(draft)
    notify.log(f"desk: draft {draft_id} ({draft['name']}) queued as {job_id}")
    return {"job": job_id}


# ---------------------------------------------------------------- the watched export folder

def watch(root: Path, stop: threading.Event | None = None, pause: float = 2.0) -> None:
    """Turn every model written under `root` into a draft. A file counts once it has stopped changing."""
    def scan() -> dict[str, tuple]:
        seen = {}
        for base, _dirs, files in os.walk(root):
            for name in files:
                if name.lower().endswith(MODELS):
                    try:
                        st = os.stat(os.path.join(base, name))
                    except OSError:
                        continue
                    seen[os.path.join(base, name)] = (st.st_mtime_ns, st.st_size)
        return seen

    known = scan()  # what was there when the desk started is not news
    pending: dict[str, tuple] = {}
    while not (stop and stop.is_set()):
        time.sleep(pause)
        try:
            now = scan()
            fresh = []
            for path, stamp in now.items():
                if known.get(path) == stamp:
                    continue
                if pending.get(path) != stamp or stamp[1] == 0:
                    pending[path] = stamp  # still being written: look again
                    continue
                known[path] = stamp
                pending.pop(path, None)
                fresh.append(path)
            for path in list(known):
                if path not in now:
                    del known[path]
            made = [d for d in (exported(Path(p)) for p in fresh) if d]
            if made:
                announce(made)
        except Exception:  # noqa: BLE001 - the watcher must outlive one bad file
            notify.log(f"desk watcher: {traceback.format_exc()}")


def exported(path: Path) -> dict | None:
    """A draft for an export. An open draft of the same path is replaced: the newest export is the part."""
    with LOCK:
        sha = jobs.sha256(path)
        for d in all_drafts():
            if d["path"] == str(path):
                if d["sha256"] == sha:
                    return None
                d["status"] = "replaced"
                save(d)
        return new_draft("export", path.name, path=path)


def announce(made: list[dict]) -> None:
    """Bring the confirmation to the operator: open the page, unless it is open already."""
    notify.log("desk: new export(s): " + ", ".join(d["name"] for d in made))
    if time.time() - PAGE_SEEN < 8 or config.env("PRINTQ_DESK_OPEN", "1") != "1":
        return  # the page is on screen and shows the draft by itself
    try:
        subprocess.Popen(["xdg-open", f"http://localhost:{port()}/#{made[-1]['id']}"], start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as exc:
        notify.log(f"desk: could not open the page: {exc}")


# ---------------------------------------------------------------- HTTP

def from_slicer(printer: str, ctype: str, body: bytes) -> dict:
    """An upload in the OctoPrint form, as slicers send it to a 'print host'."""
    config.printer(printer)
    msg = email.message_from_bytes(b"Content-Type: " + ctype.encode() + b"\r\n\r\n" + body,
                                   policy=email.policy.HTTP)
    fields, name, data = {}, "", None
    for part in msg.iter_parts():
        field = part.get_param("name", header="content-disposition")
        if part.get_filename():
            name, data = part.get_filename(), part.get_payload(decode=True)
        elif field:
            fields[field] = (part.get_payload(decode=True) or b"").decode(errors="replace").strip()
    if data is None:
        raise jobs.JobError("the upload carried no file")
    name = safe_name(name, GCODE)
    draft = new_draft("slicer", name, data=data, printer=printer)
    if fields.get("print", "").lower() == "true":
        out = queue_draft(draft["id"], {"printer": printer})
        notify.log(f"desk: {name} sent from the slicer for {printer}, queued as {out['job']}")
    return {"files": {"local": {"name": name, "origin": "local", "path": name}}, "done": True}


class Handler(BaseHTTPRequestHandler):
    server_version = "printq-desk"
    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:  # the journal does not need every poll
        pass

    def reply(self, code: int, body, ctype: str = "application/json") -> None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def allowed_host(self) -> bool:
        # A page on some other site must not be able to reach this service under a name of its own.
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].lower()
        me = socket.gethostname().lower()
        return host in {"localhost", "127.0.0.1", me, f"{me}.lan"} | set(
            config.env("PRINTQ_DESK_HOSTS").lower().split())

    def body(self) -> bytes:
        size = int(self.headers.get("Content-Length") or 0)
        if size > 600 * 1024 * 1024:
            raise jobs.JobError("file too large")
        return self.rfile.read(size)

    def do_GET(self) -> None:  # noqa: N802
        url = urllib.parse.urlsplit(self.path)
        try:
            if not self.allowed_host():
                return self.reply(403, {"error": "unknown host name"})
            if url.path == "/":
                return self.reply(200, PAGE.read_bytes(), "text/html; charset=utf-8")
            if url.path == "/api/state":
                return self.reply(200, state())
            m = re.fullmatch(r"/api/draft/(d\d+)/file", url.path)
            if m:
                return self.reply(200, Path(load(m.group(1))["path"]).read_bytes(), "application/octet-stream")
            # What a slicer asks before it trusts a print host (the OctoPrint and Moonraker greetings).
            m = re.fullmatch(r"/p/([\w.-]+)/(api/version|server/info)", url.path)
            if m:
                config.printer(m.group(1))
                if m.group(2) == "api/version":
                    return self.reply(200, {"api": "0.1", "server": "1.5.0", "text": "OctoPrint (printq desk)"})
                return self.reply(200, {"result": {"klippy_connected": True, "klippy_state": "ready",
                                                   "components": [], "api_version_string": "1.0.0"}})
            self.reply(404, {"error": "not found"})
        except (jobs.JobError, config.ConfigError, OSError) as exc:
            self.reply(400, {"error": str(exc)})

    def do_POST(self) -> None:  # noqa: N802
        url = urllib.parse.urlsplit(self.path)
        query = dict(urllib.parse.parse_qsl(url.query))
        try:
            if not self.allowed_host():
                return self.reply(403, {"error": "unknown host name"})
            m = re.fullmatch(r"/p/([\w.-]+)/(api/files/local|server/files/upload)", url.path)
            if m:
                key = config.env("PRINTQ_DESK_KEY")
                if not key or not hmac.compare_digest(self.headers.get("X-Api-Key") or "", key):
                    self.body()
                    return self.reply(403, {"error": "the API key in the slicer's printer settings is missing or wrong"})
                out = from_slicer(m.group(1), self.headers.get("Content-Type") or "", self.body())
                return self.reply(201, out)
            # Everything else comes from the page, which says so in a header no other site can send.
            if self.headers.get("X-Printq-Desk") != "1":
                self.body()
                return self.reply(403, {"error": "not from the print desk page"})
            if url.path == "/api/upload":
                name = safe_name(query.get("name", ""), MODELS + GCODE)
                return self.reply(200, new_draft("upload", name, data=self.body()))
            data = json.loads(self.body() or b"{}")
            m = re.fullmatch(r"/api/draft/(d\d+)/(preview|print|discard|slicer)", url.path)
            if m:
                draft_id, action = m.groups()
                if action == "preview":
                    return self.reply(200, preview(draft_id, data))
                if action == "print":
                    return self.reply(200, queue_draft(draft_id, data))
                draft = load(draft_id)
                if action == "discard":
                    draft["status"] = "discarded"
                    save(draft)
                    shutil.rmtree(drafts_dir() / draft_id / "slice", ignore_errors=True)
                    return self.reply(200, {"discarded": True})
                cmd = config.printer(data.get("printer", "")).get("orca_gui")
                if not cmd:
                    raise jobs.JobError("printers.json has no 'orca_gui' command for that printer")
                subprocess.Popen([os.path.expanduser(c) for c in cmd] + [draft["path"]], start_new_session=True,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return self.reply(200, {"opened": True})
            m = re.fullmatch(r"/api/job/([a-z0-9-]+)/(drop|hold|release)", url.path)
            if m:
                return self.reply(200, {"said": daemon.safe_command(m.group(2), m.group(1))})
            self.reply(404, {"error": "not found"})
        except (jobs.JobError, config.ConfigError, ValueError, OSError) as exc:
            self.reply(400, {"error": str(exc)})
        except Exception:  # noqa: BLE001
            notify.log(f"desk: {traceback.format_exc()}")
            self.reply(500, {"error": "the print desk failed; see notify.log"})


def serve(bind: str | None = None, at: int | None = None) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((bind or config.env("PRINTQ_DESK_BIND", "127.0.0.1"), port() if at is None else at),
                                 Handler)
    server.daemon_threads = True
    return server


def main() -> None:
    server = serve()
    root = config.env("PRINTQ_DESK_WATCH")
    if root:
        threading.Thread(target=watch, args=(Path(root).expanduser(),), daemon=True, name="watch").start()
    notify.log(f"print desk on http://{server.server_address[0]}:{server.server_address[1]}/"
               + (f", watching {root}" if root else ", no export folder watched")
               + ("" if config.env("PRINTQ_DESK_KEY") else ", slicer door closed (no PRINTQ_DESK_KEY)"))
    server.serve_forever()
