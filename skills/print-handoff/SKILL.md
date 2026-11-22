---
name: print-handoff
description: Hand an approved design to the printq print queue. Use when the user approves a design for printing, says to queue, print or "send it to the printer", or asks what is in the print queue.
---

# Handing a design to the print queue

The user and you have been working on a part. They have now approved it for printing. From here a local agent takes over: it slices the part, tells the user it is queued, and starts the print when the bed is clear. Your job is to write down everything that agent needs, once, correctly. It is a small model with no knowledge of this conversation; whatever is not in the ticket does not exist for it.

Approval has to be explicit and has to be for this version of the part. If the design changed after the user last looked at it, show them the change before queueing.

## Steps

1. **Export the model** the way this user's projects normally do, to its usual place. Use the exported file, not the CAD document, as `model`. The queue records the file's hash; do not touch the file afterwards.

2. **Ask for a blank ticket:** `printq blank <printer> > ticket.json`. It is the current ticket format, with the required and optional fields explained under `_help`, plus this printer's preset sets and the set its last job used. Work from it, not from memory of an earlier ticket: the format changes.

3. **Choose the preset set** from the ones the blank ticket lists. The default is the set of that printer's most recent job, unchanged. Use `overrides` only for settings you and the user agreed for this part (infill, brim, supports, wall count, layer height), with OrcaSlicer's own setting names. Put the values that matter for this part in `expect` so the queue verifies them: at least layer height and nozzle temperature.

4. **Think about what the slicer cannot know.** Tall thin features with short layer times, overhangs that need supports, orientation: settle these with the user now. The ticket prints what it says.

5. **Fill in the ticket.** Ask the user for whatever you do not know; do not guess a required field. Write `notes` for the agent in plain sentences: anything unusual, and anything the user wants to be told when it is announced.

6. **Validate, then submit:**

   ```bash
   printq check ticket.json     # resolves paths, confirms the preset set exists
   printq submit ticket.json    # prints the job id
   ```

7. **Tell the user** the job id, the printer, and what happens next: they will get the queue message with OrcaSlicer's filament and time estimate within a couple of minutes, and the print starts according to that printer's start mode once its bed is seen clear.

## Macro jobs

A calibration, PID tune or other printer routine can be queued the same way, with nothing to slice: `printq blank <printer> --macro`. Fill in `gcode` (one command per string) and decide `bed` with the user: `"clear"` unless the routine truly never moves over the bed. Mention in `notes` anything the operator should do first. The result arrives in the finish message as the printer's own console output.

## Afterwards

`printq queue` and `printq show <id>` report progress. If the user changes their mind, `printq hold <id>` or `printq drop <id>`. If the design changes after submitting, drop the job and submit a new ticket: a queued ticket is never edited.

Do not start the print yourself by another route once a job is queued. Two starters is how a print lands on an occupied bed.
