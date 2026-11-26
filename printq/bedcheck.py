"""Decide from the printer's camera whether the bed is clear.

Three possible answers, and only one of them permits a print:

    clear        the bed surface is in view and nothing is on it
    occupied     a part or other object is on the bed
    cannot_tell  anything else: camera down, dark frame, bed out of view,
                 the judge unsure or unreachable

On printers whose bed drops out of the camera's view after a print
(`inspect.mode: "raise_bed"`), the bed is raised in small steps with a look
between each, and only while the toolhead is known to be parked clear of the
bed. The camera decides when to stop: a bed that is out of view is far below
the gantry, so one more step is always safe while nothing is seen. The
reported Z is not what stops it, because a bed whose motors were released can
sink without Klipper noticing. The procedure never homes Z.

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

from . import bedlevel, config, moonraker

VIEWS = ("bed_clear", "object", "bed_partial", "bed_not_visible", "unsure")
TOOLHEADS = ("parked", "elsewhere", "not_visible")
OBJECT_AT = ("none", "bottom_edge", "higher")

PROMPT = """{intro}
The print bed is the large flat plate. The toolhead, nozzle, gantry, belts, cables, lights and chamber
walls are parts of the printer, not objects on the bed.

Decide which of these the frame shows:
- "bed_clear": the bed's top surface is plainly in view, as much of it as the camera can show, and
  nothing is on it.
- "object": a printed part, a leftover skirt or brim, a tool, or any other loose object is on the bed,
  or is rising into view from below where the bed would be.
- "bed_partial": part of the bed surface is in view and nothing is on that part, but the bed sits too
  low for the whole of it to be seen.
- "bed_not_visible": the bed surface is not in view (for example it is too low), or only a thin edge
  of it is, and no object is visible either.
- "unsure": the frame is too dark, blurred or ambiguous to decide.

Also report "toolhead": {toolhead}

Also report "object_at", where the object is in the frame:
- "none": no object is seen.
- "bottom_edge": only its top is just entering at the bottom edge of the frame, far below the gantry.
- "higher": any other place, including an object lying on a bed surface that is in view.

{context}
A wrong "bed_clear" crashes the printer into a finished part, so choose it only when you are certain.
Thin, flat, same-colour parts are easy to miss: look for edges, outlines and shadows on the surface."""


INTRO = "This is a webcam frame from inside a 3D printer, taken before starting a new print."
INTRO_REF = """These are {count} webcam frames from inside the same 3D printer, from the same fixed camera.
{captions}
The last is the live frame, taken before starting a new print. Judge only the live frame."""
CAPTIONS = {
    "park": "a reference showing the toolhead at its park position, clear of the bed, with the bed "
            "lowered out of view.",
    "clear": "a reference showing this printer's own bed raised to inspection height and completely "
             "empty. That surface, its colour and its edges are the bed, not an object. When the bed is "
             "lower, less of it shows, down to only its rear corner entering at the bottom of the frame: "
             'that is still the bed ("bed_partial", or "bed_not_visible" while it is only a small part), '
             'never an "object". Answer "bed_clear" only when the live frame shows about as much of the '
             "bed as this reference does. Compare the bed surface with the reference and report anything "
             "on it that the reference does not show, however thin or flat, and wherever it lies, "
             "including at the corners and the edges of the frame.",
}
ORDINALS = ("The first", "The second")
TOOLHEAD = 'no park reference frame was given, so answer "not_visible".'
TOOLHEAD_REF = """compare the live frame with the park reference.
- "parked": the toolhead is in the same place in the live frame as in the reference.
- "elsewhere": the toolhead is visible but in a different place, however slightly.
- "not_visible": the toolhead cannot be found in the live frame."""


def _prompt(context: str, reference: dict | None) -> str:
    reference = reference or {}
    captions = "\n".join(f"{ORDINALS[n]} is {CAPTIONS[kind]}" for n, kind in enumerate(reference))
    intro = INTRO_REF.format(count=len(reference) + 1, captions=captions) if reference else INTRO
    return PROMPT.format(intro=intro, context=context,
                         toolhead=TOOLHEAD_REF if "park" in reference else TOOLHEAD)


def _images(jpeg: bytes, reference: dict | None) -> list[bytes]:
    return [*(reference or {}).values(), jpeg]


def _context(last_file: str, last_height: float | None) -> str:
    if not last_file:
        return ""
    size = f", about {last_height:g} mm tall" if last_height else ""
    return f"The last job printed here was '{last_file}'{size}; that is the object most likely to still be there."


def judge_claude(jpeg: bytes, context: str, reference: dict | None = None) -> dict:
    """Ask Claude. Any failure (no credentials, refusal, network) comes back as view='unsure'."""
    try:
        import anthropic
        from pydantic import BaseModel

        class Verdict(BaseModel):
            view: Literal["bed_clear", "object", "bed_partial", "bed_not_visible", "unsure"]
            toolhead: Literal["parked", "elsewhere", "not_visible"]
            object_at: Literal["none", "bottom_edge", "higher"]
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
                *({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                               "data": base64.standard_b64encode(image).decode()}}
                  for image in _images(jpeg, reference)),
                {"type": "text", "text": _prompt(context, reference)},
            ]}],
            output_format=Verdict,
        )
        # A refusal or truncated reply leaves parsed_output empty; that is an "unsure", never a "clear".
        if response.stop_reason != "end_turn" or response.parsed_output is None:
            return {"view": "unsure", "description": f"judge stopped with {response.stop_reason}"}
        return response.parsed_output.model_dump()
    except Exception as exc:  # noqa: BLE001 - fail safe: every error means "do not start"
        return {"view": "unsure", "description": f"vision judge unavailable: {type(exc).__name__}: {exc}"[:300]}


def judge_ollama(jpeg: bytes, context: str, model: str, reference: dict | None = None) -> dict:
    """Same question to a local vision model. Used in shadow mode: logged, never acted on."""
    schema = {"type": "object", "required": ["view", "toolhead", "object_at", "description"], "properties": {
        "object_at": {"type": "string", "enum": ["none", "bottom_edge", "higher"]},
        "view": {"type": "string", "enum": ["bed_clear", "object", "bed_partial", "bed_not_visible", "unsure"]},
        "toolhead": {"type": "string", "enum": ["parked", "elsewhere", "not_visible"]},
        "description": {"type": "string"}}}
    body = {"model": model, "stream": False, "think": False, "format": schema, "options": {"temperature": 0},
            "messages": [{"role": "user", "content": _prompt(context, reference),
                          "images": [base64.b64encode(image).decode()
                                     for image in _images(jpeg, reference)]}]}
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


def judge(jpeg: bytes, context: str, model: str, reference: dict | None = None) -> dict:
    """Route to the configured judge: a Claude model id, 'ollama:<model>' for a local vision model,
    or 'fixed:<view>[:<toolhead>[:<object_at>]]' (always the same answer; for tests and dry runs only)."""
    if model.startswith("fixed:"):
        view, toolhead, object_at = (model.split(":", 1)[1].split(":") + ["", ""])[:3]
        return {"view": view, "toolhead": toolhead or "parked", "object_at": object_at or "higher",
                "description": "fixed test verdict"}
    if model.startswith("ollama:"):
        return judge_ollama(jpeg, context, model.split(":", 1)[1], reference)
    return judge_claude(jpeg, context, reference)


def references(cfg: dict) -> dict:
    """The printer's reference frames, {"park": jpeg, "clear": jpeg}, for those that are configured:
    the toolhead at its park position, and the empty bed in view. A configured reference that cannot
    be read is an error, not a silent downgrade."""
    out = {}
    for kind in ("park", "clear"):
        name = cfg.get("inspect", {}).get(f"{kind}_reference")
        if name:
            path = Path(name).expanduser()
            out[kind] = (path if path.is_absolute() else config.HOME / path).read_bytes()
    return out


def look(cfg: dict, context: str, extra: dict) -> dict:
    """Take one frame, judge it, measure it, record it.
    Returns {view, description, frame, toolhead, object_at, row, target}. `row` is where the bed's far
    edge sits in the frame and `target` where it sits at inspection height (bedlevel); both None when
    the printer has no pair of reference frames. An object whose place is not reported counts as
    "higher": the answer that permits no further motion."""
    blind = {"view": "unsure", "frame": None, "toolhead": "not_visible", "object_at": "higher",
             "row": None, "target": None}
    try:
        jpeg = moonraker.snapshot(cfg)
        reference = references(cfg)
        gauge = bedlevel.Gauge(reference)
        row = gauge.row(jpeg)
    except (moonraker.PrinterError, OSError, ValueError) as exc:
        return {**blind, "description": f"no usable camera frame or reference frame: {exc}"}
    verdict = judge(jpeg, context, config.VISION_MODEL, reference)
    view = verdict.get("view") if verdict.get("view") in VIEWS else "unsure"
    toolhead = verdict.get("toolhead") if verdict.get("toolhead") in TOOLHEADS else "not_visible"
    object_at = verdict.get("object_at") if verdict.get("object_at") in OBJECT_AT else "higher"
    row_ = {"judge": config.VISION_MODEL, "view": view, "toolhead": toolhead if "park" in reference else None,
            "object_at": object_at if view == "object" else None, "bed_row": row,
            "description": verdict.get("description", ""), **extra}
    if config.SHADOW_VISION_MODEL:
        shadow = judge(jpeg, context, config.SHADOW_VISION_MODEL, reference)
        row_["shadow"] = {"judge": config.SHADOW_VISION_MODEL, "view": shadow.get("view"),
                          "toolhead": shadow.get("toolhead") if "park" in reference else None}
    return {"view": view, "description": row_["description"], "frame": record(cfg["name"], jpeg, row_),
            "toolhead": toolhead, "object_at": object_at, "row": row, "target": gauge.target}


def _near(pos: list, xy: list, tol: float = 1.0) -> bool:
    return abs(pos[0] - xy[0]) <= tol and abs(pos[1] - xy[1]) <= tol


NOMINAL_Z = 250.0  # what Klipper is told when the real Z is unknown: only there to make the raises legal


def check(cfg: dict, job_id: str = "") -> dict:
    """Run the bed check for one printer. Returns {verdict, reason, frame, steps}."""
    st = moonraker.status(cfg)
    if not st["idle"]:
        return {"verdict": "cannot_tell", "reason": f"printer is {st['state']}, not idle", "frame": None, "steps": 0}
    height = moonraker.last_object_height(cfg, st["file"])
    context = _context(st["file"], height)
    insp = cfg.get("inspect", {"mode": "static"})
    raising = insp.get("mode") == "raise_bed"
    frame, steps, placed, assumed = None, 0, False, False
    sighting, confirms = "", 0  # an object first seen entering at the bottom edge, and the raises spent on it
    last_row, stride = None, 0.0  # the bed's far edge in the previous frame, and how far one raise moved it

    def result(verdict: str, reason: str) -> dict:
        if sighting and verdict == "cannot_tell":
            # An object was seen and the confirming look could not be had: the sighting stands.
            verdict, reason = "occupied", f"{sighting} (could not confirm it closer: {reason})"
        if assumed:
            # Z was only a placeholder for the relative raises. Release the motors so Klipper forgets
            # it: nothing may later trust that number, and the next print homes properly anyway.
            moonraker.gcode(cfg, "M84")
        return {"verdict": verdict, "reason": reason, "frame": str(frame) if frame else None, "steps": steps}

    while True:
        now = moonraker.status(cfg)
        pos = now["position"]
        seen = look(cfg, context, {"job": job_id, "z": pos[2], "last_file": st["file"], "z_assumed": assumed})
        view, description, toolhead = seen["view"], seen["description"], seen["toolhead"]
        frame = seen["frame"] or frame

        # Bed height by camera, where the printer has reference frames. The bed is at inspection height
        # once its far edge is within one raise of where the clear reference has it; it never goes past.
        row, target = seen["row"], seen["target"]
        if row is not None and last_row is not None and steps:
            stride = max(stride, last_row - row)
        last_row = row if row is not None else last_row
        at_height = target is not None and row is not None \
            and row - max(1.5 * stride, insp.get("stop_margin", 0.06)) <= target

        if view == "bed_clear" and raising and target is not None and not at_height:
            view = "bed_partial"  # nothing seen so far, but more of the bed shows from higher up
        if view == "bed_clear" and not sighting:  # a sighting is never overruled by a later frame
            return result("clear", description)
        if view == "object":
            # A top just entering at the bottom edge is far below the gantry. With the toolhead parked
            # the bed may come up a little more for a better look before the verdict; anywhere else in
            # the frame, or once the confirming raises are spent, the verdict is immediate.
            if not raising or seen["object_at"] != "bottom_edge" or at_height \
                    or confirms >= insp.get("object_confirm_steps", 1):
                return result("occupied", description)
            sighting, confirms = description, confirms + 1
        elif sighting:
            return result("occupied", f"{sighting} (the next frame was judged '{view}': {description})")
        elif view == "unsure" or not raising:
            return result("cannot_tell", description)
        elif at_height:
            return result("cannot_tell", f"the bed is at inspection height but was judged '{view}', not clear: "
                                         f"{description}")

        # The bed is not yet at inspection height and nothing is in the way. Raise it one step, if every
        # guard allows it.
        park, plan, z = insp["park_xy"], None, pos[2]
        if not placed and "z" not in now["homed_axes"]:
            # Motors were released after the last print. The bed may be raised only once the toolhead
            # is known to be at the park position, clear of the bed. Klipper's Z is whatever it last
            # knew; the bed may have sunk or been moved by hand since, so that number is worth nothing
            # and Klipper is given a placeholder instead.
            if _near(pos, park):
                plan = "tell"
            elif "also_from_xy" in insp and _near(pos, insp["also_from_xy"]) \
                    and "x" in now["homed_axes"] and "y" in now["homed_axes"]:
                plan = "move"
            elif insp.get("unknown_position") == "home_xy" and not now["homed_axes"]:
                # Klipper restarted and knows nothing. Homing X and Y proves where the toolhead is
                # without moving the bed towards it.
                plan = "home"
            else:
                return result("cannot_tell",
                              f"toolhead reads {pos[:2]}, not the known park position, so not moving the bed")
            if target is None:
                # Neither Klipper nor the camera can say how high the bed is: nothing would stop the
                # raise at the top but the judge alone.
                return result("cannot_tell", "the bed's height is unknown (motors were released) and this "
                                             "printer has no park_reference and clear_reference frames to "
                                             "measure it by camera, so not moving the bed")
            z = NOMINAL_Z
        if plan in ("home", "move"):
            # The toolhead is about to be put at the park position, so where the camera shows it now
            # says nothing. Park it without moving the bed, then look again: the camera has to confirm
            # the park before the first raise.
            if plan == "home":
                moonraker.gcode(cfg, insp.get("home_xy_gcode", "G28 X Y"))
            moonraker.gcode(cfg, f"G90\nG1 X{park[0]} Y{park[1]} F3000\nM400")
            moonraker.gcode(cfg, f"SET_KINEMATIC_POSITION X={park[0]} Y={park[1]} Z={NOMINAL_Z:g}")
            assumed = placed = True
            time.sleep(insp.get("settle_sec", 2))
            continue
        if insp.get("park_reference") and toolhead != "parked":
            return result("cannot_tell", f"the camera does not show the toolhead at its park position "
                                         f"(it reads as '{toolhead}'), so not moving the bed")
        # With the toolhead off the bed nothing is above a part that rises with it: while the camera sees
        # nothing coming, one more step is always safe. The camera decides when to stop. A Z that Klipper
        # really knows adds two limits of its own.
        step = insp.get("step", 10)
        if not (assumed or plan):
            floor = insp.get("stop_z", 1)
            if height is not None:
                floor = max(floor, height + insp.get("object_margin", 20))
            if z - step < floor:
                return result("cannot_tell", f"bed not judged clear at Z={z:g}; not raising it past Z={floor:g}")
        if steps >= insp.get("max_steps", 26):
            return result("cannot_tell", f"bed still not at inspection height after {steps} raises")
        if plan:
            assumed = True
        if plan or (assumed and z - step < 5):
            moonraker.gcode(cfg, f"SET_KINEMATIC_POSITION X={park[0]} Y={park[1]} Z={NOMINAL_Z:g}")
        placed = True
        moonraker.gcode(cfg, f"G91\nG1 Z-{step} F600\nG90\nM400")
        steps += 1
        time.sleep(insp.get("settle_sec", 2))
