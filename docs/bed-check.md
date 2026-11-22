# Bed check

Starting a print onto a bed that still holds the last part drives the nozzle into it. The bed check exists to make that impossible without also making you walk to the printer for every job.

## Verdicts

| Verdict | Meaning | Effect |
|---|---|---|
| `clear` | The bed surface is plainly in view and nothing is on it | The job may be uploaded and started, for the next 300 s |
| `occupied` | A part or other object is on the bed | You are told once, with the photo. The job waits and the bed is looked at again every `recheck_sec` (default 5 minutes) until it is clear |
| `cannot_tell` | Everything else | As for `occupied`: told once, then looked at again. `/go` settles it if you have looked yourself |

`cannot_tell` covers a camera that does not answer, a frame too dark to read, a bed that could not be brought into view, an unsure judge, and a judge that failed in any way. There is no path from an error to `clear`.

## The judge

The judge is a vision model that gets one frame, the name and height of the last job printed (the object most likely to still be there), and a five-way question: `bed_clear`, `object`, `bed_partial`, `bed_not_visible`, `unsure`.

`PRINTQ_VISION_MODEL` selects it:

| Value | Judge |
|---|---|
| `claude-opus-5-5` (default) | Claude through the Anthropic API, with `PRINTQ_ANTHROPIC_API_KEY`. One small image per look |
| `ollama:<model>` | A local vision model on your Ollama server |
| `fixed:<view>` | Always the same answer. For tests and dry runs |

`PRINTQ_SHADOW_VISION_MODEL` names a second judge that sees every frame too. Its answer is written to the dataset and never acted on, which is how you find out whether a local model is good enough before trusting it.

### Why not a local general-purpose vision model

It was tried first. A 12B local vision model, asked about a frame in which the bed had dropped almost entirely out of view, answered that the bed was clearly visible and empty, with high confidence. It did correctly spot a two-layer part on the bed in another frame. A judge that is right most of the time and confidently wrong about what it cannot see is the wrong tool for a decision whose error cost is a crashed machine. A model trained on your own printer's frames is a different matter; see [training-your-own-model.md](training-your-own-model.md).

## Bringing the bed into view

On printers where the bed moves in Z, the bed sits low after a print and the camera sees little or none of it. Near the top of its travel the camera sees the whole plate, including the corners nearest the lens. With `"inspect": {"mode": "raise_bed", ...}` printq raises the bed one step at a time, looks after each step, and judges the bed from that height.

The rule behind it: **while the toolhead is parked clear of the bed and the camera sees nothing coming into view, one more step up is always safe.** Nothing is above a part that rises with the bed.

1. Take a frame. Judge it, and measure where the bed's far edge sits in it (see [Bed height by camera](#bed-height-by-camera)).
   - `object`: verdict `occupied`. A tall part rises into view before the bed surface does, and this is where it is caught. If only its top is just entering at the bottom edge of the frame, the bed is first raised one more step (`object_confirm_steps`, default 1) for a closer look, under the same guards as any other raise. The verdict is `occupied` whatever the closer frame shows: a sighting is never overruled.
   - `bed_clear` at inspection height: verdict `clear`.
   - `bed_clear` lower down: treated as `bed_partial`. More of the plate shows from higher up, so the bed keeps rising.
   - `bed_partial`, `bed_not_visible`: continue, unless the bed is already at inspection height, which ends in `cannot_tell`.
   - `unsure`: stop, verdict `cannot_tell`.
2. If the printer has a `park_reference`, the judge must also report the toolhead as `parked` in this frame. Otherwise nothing moves.
3. Before the first move, if the motors were released after the last print, establish where the toolhead is. The bed moves only once the toolhead is known to be at the park position:
   - it reads as being at `park_xy`: Klipper is told where the axes are (`SET_KINEMATIC_POSITION`);
   - it reads as being at `also_from_xy` with X and Y homed: it is moved to the park first;
   - Klipper has restarted and reports nothing homed, and the printer has `"unknown_position": "home_xy"`: X and Y are homed and the toolhead is parked;
   - anything else: nothing moves.
4. Raise the bed by `step` mm and go back to 1.

### Bed height by camera

Klipper keeps reporting the last Z it knew after the motors are released. By then the bed may have sunk, or been moved by hand, in either direction, so that number is not used at all: Klipper is handed a placeholder Z to make the relative raises legal, and the motors are released again (`M84`) when the check ends so that nothing later trusts it.

The camera supplies the height instead. As the bed rises, its far edge climbs the frame, and the row of that edge is a direct reading of bed height. Two reference frames calibrate it:

- `park_reference`: the toolhead parked, the bed out of view;
- `clear_reference`: the empty bed at the height you want it inspected from.

Their difference is the bed, which gives its colour; the far edge in the clear reference is the row the bed is raised towards. The bed stops once its edge is within one raise of that row, and never passes it. This is ordinary image arithmetic, not a model, and it stops the raise whatever the judge says.

Without both reference frames a printer whose Z is unknown is not moved. The reading depends on the plate's colour and the lighting, so new plates and moved cameras need new reference frames. Every frame's reading is saved in the dataset as `bed_row`.

The limits, each of which ends the procedure with `cannot_tell`:

| Limit | Reason |
|---|---|
| No motion unless the toolhead is proven to be at `park_xy` by one of the three routes above | Elsewhere a rising part could meet the nozzle |
| No motion unless the camera shows the toolhead at the park, when `park_reference` is set | Klipper's X and Y are also only what it last knew; a toolhead pushed by hand reads as parked |
| No raise once the bed's far edge is within one raise of the reference row | The bed never goes above the height the clear reference was taken at |
| No motion when Z is unknown and the printer lacks the two reference frames | Nothing but the judge would stop the raise at the top |
| With Z really homed: never below `stop_z` (default 1), nor below the last object's height + `object_margin` | A Z that Klipper knows is honoured as well |
| At most `max_steps` raises (default 26) | A bound that needs neither Z nor the camera |
| Z is never homed | Homing drives the bed up to the nozzle, through anything on it |

`unknown_position: "home_xy"` is opt-in because it relies on homing X and Y not moving the bed towards the nozzle (check your `homing_override`). `home_xy_gcode` (default `G28 X Y`) sets the command.

### `inspect` settings

```json
"inspect": {
  "mode": "raise_bed",
  "park_xy": [202, 264],
  "also_from_xy": [187, 250],
  "step": 10,
  "stop_z": 1,
  "object_margin": 20,
  "max_steps": 26,
  "object_confirm_steps": 1,
  "settle_sec": 2,
  "unknown_position": "home_xy",
  "park_reference": "reference/printer-1-park.jpg",
  "clear_reference": "reference/printer-1-clear.jpg"
}
```

`park_xy` is where your end-of-print G-code leaves the toolhead, and it must be clear of the bed. Use `"mode": "static"` for printers whose bed does not move in Z or is always in view: one frame, no motion.

### Recognising the parked toolhead

`park_reference` names a frame from the same camera with the toolhead at its park position (relative paths are looked up in `$PRINTQ_HOME`). The judge is shown that frame beside the live one and reports the toolhead as `parked`, `elsewhere` or `not_visible`. The bed is raised only on `parked`.

This closes a gap in the position check: with the motors off, Klipper's X and Y are as stale as its Z. The camera sees where the toolhead actually is.

To make a reference, park the toolhead, check the frame by eye, and copy it from the dataset directory (every frame the bed check takes is saved there). Replace it whenever the camera is moved. The answer is recorded with every frame as `toolhead`, so the frames also train this part of [your own model](training-your-own-model.md).

### Showing the judge the empty bed

`clear_reference` names a frame from the same camera with the bed at inspection height and nothing on it. Take it near the top of the travel, where the camera sees the whole plate. The judge is shown it beside the live frame, told that this surface is the bed, and asked to report anything on the bed that the reference does not show. It answers `bed_clear` only when the live frame shows about as much of the bed as the reference does.

Without it, the rear corner of the bed entering at the bottom of the frame can be taken for a flat part rising into view, most easily when the plate is brightly coloured.

## Start modes

Set per printer in `printers.json`:

| `start_mode` | After a `clear` verdict |
|---|---|
| `confirm` (default) | You get the photo and the job waits for `/go` |
| `veto` | You get the photo; the print starts after `veto_sec` seconds unless you send `/hold`. All gates are checked again when the countdown ends |
| `auto` | The print starts at once |

## Trying it

```bash
printq bedcheck <printer>
```

runs the check now and prints the verdict. On a `raise_bed` printer this moves the bed, so do it while you are watching the machine.
