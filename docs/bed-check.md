# Bed check

Starting a print onto a bed that still holds the last part drives the nozzle into it. The bed check exists to make that impossible without also making you walk to the printer for every job.

## Verdicts

| Verdict | Meaning | Effect |
|---|---|---|
| `clear` | The bed surface is plainly in view and nothing is on it | The job may be uploaded and started, for the next 300 s |
| `occupied` | A part or other object is on the bed | Escalated to you with the photo |
| `cannot_tell` | Everything else | Escalated to you with the photo |

`cannot_tell` covers a camera that does not answer, a frame too dark to read, a bed that could not be brought into view, an unsure judge, and a judge that failed in any way. There is no path from an error to `clear`.

## The judge

The judge is a vision model that gets one frame, the name and height of the last job printed (the object most likely to still be there), and a four-way question: `bed_clear`, `object`, `bed_not_visible`, `unsure`.

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

On printers where the bed moves in Z, the bed sits low after a print and the camera may see none of it. With `"inspect": {"mode": "raise_bed", ...}` printq raises the bed one step at a time and looks after each step:

1. Take a frame and judge it.
   - `object`: stop, verdict `occupied`. A tall part rises into view before the bed surface does, and this is where it is caught.
   - `bed_clear`: stop, verdict `clear`.
   - `unsure`: stop, verdict `cannot_tell`.
   - `bed_not_visible`: continue.
2. Before the first move, if the motors were released after the last print, establish where the toolhead is. The bed moves only once the toolhead is known to be at the park position, clear of the bed:
   - it reads as being at `park_xy`: Klipper is told where the axes already are (`SET_KINEMATIC_POSITION`);
   - it reads as being at `also_from_xy` with X and Y homed: it is moved to the park first;
   - Klipper has restarted and reports nothing homed, and the printer has `"unknown_position": "home_xy"`: X and Y are homed, the toolhead is parked, and Z is *assumed* to be `rest_z`, the height your end-of-print G-code leaves the bed at. When the check ends the motors are released again (`M84`) so that nothing later trusts the assumed Z;
   - anything else: nothing moves.
3. Raise the bed by `step` mm and go back to 1.

Why this is safe: with the toolhead parked off the bed, nothing is above a part that rises with the bed, and a bed that is out of the camera's view is far below the gantry. The camera decides when to stop, so neither the exact Z nor the height of the last part has to be known.

The limits, each of which ends the procedure with `cannot_tell`:

| Limit | Reason |
|---|---|
| No motion unless the toolhead is proven to be at `park_xy` by one of the three routes above | Elsewhere a rising part could meet the nozzle |
| Z never goes below `min_z`, or below the last object's height + `object_margin` when that height is known | The bed is fully in view by `min_z`; a known part keeps its top clear of the nozzle plane as well |
| At most `max_steps` raises | A judge that never sees the bed must not keep going |
| Z is never homed | Homing drives the bed up to the nozzle, through anything on it |

An unknown last object (for example after a Klipper restart) no longer prevents the raise: the park position is what makes it safe.

`unknown_position: "home_xy"` is opt-in because it relies on two things about your machine: homing X and Y must not move the bed towards the nozzle (check your `homing_override`), and the bed must rest at `rest_z` or lower after every print. `home_xy_gcode` (default `G28 X Y`) sets the command.

### `inspect` settings

```json
"inspect": {
  "mode": "raise_bed",
  "park_xy": [202, 264],
  "also_from_xy": [187, 250],
  "step": 10,
  "min_z": 95,
  "object_margin": 20,
  "max_steps": 10,
  "settle_sec": 2,
  "unknown_position": "home_xy",
  "rest_z": 170
}
```

`park_xy` is where your end-of-print G-code leaves the toolhead, and it must be clear of the bed. `min_z` is the highest you ever want the bed raised for inspection; pick it by watching where the bed comes fully into the camera's view. Use `"mode": "static"` for printers whose bed does not move in Z or is always in view: one frame, no motion.

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
