# The print desk

`printq desk` serves one page (default http://localhost:8766/) where a file becomes a job. It is a second way to write a ticket, not a second queue: everything it queues goes through `submit` and passes the same gates as any other job.

## Three ways in

| Way in | What you do | What happens |
|---|---|---|
| Drop | Drag an STL, 3MF or STEP file onto the page | A draft appears on the page |
| Export | Save a model anywhere under `PRINTQ_DESK_WATCH` | A draft appears, and the page opens in your browser if it is not open already |
| Slicer | Press Print in the slicer, with this service as its print host | The G-code is queued as it is. With Upload instead of Print it waits as a draft |

A draft is only a file and three questions: which printer, which filament, which settings.

## What the page does with a draft

- **Recognises a reprint.** printq keeps the SHA-256 of every model it prints. The page says whether this exact file was printed before, or an earlier version of it (same path, or same name, different contents), on which printer and with what estimate.
- **Proposes the settings.** A part printed before on the chosen printer gets its own last settings: preset set and overrides. Any other part gets the settings of that printer's most recent job in the chosen material. "Yes, change settings" opens the preset set and the overrides for editing.
- **Offers the filament from inventory.** With `PRINTQ_SPOOLMAN_URL` set, the picker lists every filament with a spool on hand, grouped by material, and marks what is loaded on which printer. Materials the printer has no preset set for are shown but cannot be picked.
- **Slices a preview.** Print is locked until the draft has been sliced with exactly the settings on screen, so the time and weight shown are the slicer's own. Changing a setting locks it again.
- **Takes a pointer to print.** The Print button ignores the keyboard. After queueing, "Take it back out" drops the job.

The picture is the STL as exported, in the orientation it will print. Supports and brim are not drawn. A part that needs orienting or supports belongs in the slicer: "Open in OrcaSlicer instead" runs the printer's `orca_gui` command on the file.

## Filament and queue order

A ticket queued here carries `filament`, the Spoolman filament id. Two things follow:

- **Grouping.** For each printer the queue first runs every job the loaded spool can print, oldest first. Only then does it ask for another spool, taking the filament whose oldest job has waited longest. Jobs of one filament therefore run back to back, whatever order they were queued in.
- **Waiting for the spool.** A job whose filament is not loaded waits, and says so once: "Load X and make it the active spool". `/go` prints with what is loaded. A different *material* is never overridden.

Where a printer reports no active spool, the filament of its last print is taken to be still loaded.

## Using the slicer's Print button

In the slicer's printer settings, set the print host:

| Field | Value |
|---|---|
| Host type | Octo/Klipper |
| Hostname, IP or URL | `http://localhost:8766/p/<printer name from printers.json>` |
| API key | the value of `PRINTQ_DESK_KEY` |
| Device UI (if the slicer has the field) | the printer's own address, so the device tab still shows the printer |

The file is queued as a `gcode` job. Nothing is sliced again; printq reads the estimate from the file, checks the printer model against `gcode_printer_model` and the material against the file, and from there the job is like any other: bed check, start mode, messages.

Without `PRINTQ_DESK_KEY` this way in is closed.

## Settings

In `$PRINTQ_HOME/env`: `PRINTQ_DESK_PORT`, `PRINTQ_DESK_BIND`, `PRINTQ_DESK_WATCH`, `PRINTQ_DESK_OPEN`, `PRINTQ_DESK_KEY` (see `config/env.example`).

In `printers.json`, per printer:

| Key | Meaning |
|---|---|
| `gcode_printer_model` | Part of the slicer's `printer_model` for this printer. G-code sliced for another model is refused |
| `preset_machine` | Part of the machine preset's name. Says which preset sets under a shared `presets_root` are this printer's |
| `orca_gui` | Command (a list) that opens the slicer for this printer; the file is appended |

## Who can reach it

The page has no login. It listens on localhost, refuses requests addressed to a host name it does not know, and takes changes only from its own page (a header other sites cannot send) or from a slicer that knows the key. It cannot start a print: it submits tickets, and the queue's gates decide the rest. Think before setting `PRINTQ_DESK_BIND` to anything wider.
