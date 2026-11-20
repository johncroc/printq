"""Headless OrcaSlicer: slice a ticket's model and verify the result.

The ticket names a preset set (a directory with flattened machine.json,
process.json and filament.json, the form the Orca CLI accepts) plus optional
`overrides`. Overrides are merged into temporary copies of those files, so
the preset set itself is never modified.

Nothing here trusts the slicer's exit code alone. After slicing, the G-code's
own CONFIG_BLOCK is read back and every override and every `expect` entry of
the ticket is compared with what the slicer actually used.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

from . import config
from .jobs import JobError

PARTS = ("machine", "process", "filament")
HEADER = {
    "filament_mm": r"^; filament used \[mm\] = ([\d.]+)",
    "filament_g": r"^; total filament used \[g\] = ([\d.]+)",
    "time": r"^; estimated printing time \(normal mode\) = (.+)$",
    "layers": r"^; total layers count = (\d+)",
    "height_mm": r"^; max_z_height: ([\d.]+)",
}


def _norm(value) -> str:
    """One comparable string for a preset value and for its CONFIG_BLOCK text."""
    if isinstance(value, list):
        value = ",".join(str(v) for v in value)
    return str(value).strip().strip('"').replace(" ", "")


def merge_overrides(presets: Path, overrides: dict, workdir: Path) -> dict[str, Path]:
    """Copy the three preset files into workdir with the overrides applied."""
    data = {p: json.loads((presets / f"{p}.json").read_text()) for p in PARTS}
    unknown = []
    for key, value in overrides.items():
        homes = [p for p in PARTS if key in data[p]]
        if not homes:
            unknown.append(key)
            continue
        for p in homes:
            old = data[p][key]
            # keep the preset's own shape: a per-extruder list stays a list
            data[p][key] = [str(value)] * max(len(old), 1) if isinstance(old, list) and not isinstance(value, list) \
                else ([str(v) for v in value] if isinstance(value, list) else str(value))
    if unknown:
        raise JobError("overrides name settings this preset set does not have: " + ", ".join(unknown))
    out = {}
    for p in PARTS:
        out[p] = workdir / f"{p}.json"
        out[p].write_text(json.dumps(data[p], indent=1))
    return out


def read_gcode(path: Path) -> tuple[dict, dict]:
    """Return (summary, config) parsed from an Orca G-code file's comments."""
    summary, cfg, in_block = {}, {}, False
    with open(path, errors="replace") as f:
        for line in f:
            if not line.startswith(";"):
                continue
            if line.startswith("; CONFIG_BLOCK_START"):
                in_block = True
            elif line.startswith("; CONFIG_BLOCK_END"):
                in_block = False
            elif in_block and " = " in line:
                key, _, value = line[2:].rstrip("\n").partition(" = ")
                cfg[key] = value
            else:
                for name, pattern in HEADER.items():
                    m = re.match(pattern, line)
                    if m:
                        summary[name] = m.group(1).strip()
    for key in ("filament_mm", "filament_g", "height_mm"):
        if key in summary:
            summary[key] = float(summary[key])
    if "layers" in summary:
        summary["layers"] = int(summary["layers"])
    return summary, cfg


def slice_ticket(ticket: dict, workdir: Path) -> dict:
    """Slice and verify. Returns a report; report['ok'] is False if anything is off."""
    cfg = config.printer(ticket["printer"])
    if workdir.exists():
        shutil.rmtree(workdir)
    workdir.mkdir(parents=True)
    files = merge_overrides(Path(ticket["presets"]), ticket["overrides"], workdir)
    cmd = [config.ORCA_BIN]
    if cfg.get("orca_datadir"):
        cmd += ["--datadir", str(Path(cfg["orca_datadir"]).expanduser())]
    cmd += ["--load-settings", f"{files['machine']};{files['process']}",
            "--load-filaments", str(files["filament"]),
            "--arrange", "1", "--orient", "0", "--slice", "1", "--outputdir", str(workdir), "--debug", "2"]
    if cfg.get("bed_exclude_area") is not None:
        cmd += ["--bed-exclude-area", cfg["bed_exclude_area"]]
    cmd += [ticket["model"]] * ticket["copies"]
    try:
        run = subprocess.run(cmd, capture_output=True, text=True, timeout=1800, cwd=workdir)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "problems": [f"could not run the slicer: {exc}"]}
    (workdir / "orca.log").write_text(run.stdout + run.stderr)
    result = {}
    if (workdir / "result.json").exists():
        result = json.loads((workdir / "result.json").read_text())
    gcode = workdir / "plate_1.gcode"
    problems = []
    if run.returncode != 0 or result.get("return_code", 0) != 0 or not gcode.exists():
        problems.append(f"slicer failed (exit {run.returncode}): {result.get('error_string', 'no result.json')}")
        return {"ok": False, "problems": problems}
    if (workdir / "plate_2.gcode").exists():
        problems.append("the model(s) did not fit on one plate")
    summary, used = read_gcode(gcode)
    for field in ("filament_mm", "filament_g", "time", "layers"):
        if field not in summary:
            problems.append(f"G-code has no '{field}' line")
    checked = {}
    for key, want in {**ticket["overrides"], **ticket["expect"]}.items():
        got = used.get(key)
        checked[key] = got
        if got is None or _norm(got) != _norm(want):
            problems.append(f"setting {key}: ticket wants {want!r}, slicer used {got!r}")
    warnings = [w for plate in result.get("sliced_plates", []) for w in [plate.get("warning_message")] if w]
    warnings += [str(w) for w in result.get("warnings", [])]
    if ticket.get("material") and _norm(used.get("filament_type", "")) != _norm(ticket["material"]):
        problems.append(f"ticket material {ticket['material']} but the filament preset is {used.get('filament_type')}")
    return {
        "ok": not problems, "problems": problems, "warnings": warnings, "gcode": str(gcode),
        "estimate": summary, "checked_settings": checked,
        "settings": {k: used.get(k) for k in (
            "printer_settings_id", "print_settings_id", "filament_settings_id", "filament_type", "layer_height",
            "nozzle_temperature", "hot_plate_temp", "textured_plate_temp", "curr_bed_type",
            "chamber_temperatures", "sparse_infill_density", "wall_loops", "enable_support", "brim_type")},
    }
