# Training your own bed-check model

"Is this printer's bed clear?" is close to an ideal problem for a small dedicated model. The camera is fixed, the lighting is yours, the background never changes, and the answer is one of a few classes. A general vision model has to work it out from first principles every time; a classifier trained on a few hundred frames from your own machines only has to recognise what is different from an empty bed.

It needs labelled frames first, and collecting those is the slow part. printq does it as a side effect of normal use.

## What printq collects

Every frame the bed check takes is saved under `$PRINTQ_DATA/dataset/<printer>/` and indexed in `dataset/index.jsonl`:

```json
{"frame": "printer-1/20261119-183807.jpg", "printer": "printer-1", "time": "20261119-183807",
 "judge": "claude-opus-5-5", "view": "bed_clear", "toolhead": "parked", "description": "...",
 "shadow": {"judge": "ollama:gemma4:12b", "view": "bed_clear"},
 "job": "j12", "x": 202.0, "y": 264.0, "z": 99.7, "homed": "xyz", "last_file": "bracket.gcode"}
```

Each row also carries `check` (the bed check the frame belongs to), `step` (how many raises had been made), `bed_row` and `bed_target` (where the bed's far edge is in the frame, and where it is at inspection height), `last_height` (height of the last print, when known) and `refs` (a fingerprint of the reference frames, which changes when the camera is moved and the references are retaken).

## Labels

What the judge said is not ground truth. Ground truth is kept separately, in `dataset/labels.jsonl`, one appended row per statement about the bed:

```json
{"time": "20261126-101502", "printer": "printer-1", "label": "clear", "source": "operator",
 "check": "printer-1-20261126-100449", "frame": "printer-1/20261126-100544.jpg", "job": "j7",
 "judged": "occupied", "note": ""}
```

A row with a `check` applies to every frame of that check; a row with only a `frame` applies to that frame. A later row replaces an earlier one. `judged` is what the judge had said, so its mistakes can be counted.

| Source | When it is written | Label | How far to trust it |
|---|---|---|---|
| `operator` | `/clear ID`: the bed was already clear in the photo the check sent | `clear`, for the check | You looked. These are the judge's false alarms, the most useful frames there are. |
| `operator` | `/occupied ID`: the photo is right, or the check called an occupied bed clear | `occupied`, for the check | You looked. If the check had said clear, the job is also put on hold. |
| `operator` | `/go ID` or `/clear ID` | `clear`, for one fresh frame taken at that moment | You looked. The bed may be low or out of view in it. |
| `print_end` | a print printq started has finished | `occupied`, for one frame | Certain: the part is there. |
| `print_completed` | a print ran to the end on a bed the check called clear | `clear`, for the check | Indirect. Leave it out if you want only first-hand labels. |

`/go` on its own says the bed is clear now. It says nothing about the earlier photo, which may show the part you have just removed, so it does not label that check.

`printq dataset` (or `/dataset`) counts what has been collected per printer and label, and how often you corrected the judge. `printq dataset manifest.csv` writes the training manifest: one row per frame with its label, the label's source, the judge's answer and the fields above. Its `group` column is the check the frame came from; split on it.

## How much data

As a starting point, not a measured result: a few hundred frames per printer, with every class represented and the occupied class covering small, flat and bed-coloured parts. Those hard cases are worth photographing on purpose: put offcuts and old prints on the bed, at the inspection height, and run `printq bedcheck`.

Frames from one printer should not be assumed to transfer to another camera position.

## A sensible route

1. **Collect** with the API judge in charge. Set `PRINTQ_SHADOW_VISION_MODEL` to any local candidate to see how it would have done on the same frames.
2. **Review the labels.** Answer the check's messages with `/clear` and `/occupied` as you go, then read through the manifest beside the frames and correct what is left. A few hundred images is an evening.
3. **Train a small classifier.** Fine-tuning a compact pretrained image backbone (a ResNet-18 or a small ViT) on five classes fits comfortably on a 16 GB GPU and trains in minutes at this scale. Hold out whole checks (the manifest's `group` column), not random frames, for validation: consecutive frames of one inspection are nearly identical and would leak.
4. **Measure the right error.** Accuracy is not the number that matters. Count the *false clears*: frames labelled `object` or `bed_not_visible` that the model called `bed_clear`. Choose the decision threshold so that count is zero on the validation set, and accept more `unsure` answers as the price.
5. **Run it in shadow.** Serve the model and name it in `PRINTQ_SHADOW_VISION_MODEL`. It now sees every live frame without deciding anything.
6. **Promote it** by putting it in `PRINTQ_VISION_MODEL` once its shadow record has no false clears over enough jobs to convince you. Keeping the API judge as the shadow for a while afterwards costs little.

Steps 5 and 6 need a small adapter: `bedcheck.judge()` routes on the model name, and a locally served classifier is one more branch there returning one of the five views.

Until then the default judge costs one small image per look, a handful of looks per job.
