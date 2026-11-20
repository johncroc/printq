"""Thin Moonraker (Klipper) and Spoolman client. Standard library only."""
from __future__ import annotations

import json
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
    except (urllib.error.URLError, OSError) as exc:
        raise PrinterError(f"{url.split('?')[0]}: {exc}") from exc


def get(cfg: dict, path: str, timeout: float = 15) -> dict:
    return json.loads(_call(cfg["url"] + path, timeout=timeout))["result"]


def post(cfg: dict, path: str, timeout: float = 30) -> dict:
    return json.loads(_call(cfg["url"] + path, data=b"", timeout=timeout))


def status(cfg: dict) -> dict:
    """State, last file, position, temperatures and the active spool of one printer."""
    s = get(cfg, "/printer/objects/query?print_stats&toolhead=position,homed_axes"
                 "&heater_bed=temperature,target&extruder=temperature,target&display_status=progress")["status"]
    ps = s["print_stats"]
    out = {
        "printer": cfg["name"], "state": ps["state"], "idle": ps["state"] in IDLE_STATES,
        "file": ps.get("filename", ""), "progress": round(s["display_status"].get("progress") or 0, 3),
        "position": [round(v, 2) for v in s["toolhead"]["position"][:3]],
        "homed_axes": s["toolhead"]["homed_axes"],
        "bed": [round(s["heater_bed"]["temperature"]), s["heater_bed"]["target"]],
        "nozzle": [round(s["extruder"]["temperature"]), s["extruder"]["target"]],
    }
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
    data = _call(url, timeout=15)  # urllib follows redirects
    if data[:2] != b"\xff\xd8":
        raise PrinterError(f"{cfg['name']} webcam did not return a JPEG")
    return data


def gcode(cfg: dict, script: str, timeout: float = 120) -> None:
    post(cfg, "/printer/gcode/script?script=" + urllib.parse.quote(script), timeout=timeout)


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
