# EPIC Log Extractor

Extracts EPIC log data for samples while they are inside the GC position.
Detects GC entry/exit events from `Messages.txt`, builds time-filtered
output folders per sample/growth run, and optionally uploads to NOMAD.

## Quick start

```bash
# With a path argument — processes immediately
python log_extractor.py "C:\Users\sina\Desktop\2026-03-13"

# Without arguments — reads log_path from config.yml (or asks for one)
python log_extractor.py
```

## How it works

1. Parses `Messages.txt` for GC move events across dated sibling folders.
2. Pairs entry/exit events into visits, handling **implicit swaps** (when a
   new sample displaces the current GC occupant without a logged exit).
3. For each visit, creates an output folder with time-filtered EPIC log files
   under `EPIC_logs/`, plus any configured auxiliary files.
4. Optionally zips and uploads each folder to a NOMAD Oasis.

## Output structure

```
<growth_run_id>/
    EPIC_logs/          ← filtered EPIC log files
    (auxiliary files)   ← copied from config.yml
```

## Configuration

All settings live in `config.yml` (next to the script):

| Setting | Description |
|---------|-------------|
| `log_path` | Default log folder when no path is given on the command line. Leave empty to be prompted. |
| `output.base_path` | Where to write growth-run folders. Leave empty for default (log folder's parent). |
| `auxiliary_files` | List of file paths to copy into each growth-run folder. |
| `nomad.servers` | Per-user NOMAD server URLs and upload IDs (saved across sessions). |

## NOMAD upload

After processing, the script asks whether to upload to NOMAD. If yes:

1. Prompts for username once.
2. Uses saved server URL from config (or asks for a new one).
3. Prompts for password once — reused for all subsequent folder uploads.
4. Uses saved upload ID from config (or asks for one).
5. Zips each folder and uploads it.

## GC swap detection

EPIC never logs an explicit exit when a new sample displaces the one
currently in GC. The script tracks a single global GC occupant
chronologically. When a new entry arrives while another sample is in GC,
the previous occupant's visit is closed at the new entry's timestamp
with a terminal message:

```
Implicit swap: m84317 exited GC at 13/03/2026 13:08:21.029 (displaced by m84313)
```

Warnings are shown for:
- Exits with no current GC occupant
- Samples still in GC at the end of the log (never displaced or exited)

## Features

- Cross-day extraction (merges from adjacent dated folders)
- Automatic dated-folder discovery
- Implicit GC swap detection
- Artificial boundary rows for numeric logs
- Non-timestamped files copied unchanged
- Locked-file handling
- Output folder collision handling (`_filtered`, `_filtered_2`, ...)
- NOMAD upload with single-login for multiple folders

## Requirements

- Python 3.10+
- `pyyaml` (`pip install pyyaml`)
- `requests` (`pip install requests`) — only needed for NOMAD upload
