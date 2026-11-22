"""Thin Moonraker (Klipper) and Spoolman client. Standard library only."""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from . import config

IDLE_STATES = ("standby", "complete", "cancelled")


class PrinterError(Exception):
    pass


def _call(url: str, data: bytes | None = None, headers: dict | None = None, timeout: float = 15) -> bytes:
    req = urllib.request.Request(url, data=data, headers=headers or {}, method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except urllib.error.HTTPError as exc:
        # Moonraker answers a failed command with the printer's own message; that is what the operator needs.
        body = exc.read()[:2000]
        try:
            said = json.loads(body)["error"]["message"]
        except (ValueError, KeyError, TypeError):
            said = str(exc)
        raise PrinterError(f"{url.split('?')[0]}: {said}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise PrinterError(f"{url.split('?')[0]}: {exc}") from exc


def get(cfg: dict, path: str, timeout: float = 15) -> dict:
    return json.loads(_call(cfg["url"] + path, timeout=timeout))["result"]


def post(cfg: dict, path: str, timeout: float = 30) -> dict:
    return json.loads(_call(cfg["url"] + path, data=b"", timeout=timeout))


def status(cfg: dict) -> dict:
    """State, last file, position, temperatures and the active spool of one printer."""
    macro = f"gcode_macro {cfg['cooldown_macro']}" if cfg.get("cooldown_macro") else ""
    s = get(cfg, "/printer/objects/query?print_stats&toolhead=position,homed_axes"
                 "&heater_bed=temperature,target&extruder=temperature,target&display_status=progress"
                 + ("&" + urllib.parse.quote(macro) if macro else ""))["status"]
    ps = s["print_stats"]
    # A cool-down macro that keeps running after the file has ended: the part is not ready and the
    # printer is not free, whatever print_stats says.
    cool = s.get(macro) or {}
    cooling = bool(cool.get("active"))
    out = {
        "printer": cfg["name"], "state": ps["state"], "idle": ps["state"] in IDLE_STATES and not cooling,
        "cooling": cooling, "extruding": (ps.get("print_duration") or 0) > 0,
        "file": ps.get("filename", ""), "progress": round(s["display_status"].get("progress") or 0, 3),
        "position": [round(v, 2) for v in s["toolhead"]["position"][:3]],
        "homed_axes": s["toolhead"]["homed_axes"],
        "bed": [round(s["heater_bed"]["temperature"]), s["heater_bed"]["target"]],
        "nozzle": [round(s["extruder"]["temperature"]), s["extruder"]["target"]],
    }
    if cooling:
        stages = cool.get("stages") or []
        out["cooling_min_left"] = round(sum(x.get("hold", 0) for x in stages[int(cool.get("stage", 0)):]) / 60)
    out["spool"] = spool(cfg) or "unknown (no spool tracking for this printer)"
    return out


def last_object_height(cfg: dict, filename: str) -> float | None:
    """Height in mm of the object the given (last printed) file produced, if Moonraker knows it."""
    if not filename:
        return None
    try:
        meta = get(cfg, "/server/files/metadata?filename=" + urllib.parse.quote(filename))
    except PrinterError:
        return None
    return meta.get("object_height")


def spool(cfg: dict) -> dict | None:
    """The printer's active Spoolman spool: id, material, colour name and metres left."""
    if not config.SPOOLMAN_URL:
        return None
    try:
        sid = get(cfg, "/server/spoolman/spool_id").get("spool_id")
        if not sid:
            return None
        sp = json.loads(_call(f"{config.SPOOLMAN_URL}/spool/{sid}"))
    except (PrinterError, ValueError, KeyError):
        return None
    fil = sp.get("filament", {})
    return {"id": sid, "material": fil.get("material", ""), "name": fil.get("name", ""),
            "remaining_g": round(sp.get("remaining_weight") or 0),
            "remaining_m": round((sp.get("remaining_length") or 0) / 1000, 1)}


SNAPSHOT_TRIES, SNAPSHOT_PAUSE = 4, 5  # seconds between tries


def snapshot(cfg: dict) -> bytes:
    """One JPEG from the printer's first webcam."""
    url = cfg.get("snapshot_url")
    if not url:
        cams = get(cfg, "/server/webcams/list")["webcams"]
        if not cams:
            raise PrinterError(f"{cfg['name']} has no webcam configured in Moonraker")
        url = cams[0]["snapshot_url"]
    if url.startswith("/"):
        # Moonraker stores paths relative to the web front end, which is the host without the API port
        parts = urllib.parse.urlsplit(cfg["url"])
        url = f"{parts.scheme}://{parts.hostname}{url}"
    # Camera servers stall now and then. One slow frame must not end a bed check half-way up.
    for attempt in range(SNAPSHOT_TRIES):
        try:
            data = _call(url, timeout=15)  # urllib follows redirects
            if data[:2] == b"\xff\xd8":
                return data
            problem = PrinterError(f"{cfg['name']} webcam did not return a JPEG")
        except PrinterError as exc:
            problem = exc
        if attempt + 1 < SNAPSHOT_TRIES:
            time.sleep(SNAPSHOT_PAUSE)
    raise problem


def gcode(cfg: dict, script: str, timeout: float = 120) -> None:
    post(cfg, "/printer/gcode/script?script=" + urllib.parse.quote(script), timeout=timeout)


def last_job_status(cfg: dict) -> str:
    """Moonraker's verdict on the most recent job: 'completed', 'cancelled', 'error', ... or '' if unknown."""
    try:
        found = get(cfg, "/server/history/list?limit=1&order=desc")["jobs"]
        return found[0]["status"] if found else ""
    except (PrinterError, KeyError):
        return ""


def klipper_state(cfg: dict) -> str:
    """'ready', 'startup', 'shutdown' or 'error'."""
    return get(cfg, "/printer/info")["state"]


def responses(cfg: dict, since: float) -> list[str]:
    """What the printer has printed to its console since the given time."""
    store = get(cfg, "/server/gcode_store?count=200")["gcode_store"]
    return [e["message"] for e in store if e.get("type") == "response" and e.get("time", 0) >= since]


def upload(cfg: dict, path: Path, name: str) -> str:
    """Upload a G-code file (no auto-start) and return the name Moonraker stored it under."""
    boundary = uuid.uuid4().hex
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{name}\"\r\n"
            "Content-Type: application/octet-stream\r\n\r\n").encode() + path.read_bytes() + \
           f"\r\n--{boundary}--\r\n".encode()
    raw = _call(cfg["url"] + "/server/files/upload", data=body,
                headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}, timeout=300)
    stored = json.loads(raw)
    stored = stored.get("result", stored)
    return stored["item"]["path"]


def start(cfg: dict, filename: str) -> None:
    post(cfg, "/printer/print/start?filename=" + urllib.parse.quote(filename))
