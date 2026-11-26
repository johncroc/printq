# Operations

## Install

```bash
git clone https://github.com/johncroc/printq && cd printq
uv venv && uv pip install -e .
```

printq runs on the machine that has OrcaSlicer, the model files and the Ollama server. That is normally your design workstation.

## Configuration

Two files, both outside the repository, in `~/.config/printq/` (override with `PRINTQ_HOME`):

**`printers.json`**, from [`config/printers.example.json`](../config/printers.example.json):

| Key | Meaning |
|---|---|
| `url` | Moonraker base URL, with port if it is not 80 |
| `orca_datadir` | OrcaSlicer data directory for this printer, if you keep one per printer |
| `presets_root` | Where relative `presets` names in tickets are looked up |
| `bed_exclude_area` | Passed to the slicer as `--bed-exclude-area`; `"0x0"` removes a profile's exclusion zone |
| `snapshot_url` | Camera snapshot URL, if the first webcam in Moonraker is not the right one |
| `start_mode`, `veto_sec` | See [bed-check.md](bed-check.md#start-modes) |
| `recheck_sec` | How often a bed that was not seen clear is looked at again (default 300). The job keeps its place in the queue and goes ahead by itself once the bed is clear; you are told once when the wait begins |
| `cooldown_macro` | Name of a macro that keeps running after the print file has ended, for example a staged bed cool-down. It must expose a variable `active` (and may expose `stage` and `stages` with a `hold` in seconds each, used for the time estimate). While `active` is set the printer is not idle: no bed check, print or macro job starts, and a finished print is announced as "extrusion complete, entering cooldown" first and "ready to be cleared" only when the macro is done |
| `inspect` | See [bed-check.md](bed-check.md#inspect-settings) |

**`env`**, from [`config/env.example`](../config/env.example), mode 600:

| Variable | Meaning |
|---|---|
| `PRINTQ_ORCA_BIN` | OrcaSlicer executable |
| `PRINTQ_ANTHROPIC_API_KEY` | Claude API key for the bed-check judge |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | The queue's bot and the chat it serves |
| `PRINTQ_AGENT_MODEL` | Ollama model that drives the agent (default `gpt-oss:20b`) |
| `PRINTQ_OLLAMA_URL` | Default `http://localhost:11434` |
| `PRINTQ_VISION_MODEL`, `PRINTQ_SHADOW_VISION_MODEL` | See [bed-check.md](bed-check.md#the-judge) |
| `PRINTQ_SPOOLMAN_URL` | Spoolman API base, e.g. `http://spoolman/api/v1`. Enables spool checks |
| `PRINTQ_BED_OK_TTL` | Seconds a clear verdict stays valid (default 300) |

`printq doctor` checks all of it and reports what is missing.

The bed-check key is deliberately not read from `ANTHROPIC_API_KEY`: the agent harness sets the standard `ANTHROPIC_*` variables to point at the local model server, and the judge must not follow them there.

## Telegram

The queue needs its own bot. Telegram delivers updates to one consumer per bot, so a bot that another service already polls cannot be shared.

1. Create a bot with [@BotFather](https://t.me/BotFather) and put its token in `env`.
2. Start a chat with the bot (or add it to a group) and send any message.
3. Find the chat id: `curl -s "https://api.telegram.org/bot<token>/getUpdates"` and read `message.chat.id`. Put it in `env`.

Only messages from that chat are obeyed. Without a token, messages go to `$PRINTQ_DATA/notify.log` and the same commands work from the CLI.

## Commands

In Telegram, the id can follow an underscore so the command is one tap: `/go_j12`.

| Telegram | CLI | Effect |
|---|---|---|
| `/queue` | `printq queue` | Active jobs and their states |
| `/show j12` | `printq show j12` | One job: state, estimate, last reason |
| `/hold j12` | `printq hold j12` | Keep the job from launching; also stops a veto countdown |
| `/release j12` | `printq release j12` | Undo hold |
| `/go j12` | `printq go j12` | You checked the bed yourself: start it. See [architecture.md](architecture.md#operator-override). During the last print's cool-down it also stops the cool-down and holds the bed at its current temperature, so the next job does not wait for a bed that is already clear |
| `/clear j12` | `printq clear j12` | Like `/go`, and the bed was already clear in the photo the check sent: kept as a label, see [training-your-own-model.md](training-your-own-model.md#labels) |
| `/occupied j12` | `printq occupied j12` | The bed holds something: kept as a label; a job the check had allowed is put on hold |
| `/dataset` | `printq dataset [FILE.csv]` | Bed frames and labels collected; with a file name, the training manifest |
| `/success j12`, `/partial j12`, `/failure j12 [note]` | `printq success j12` ... | How the part came out, kept in the print's record |
| `/retry j12` | `printq retry j12` | Run the step that stopped again |
| `/drop j12` | `printq drop j12` | Remove the job |

## Print records

Every print the queue starts leaves one JSON record in `$PRINTQ_DATA/sessions/`, named `<printer>_<date>_<time>.json`. It is written at the start and completed at the end:

| Field | Content |
|---|---|
| `session_id`, `job`, `printer`, `title` | Which print this was |
| `filament` | Type, filament preset and, with Spoolman, the active spool |
| `profile`, `settings`, `overrides` | The preset set and the settings the G-code was sliced with |
| `gcode_hash`, `model_sha256`, `estimate` | Exactly what was printed, and the slicer's estimate |
| `bed_check` | The check that let it start: its id in the bed dataset, and whether the camera or the operator gave the verdict |
| `metrics_start`, `extrusion_end`, `metrics_end` | UTC times, for joining with temperature logs, camera frames or any other time series |
| `printer_result` | How the printer says it ended |
| `outcome`, `outcome_note` | `success`, `partial` or `failure`, as you judged the part. Empty until you answer the finish message |
| `anomalies` | What happened to the print, with times: pauses, an abnormal end |
| `interventions` | What was done to the job, with times: hold, release, go, clear, occupied, retry, drop |

The printer's verdict and yours are kept apart on purpose: a print can run to the end and still be scrap.

## Utilization

A printer earns only while it prints. The queue records what every configured printer is doing, all the time, including prints it did not start, and `printq util [DAYS] [PRINTER]` (or `/util` in Telegram) reports the share of time spent printing and where the rest went:

| Group | What is in it |
|---|---|
| `active` | Printing |
| `process` | What the workflow itself costs: heating and calibrating before a print, the cool-down after it, bed check and upload, slicing, macro jobs |
| `operator` | Waiting for a person: a paused print (spool change), a bed that needs clearing, a job waiting for `/go`, on hold, or stopped with a question |
| `no_work` | Idle with nothing queued for that printer |
| `down` | Unreachable or in an error state |

The record is one line per change in `utilization/<printer>.jsonl` under the data directory (`{"t": unix time, "state": ..., "job": ...}`; a state lasts until the next line), sampled every 30 seconds. Time during which the queue was not running is counted as not recorded and left out of the percentages.

## Running as a service

```bash
mkdir -p ~/.config/systemd/user
cp systemd/printq.service ~/.config/systemd/user/     # adjust the path if the clone is not in ~/printq
systemctl --user daemon-reload
systemctl --user enable --now printq
journalctl --user -u printq -f
```

A user service stops when you log out unless lingering is on: `loginctl enable-linger $USER`.

## Commissioning a printer

Do this once per printer, at the machine, before giving it `veto` or `auto`.

1. Leave `start_mode` at `confirm`.
2. On a `raise_bed` printer, make the two reference frames first (see [bed-check.md](bed-check.md#bed-height-by-camera)): one with the toolhead parked and the bed low, one with the empty bed at inspection height. Then, with the bed clear and the printer in its normal after-print state, run `printq bedcheck <printer>` and watch. The bed should come up in steps and stop just short of the height of the clear reference, with verdict `clear`. If it stops early with `cannot_tell`, read the reason: usually `park_xy` does not match where your end G-code leaves the head, or a reference frame is missing or out of date.
3. Put a finished part on the bed and run it again. The verdict must be `occupied`. Try a tall part and a thin flat one.
4. Submit a real job and let it reach `awaiting_go`. Check the photo you are sent, send `/go`, and watch the start.
5. When the verdicts have matched what you saw for a while, switch to `veto`. The dataset index (`$PRINTQ_DATA/dataset/index.jsonl`) records every verdict if you want to count.

## Troubleshooting

| Symptom | Look at |
|---|---|
| A job sits in `submitted` | `journalctl --user -u printq`; is Ollama up and the agent model pulled? `printq doctor` |
| "Needs you: the prepare step stopped..." | `jobs/<id>/agent-prepare-*.json` holds the agent's whole run; `jobs/<id>/slice/orca.log` the slicer's |
| Slicing fails on a setting mismatch | The message names the setting, the wanted value and the value used. Usually an override the preset set expresses differently (`"20%"` vs `"20"`) |
| Verdict is always `cannot_tell` | `printq bedcheck <printer>` prints the reason. No API key and a dark chamber are the common ones |
| A job stays `queued` on an idle printer | Is it held? Is an earlier job on that printer `awaiting_go` or `needs_human`? `printq queue` |

`jobs/<id>/events.log` is the timeline of any job.
