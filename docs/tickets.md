# Tickets and presets

A ticket is the handoff from the design session to the queue: everything needed to print the part, written down once, at the moment the design is approved.

## Start from a blank ticket

```bash
printq blank printer-1 > ticket.json    # or `printq blank` for every printer; add --macro for a macro job
```

The blank ticket is the current format, written by the program that validates it: every field with its empty value, and under `_help` which fields are required, what each one means, and what this installation offers (printers, their start mode, the preset sets found under each `presets_root`, and the set the printer's last job used). Fill it in, then `printq check` and `printq submit`. Keys beginning with `_` are ignored at submit, so `_help` can stay in the file.

Anything that writes tickets, a person or an assistant, should start here and not from a remembered copy of the format. The format will change; the blank ticket changes with it.

The job number is assigned at submit, not when the blank ticket is handed out, so an abandoned draft leaves nothing behind in the queue.

## Ticket format

```json
{
  "title": "Cable clip, 12 mm",
  "printer": "printer-1",
  "model": "~/models/cable-clip-12.stl",
  "presets": "printer-1-asa-0.2",
  "material": "ASA",
  "copies": 4,
  "overrides": {
    "sparse_infill_density": "25%",
    "brim_type": "outer_only"
  },
  "expect": {
    "layer_height": "0.2",
    "nozzle_temperature": "260"
  },
  "notes": "Snap fit. If the spool has less than 10 m left, say so in the announcement."
}
```

| Field | Required | Meaning |
|---|---|---|
| `title` | yes | Shown in every message |
| `printer` | yes | A name from `printers.json` |
| `model` | yes | STL, 3MF or STEP. Its SHA-256 is recorded at submit; if the file changes afterwards the job refuses to slice |
| `presets` | yes | A preset set: a directory holding `machine.json`, `process.json`, `filament.json`. Relative names resolve against the printer's `presets_root` |
| `material` | no | Checked against the filament preset, and against the loaded spool before starting |
| `copies` | no | Number of copies to arrange on the plate (default 1) |
| `overrides` | no | OrcaSlicer settings to change for this job only. Keys are Orca's own setting names |
| `expect` | no | Settings that must have these values in the sliced G-code, without changing anything. Use it to pin what matters about the preset set |
| `notes` | no | Free text for the agent: what to tell the operator, what is unusual |

`printq check ticket.json` validates a ticket without queueing it. A field that is not in this table is an error, so a misspelt optional field cannot be dropped silently.

## Macro jobs

A ticket with `"kind": "macro"` runs commands on the printer; there is nothing to slice. Use it for work that should wait its turn like a print does: a probe calibration, a PID tune, a bed mesh. `printq blank printer-1 --macro` gives the blank ticket.

```json
{
  "kind": "macro",
  "title": "Hotend PID tune at 260",
  "printer": "printer-1",
  "gcode": ["M106 S255", "PID_CALIBRATE HEATER=extruder TARGET=260", "M106 S0", "SAVE_CONFIG"],
  "bed": "any",
  "notes": "Part fan at print speed during the tune."
}
```

| Field | Required | Meaning |
|---|---|---|
| `kind` | yes, `"macro"` | A ticket without `kind` is a print |
| `title`, `printer`, `notes` | as for a print | |
| `gcode` | yes | Commands, one per string. They run in order, each one finishing before the next is sent. The first error stops the job |
| `bed` | no | `"clear"` (default): the job runs only after the camera bed check, like a print. `"any"`: the bed may hold a part |
| `timeout_sec` | no | Longest a single command may take (default 1800) before the job is reported as stuck |
| `heaters_off` | no | Send `TURN_OFF_HEATERS` when the commands have finished (default true) |

A macro job passes the same gates as a print: the printer must be idle, the bed must be seen clear unless the ticket says `"bed": "any"`, and the printer's start mode (`confirm`, `veto`, `auto`) applies. While it runs, no other job starts on that printer. When it ends you get the last lines the printer wrote to its console, which is where a PID tune or a calibration reports its result.

Things to know:

- **Choose `bed` honestly.** Anything that homes, probes or travels over the bed needs `"clear"`. `"any"` is for work that leaves the toolhead where it is.
- **Restarts are waited out.** After `SAVE_CONFIG`, `RESTART` or `FIRMWARE_RESTART` the queue waits until Klipper reports ready before going on. A restart ends anything else the printer was doing, a cool-down macro included.
- **A failed command turns the heaters off** and the job asks for you with the printer's own error message. `/retry` runs the whole list again from the start, after a new bed check.
- **A timeout is not a cancel.** Klipper keeps running a command after the queue stops waiting for it. The message says so; look at the printer before retrying.
- **If the queue itself is restarted** while a macro job is running, the job is not run again by itself: how far it got is unknown, so it asks for you.
- The commands are sent as written. printq does not judge whether they are sensible for your printer.

## Overrides and verification

Overrides are merged into temporary copies of the three preset files, so a preset set is never modified by a job. A key that exists in none of the three files is an error at slicing time: a misspelt setting name would otherwise be ignored silently by the slicer.

After slicing, printq reads the `CONFIG_BLOCK` that OrcaSlicer writes at the end of every G-code file. That block is the slicer's own record of the values it used. Each `overrides` and `expect` entry is compared with it, and any difference fails the job before anything reaches the printer. The filament and time figures in the queue announcement come from the same file, so they are OrcaSlicer's own numbers.

## Preset sets

The OrcaSlicer CLI does not load the presets you see in the GUI: it needs each file to carry a `"type"` key and does not resolve `inherits`. The dependable way to get a correct set is to take it from a print that worked:

1. Slice and print a job from the GUI with the settings you want as your baseline.
2. Rebuild the three JSON files from that G-code's `CONFIG_BLOCK`, which holds every resolved value.
3. Slice the same model with the CLI and compare the new `CONFIG_BLOCK` with the original. Only metadata should differ.

Notes from doing this with OrcaSlicer 2.5:

- In the machine file, leave `inherits` unset and `compatible_printers` empty, or the CLI reports that the preset was not found in the loaded bundle.
- Values that are per-extruder lists in the GUI stay lists (`"nozzle_temperature": ["260"]`). An override given as a single value is expanded to the preset's own shape.
- If a printer profile has a bed exclusion area you do not use, a value in the preset is not enough when slicing; set `bed_exclude_area` in `printers.json` and printq passes `--bed-exclude-area` on the command line.
- If each printer has its own OrcaSlicer data directory, name it in `orca_datadir`.

Keep one set per printer and material as the default, and make a new dated set when a job needs something structurally different. A ticket's `overrides` are for the small differences.
