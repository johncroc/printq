"""Decide from the printer's camera whether the bed is clear.

Three possible answers, and only one of them permits a print:

    clear        the bed surface is in view and nothing is on it
    occupied     a part or other object is on the bed
    cannot_tell  anything else: camera down, dark frame, bed out of view,
                 the judge unsure or unreachable

On printers whose bed drops out of the camera's view after a print
(`inspect.mode: "raise_bed"`), the bed is raised in small steps with a look
between each, and only while the toolhead is known to be parked clear of the
bed. A known last object keeps its top below the nozzle plane by a margin.
The procedure never homes Z.

Every frame is saved to the dataset directory with the verdicts of each
judge, so the frames can later train a dedicated classifier
(docs/training-your-own-model.md).
"""
from __future__ import annotations

import base64
import json
import time
import urllib.request
from pathlib import Path
from typing import Literal

from . import config, moonraker

VIEWS = ("bed_clear", "object", "bed_not_visible", "unsure")

PROMPT = """This is a webcam frame from inside a 3D printer, taken before starting a new print.
The print bed is the large flat plate. The toolhead, nozzle, gantry, belts, cables, lights and chamber
walls are parts of the printer, not objects on the bed.

Decide which of these the frame shows:
- "bed_clear": a large part of the bed's top surface is plainly in view and nothing is on it.
- "object": a printed part, a leftover skirt or brim, a tool, or any other loose object is on the bed,
  or is rising into view from below where the bed would be.
- "bed_not_visible": the bed surface is not in view (for example it is too low), or only a thin edge
  of it is, and no object is visible either.
- "unsure": the frame is too dark, blurred or ambiguous to decide.

{context}
A wrong "bed_clear" crashes the printer into a finished part, so choose it only when you are certain.
Thin, flat, same-colour parts are easy to miss: look for edges, outlines and shadows on the surface."""


def _context(last_file: str, last_height: float | None) -> str:
    if not last_file:
        return ""
    size = f", about {last_height:g} mm tall" if last_height else ""
    return f"The last job printed here was '{last_file}'{size}; that is the object most likely to still be there."


def judge_claude(jpeg: bytes, context: str) -> dict:
    """Ask Claude. Any failure (no credentials, refusal, network) comes back as view='unsure'."""
    try:
        import anthropic
        from pydantic import BaseModel

        class Verdict(BaseModel):
            view: Literal["bed_clear", "object", "bed_not_visible", "unsure"]
            description: str

        key = config.env("PRINTQ_ANTHROPIC_API_KEY")
        if not key:
            return {"view": "unsure", "description": "no PRINTQ_ANTHROPIC_API_KEY set, so no vision judge"}
        # Explicit key and URL: the agent harness that launches this process points the standard
        # ANTHROPIC_* variables at the local model server, and the judge must not follow them there.
        client = anthropic.Anthropic(api_key=key, base_url="https://api.anthropic.com")
        response = client.messages.parse(
            model=config.VISION_MODEL,
            max_tokens=2000,
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                             "data": base64.standard_b64encode(jpeg).decode()}},
                {"type": "text", "text": PROMPT.format(context=context)},
            ]}],
            output_format=Verdict,
        )
        # A refusal or truncated reply leaves parsed_output empty; that is an "unsure", never a "clear".
        if response.stop_reason != "end_turn" or response.parsed_output is None:
            return {"view": "unsure", "description": f"judge stopped with {response.stop_reason}"}
        return response.parsed_output.model_dump()
    except Exception as exc:  # noqa: BLE001 - fail safe: every error means "do not start"
        return {"view": "unsure", "description": f"vision judge unavailable: {type(exc).__name__}: {exc}"[:300]}


def judge_ollama(jpeg: bytes, context: str, model: str) -> dict:
    """Same question to a local vision model. Used in shadow mode: logged, never acted on."""
    schema = {"type": "object", "required": ["view", "description"], "properties": {
        "view": {"type": "string", "enum": ["bed_clear", "object", "bed_not_visible", "unsure"]},
        "description": {"type": "string"}}}
    body = {"model": model, "stream": False, "think": False, "format": schema, "options": {"temperature": 0},
            "messages": [{"role": "user", "content": PROMPT.format(context=context),
                          "images": [base64.b64encode(jpeg).decode()]}]}
    try:
        req = urllib.request.Request(config.OLLAMA_URL + "/api/chat", json.dumps(body).encode(),
                                     {"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(json.load(r)["message"]["content"])
    except Exception as exc:  # noqa: BLE001
        return {"view": "unsure", "description": f"shadow judge failed: {exc}"[:200]}


def record(printer: str, jpeg: bytes, row: dict) -> Path:
    """Save a frame and append its row to the dataset index. Returns the frame path."""
    folder = config.DATASET / printer
    folder.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = folder / f"{stamp}.jpg"
    n = 1
    while path.exists():
        n += 1
        path = folder / f"{stamp}-{n}.jpg"
    path.write_bytes(jpeg)
    with open(config.DATASET / "index.jsonl", "a") as f:
        f.write(json.dumps({"frame": str(path.relative_to(config.DATASET)), "printer": printer,
                            "time": stamp, **row}) + "\n")
    return path


def judge(jpeg: bytes, context: str, model: str) -> dict:
    """Route to the configured judge: a Claude model id, 'ollama:<model>' for a local vision model,
    or 'fixed:<view>' (always the same answer; for tests and dry runs only)."""
    if model.startswith("fixed:"):
        return {"view": model.split(":", 1)[1], "description": "fixed test verdict"}
    if model.startswith("ollama:"):
        return judge_ollama(jpeg, context, model.split(":", 1)[1])
    return judge_claude(jpeg, context)


def look(cfg: dict, context: str, extra: dict) -> tuple[str, str, Path | None]:
    """Take one frame, judge it, record it. Returns (view, description, frame path)."""
    try:
        jpeg = moonraker.snapshot(cfg)
    except moonraker.PrinterError as exc:
        return "unsure", f"no camera frame: {exc}", None
    verdict = judge(jpeg, context, config.VISION_MODEL)
    view = verdict.get("view") if verdict.get("view") in VIEWS else "unsure"
    row = {"judge": config.VISION_MODEL, "view": view, "description": verdict.get("description", ""), **extra}
    if config.SHADOW_VISION_MODEL:
        shadow = judge(jpeg, context, config.SHADOW_VISION_MODEL)
        row["shadow"] = {"judge": config.SHADOW_VISION_MODEL, "view": shadow.get("view")}
    return view, row["description"], record(cfg["name"], jpeg, row)


def _near(pos: list, xy: list, tol: float = 1.0) -> bool:
    return abs(pos[0] - xy[0]) <= tol and abs(pos[1] - xy[1]) <= tol


def check(cfg: dict, job_id: str = "") -> dict:
    """Run the bed check for one printer. Returns {verdict, reason, frame, steps}."""
    st = moonraker.status(cfg)
    if not st["idle"]:
        return {"verdict": "cannot_tell", "reason": f"printer is {st['state']}, not idle", "frame": None, "steps": 0}
    height = moonraker.last_object_height(cfg, st["file"])
    context = _context(st["file"], height)
    insp = cfg.get("inspect", {"mode": "static"})
    frame, steps, placed, assumed = None, 0, False, False

    def result(verdict: str, reason: str) -> dict:
        if assumed:
            # Z was only assumed for the relative raises. Release the motors so Klipper forgets it:
            # nothing may later trust that number, and the next print homes properly anyway.
            moonraker.gcode(cfg, "M84")
        return {"verdict": verdict, "reason": reason, "frame": str(frame) if frame else None, "steps": steps}

    while True:
        now = moonraker.status(cfg)
        pos = now["position"]
        view, description, shot = look(cfg, context, {"job": job_id, "z": pos[2], "last_file": st["file"],
                                                      "z_assumed": assumed})
        frame = shot or frame
        if view == "bed_clear":
            return result("clear", description)
        if view == "object":
            return result("occupied", description)
        if view == "unsure" or insp.get("mode") != "raise_bed":
            return result("cannot_tell", description)

        # The bed is out of view. Raise it one step, if every guard allows it.
        park, plan, z = insp["park_xy"], None, pos[2]
        if not placed and "z" not in now["homed_axes"]:
            # Motors were released after the last print. The bed may be raised only once the toolhead
            # is known to be at the park position, clear of the bed.
            if _near(pos, park):
                plan = "tell"
            elif "also_from_xy" in insp and _near(pos, insp["also_from_xy"]) \
                    and "x" in now["homed_axes"] and "y" in now["homed_axes"]:
                plan = "move"
            elif insp.get("unknown_position") == "home_xy" and not now["homed_axes"]:
                # Klipper restarted and knows nothing. Homing X and Y proves where the toolhead is
                # without moving the bed towards it; Z is then assumed to be the end-of-print rest height.
                plan, z = "home", float(insp["rest_z"])
            else:
                return result("cannot_tell",
                              f"toolhead reads {pos[:2]}, not the known park position, so not moving the bed")
        # With the toolhead off the bed nothing is above a part that rises with it, so an unknown last
        # object is no reason to stay put; a known one still keeps its top below the nozzle plane.
        floor = max(insp["min_z"], height + insp.get("object_margin", 20) if height is not None else 0)
        step = insp.get("step", 10)
        if z - step < floor:
            return result("cannot_tell", f"bed still not in view at Z={z:g}; not raising it past Z={floor:g}")
        if steps >= insp.get("max_steps", 12):
            return result("cannot_tell", f"bed still not in view after {steps} raises")
        if plan == "home":
            moonraker.gcode(cfg, insp.get("home_xy_gcode", "G28 X Y"))
            assumed = True
        if plan in ("home", "move"):
            moonraker.gcode(cfg, f"G90\nG1 X{park[0]} Y{park[1]} F3000\nM400")
        if plan:
            moonraker.gcode(cfg, f"SET_KINEMATIC_POSITION X={park[0]} Y={park[1]} Z={z}")
        placed = True
        moonraker.gcode(cfg, f"G91\nG1 Z-{step} F600\nG90\nM400")
        steps += 1
        time.sleep(insp.get("settle_sec", 2))
