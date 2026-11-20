# Handoff from a design session

printq begins where a design conversation ends. The handoff is deliberately a file, not a conversation between agents: the design assistant writes a [ticket](tickets.md), and from then on the local agent works only from what the ticket says.

That boundary is what lets the two sides be very different. The design side can be a large hosted model with your CAD tools and the whole history of the part. The print side is a small local model with eight tools and no memory. Neither needs to trust the other, because the ticket is validated at submit and verified again after slicing.

## What the designer is responsible for

- The user's explicit approval of this version of the part.
- The exported model file, which must not change after submit.
- The preset set and any overrides, using OrcaSlicer's setting names.
- `expect` entries for the settings that matter, so a wrong preset set fails loudly.
- Notes for the agent about anything unusual.

## What the designer must not do

Start the print by another route once the job is queued. The queue assumes it is the only thing starting prints on a printer it manages.

## Claude Code skill

[`skills/print-handoff/SKILL.md`](../skills/print-handoff/SKILL.md) teaches a Claude Code session the procedure. Install it by linking it into your skills directory:

```bash
ln -s "$PWD/skills/print-handoff" ~/.claude/skills/print-handoff
```

Any assistant that can write a JSON file and run `printq submit` can do the same; the skill is the procedure written down.
