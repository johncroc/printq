# Training your own bed-check model

"Is this printer's bed clear?" is close to an ideal problem for a small dedicated model. The camera is fixed, the lighting is yours, the background never changes, and the answer is one of a few classes. A general vision model has to work it out from first principles every time; a classifier trained on a few hundred frames from your own machines only has to recognise what is different from an empty bed.

It needs labelled frames first, and collecting those is the slow part. printq does it as a side effect of normal use.

## What printq collects

Every frame the bed check takes is saved under `$PRINTQ_DATA/dataset/<printer>/` and indexed in `dataset/index.jsonl`:

```json
{"frame": "printer-1/20261119-183807.jpg", "printer": "printer-1", "time": "20261119-183807",
 "judge": "claude-opus-5-5", "view": "bed_clear", "description": "...",
 "shadow": {"judge": "ollama:gemma4:12b", "view": "bed_clear"},
 "job": "j12", "z": 99.7, "last_file": "bracket.gcode"}
```

Two kinds of label come for free:

- **The judge's verdict** on every frame, including the `bed_not_visible` frames taken while the bed is being raised. Those matter: a model that cannot say "I can't see the bed" is the failure described in [bed-check.md](bed-check.md#why-not-a-local-general-purpose-vision-model).
- **Ground truth from the machine.** When a job printq started finishes, a frame is saved with `"label_hint": "occupied"`: the part is certainly there. Each print therefore adds a reliable positive, with whatever geometry and colour you happened to print.

Your own decisions add a third: a `/go` after an `occupied` or `cannot_tell` verdict means the judge was wrong or blind for that frame, and the job's `events.log` records it.

## How much data

As a starting point, not a measured result: a few hundred frames per printer, with every class represented and the occupied class covering small, flat and bed-coloured parts. Those hard cases are worth photographing on purpose: put offcuts and old prints on the bed, at the inspection height, and run `printq bedcheck`.

Frames from one printer should not be assumed to transfer to another camera position.

## A sensible route

1. **Collect** with the API judge in charge. Set `PRINTQ_SHADOW_VISION_MODEL` to any local candidate to see how it would have done on the same frames.
2. **Review the labels.** Read through `index.jsonl` beside the frames and correct what the judge got wrong. A few hundred images is an evening.
3. **Train a small classifier.** Fine-tuning a compact pretrained image backbone (a ResNet-18 or a small ViT) on four classes fits comfortably on a 16 GB GPU and trains in minutes at this scale. Hold out whole print jobs, not random frames, for validation: consecutive frames of one inspection are nearly identical and would leak.
4. **Measure the right error.** Accuracy is not the number that matters. Count the *false clears*: frames labelled `object` or `bed_not_visible` that the model called `bed_clear`. Choose the decision threshold so that count is zero on the validation set, and accept more `unsure` answers as the price.
5. **Run it in shadow.** Serve the model and name it in `PRINTQ_SHADOW_VISION_MODEL`. It now sees every live frame without deciding anything.
6. **Promote it** by putting it in `PRINTQ_VISION_MODEL` once its shadow record has no false clears over enough jobs to convince you. Keeping the API judge as the shadow for a while afterwards costs little.

Steps 5 and 6 need a small adapter: `bedcheck.judge()` routes on the model name, and a locally served classifier is one more branch there returning one of the four views.

Until then the default judge costs one small image per look, a handful of looks per job.
