import re
import shutil
import sys
from collections import defaultdict
from datetime import datetime, date, timedelta
from pathlib import Path

import yaml
from nomad_upload import yn_prompt, setup_auth, upload_folder

_CONFIG_PATH = Path(__file__).parent / "config.yml"

# ── Timestamp formats ──────────────────────────────────────────────────────
_TS_FORMATS = (
    "%d/%m/%Y %H:%M:%S.%f",
    "%d/%m/%Y %H:%M:%S",
)

# ── Dated-folder name patterns  (group names: y, m, d) ────────────────────
_DATE_FOLDER_PATTERNS = [
    re.compile(r"^(?P<y>\d{4})[-_](?P<m>\d{2})[-_](?P<d>\d{2})$"),   # 2026-03-13 or 2026_03_13
    re.compile(r"^(?P<d>\d{2})[-_](?P<m>\d{2})[-_](?P<y>\d{4})$"),   # 13-03-2026
]

# ── GC move patterns ───────────────────────────────────────────────────────
RE_ENTER_GC = re.compile(
    r"^(?P<sample>.+?)@\S+\s+moved\s+from\s+.+?\s+to\s+GC\s*$",
    re.IGNORECASE,
)
RE_EXIT_GC = re.compile(
    r"^(?P<sample>.+?)@\S+\s+moved\s+from\s+GC\s+to\s+.+?\s*$",
    re.IGNORECASE,
)

# ── Non-numeric / annotation files: never inject artificial rows ───────────
_SKIP_INJECT_NAMES = {
    "messages.txt",
    "shutters.txt",
    "fitting.txt",
}


# ═══════════════════════════════════════════════════════════════════════════
# LOW-LEVEL HELPERS
# ═══════════════════════════════════════════════════════════════════════════

def decode_line(raw: bytes) -> str:
    """Decode bytes → str, strip BOM, strip trailing CR/LF."""
    return raw.decode("utf-8-sig", errors="replace").rstrip("\r\n")


def parse_timestamp(ts_str: str) -> datetime | None:
    """Try all known EPIC timestamp formats. Return None on failure."""
    ts_str = ts_str.strip()
    for fmt in _TS_FORMATS:
        try:
            return datetime.strptime(ts_str, fmt)
        except ValueError:
            pass
    return None


def is_header_line(line: str) -> bool:
    """
    True when the line is a header/comment, not a data row.
    Rule: if the first comma-separated field does NOT parse as a timestamp
    the line is a header.  Works for both ' -prefixed and plain headers.
    """
    stripped = line.strip()
    if not stripped:
        return False
    return parse_timestamp(stripped.split(",", 1)[0]) is None


def is_timestamped_file(file_path: Path) -> bool:
    """
    True when the file contains at least one row whose first field is a
    valid EPIC timestamp.  Handles both header styles and BOM.
    """
    if file_path.suffix.lower() not in {".txt", ".csv", ".log", ".dat"}:
        return False
    try:
        with open(file_path, "rb") as fh:
            for raw in fh:
                line = decode_line(raw).strip()
                if not line:
                    continue
                if parse_timestamp(line.split(",", 1)[0]) is not None:
                    return True
    except OSError:
        pass
    return False


def try_read_file(file_path: Path) -> bool:
    """Return True if the file can be opened for reading (not locked)."""
    try:
        with open(file_path, "rb") as fh:
            fh.read(1)
        return True
    except OSError:
        return False


def format_ts(ts: datetime) -> str:
    """Format a datetime to the EPIC log timestamp string (with ms)."""
    return ts.strftime("%d/%m/%Y %H:%M:%S.") + f"{ts.microsecond // 1000:03d}"


# ═══════════════════════════════════════════════════════════════════════════
# DATED SIBLING FOLDER DISCOVERY
# ═══════════════════════════════════════════════════════════════════════════

def parse_folder_date(folder: Path) -> date | None:
    """Try to parse a calendar date from a folder name. None if not parseable."""
    name = folder.name
    for pat in _DATE_FOLDER_PATTERNS:
        m = pat.match(name)
        if m:
            try:
                return date(int(m.group("y")), int(m.group("m")), int(m.group("d")))
            except ValueError:
                pass
    return None


def find_dated_siblings(base_dir: Path) -> dict[date, Path]:
    """
    Scan the parent of base_dir for dated sibling folders.
    Returns {date: folder_path} for every parseable sibling (including
    base_dir itself if it has a parseable name).
    """
    siblings: dict[date, Path] = {}
    parent = base_dir.parent
    try:
        for child in parent.iterdir():
            if child.is_dir():
                d = parse_folder_date(child)
                if d is not None:
                    siblings[d] = child
    except OSError:
        pass
    return siblings


def folders_for_span(
    start: datetime,
    end: datetime,
    base_dir: Path,
    dated_siblings: dict[date, Path],
) -> list[Path]:
    """
    Return an ordered list of folders that cover the date range
    [start.date … end.date].  Falls back to [base_dir] when no dated
    siblings are found.
    """
    if not dated_siblings:
        return [base_dir]

    needed_dates = []
    cur = start.date()
    while cur <= end.date():
        needed_dates.append(cur)
        cur += timedelta(days=1)

    result = []
    for d in needed_dates:
        if d in dated_siblings:
            result.append(dated_siblings[d])
        else:
            # Date needed but folder absent – warn at call site
            pass

    # Deduplicate while preserving order
    seen = set()
    unique = []
    for p in result:
        if p not in seen:
            seen.add(p)
            unique.append(p)

    return unique if unique else [base_dir]


# ═══════════════════════════════════════════════════════════════════════════
# MESSAGES.TXT PARSING
# ═══════════════════════════════════════════════════════════════════════════

def parse_messages(messages_file: Path) -> list[dict]:
    """
    Scan Messages.txt for GC entry/exit events.
    Format: Date&Time,CallerID,Message,Color
    Matching is on Message content only (not CallerID).
    """
    events = []
    try:
        with open(messages_file, "rb") as fh:
            for raw in fh:
                line = decode_line(raw)
                if not line.strip() or is_header_line(line):
                    continue
                parts = line.split(",", 3)
                if len(parts) < 3:
                    continue
                ts = parse_timestamp(parts[0])
                if ts is None:
                    continue
                message = parts[2].strip()
                m = RE_ENTER_GC.match(message)
                if m:
                    events.append({
                        "sample":    m.group("sample").strip().lower(),
                        "direction": "enter",
                        "timestamp": ts,
                    })
                    continue
                m = RE_EXIT_GC.match(message)
                if m:
                    events.append({
                        "sample":    m.group("sample").strip().lower(),
                        "direction": "exit",
                        "timestamp": ts,
                    })
    except OSError as exc:
        print(f"  ERROR reading Messages.txt: {exc}")
    return events


# ═══════════════════════════════════════════════════════════════════════════
# VISIT PAIRING
# ═══════════════════════════════════════════════════════════════════════════

def build_sample_visits(events: list[dict]) -> tuple[list[dict], list[str]]:
    """
    Pair GC entries and exits using global chronological tracking.

    EPIC never logs an explicit exit when a new sample displaces the one
    currently in GC.  This tracks the single GC occupant across all samples
    so that an entry while another sample is in GC implicitly closes the
    previous occupant's visit.

    Returns (visits, warnings).
    """
    visits: list[dict] = []
    warnings: list[str] = []

    # Per-sample visit counter for unique folder naming
    visit_counter: dict[str, int] = defaultdict(int)

    # Current GC occupant: {"sample": ..., "start": ...} or None
    occupant = None

    for ev in events:
        sample = ev["sample"]
        ts = ev["timestamp"]

        if ev["direction"] == "enter":
            if occupant is not None and occupant["sample"] != sample:
                # Implicit swap: close previous occupant's visit
                visit_counter[occupant["sample"]] += 1
                vn = visit_counter[occupant["sample"]]
                folder = (
                    occupant["sample"]
                    if vn == 1
                    else f"{occupant['sample']}_{vn}"
                )
                visits.append({
                    "sample":      occupant["sample"],
                    "folder_name": folder,
                    "start":       occupant["start"],
                    "end":         ts,
                })
                print(
                    f"    Implicit swap: {occupant['sample']} exited GC at "
                    f"{format_ts(ts)} (displaced by {sample})"
                )

            # Start visit for the new entrant
            occupant = {"sample": sample, "start": ts}

        elif ev["direction"] == "exit":
            if occupant is None:
                warnings.append(
                    f"WARNING: '{sample}' has a GC exit at "
                    f"{format_ts(ts)} with no preceding entry — skipped."
                )
                continue
            if occupant["sample"] != sample:
                warnings.append(
                    f"WARNING: '{sample}' exited GC at {format_ts(ts)} but "
                    f"'{occupant['sample']}' is the current occupant — "
                    f"treating as exit for '{sample}'."
                )
            visit_counter[sample] += 1
            vn = visit_counter[sample]
            folder = sample if vn == 1 else f"{sample}_{vn}"
            visits.append({
                "sample":      sample,
                "folder_name": folder,
                "start":       occupant["start"],
                "end":         ts,
            })
            occupant = None

    # End-of-log: sample still in GC
    if occupant is not None:
        warnings.append(
            f"WARNING: '{occupant['sample']}' entered GC at "
            f"{format_ts(occupant['start'])} but was never displaced or "
            f"exited by the end of the log — skipped."
        )

    visits.sort(key=lambda v: v["start"])
    return visits, warnings


# ═══════════════════════════════════════════════════════════════════════════
# OUTPUT FOLDER NAME RESOLUTION
# ═══════════════════════════════════════════════════════════════════════════

def resolve_output_folder(output_base: Path, folder_name: str) -> Path:
    """
    Return a free path for the output folder.
    Appends _filtered, _filtered_2, … if the name is already taken.
    """
    candidate = output_base / folder_name
    if not candidate.exists():
        return candidate
    candidate = output_base / f"{folder_name}_filtered"
    if not candidate.exists():
        return candidate
    n = 2
    while True:
        candidate = output_base / f"{folder_name}_filtered_{n}"
        if not candidate.exists():
            return candidate
        n += 1


# ═══════════════════════════════════════════════════════════════════════════
# READING RAW DATA ROWS FROM ONE OR MORE SOURCE FOLDERS
# ═══════════════════════════════════════════════════════════════════════════

def read_all_rows(file_path: Path) -> list[tuple[datetime, bytes]]:
    """
    Read every timestamped data row from a single file.
    Returns [(timestamp, raw_bytes), …] sorted by timestamp.
    Header/blank lines are discarded.
    """
    rows: list[tuple[datetime, bytes]] = []
    try:
        with open(file_path, "rb") as fh:
            for raw in fh:
                line = decode_line(raw).strip()
                if not line:
                    continue
                ts = parse_timestamp(line.split(",", 1)[0])
                if ts is not None:
                    rows.append((ts, raw))
    except OSError:
        pass
    rows.sort(key=lambda r: r[0])
    return rows


def read_header_bytes(file_path: Path) -> list[bytes]:
    """
    Read and return all header/comment lines from the start of a file
    as raw bytes (preserving exact encoding and line endings).
    """
    headers: list[bytes] = []
    try:
        with open(file_path, "rb") as fh:
            for raw in fh:
                line = decode_line(raw).strip()
                if not line:
                    headers.append(raw)   # preserve blank lines in header
                    continue
                if is_header_line(line):
                    headers.append(raw)
                else:
                    break                 # first data row reached
    except OSError:
        pass
    return headers


def collect_rows_across_folders(
    folders: list[Path],
    filename: str,
) -> tuple[list[bytes], list[tuple[datetime, bytes]]]:
    """
    Gather header lines and all data rows for `filename` across multiple
    source folders (for cross-day merging).

    Returns:
        header_bytes  – raw header lines from the FIRST folder that has
                        the file (used once in the output).
        all_rows      – chronologically sorted [(ts, raw_bytes), …] from
                        ALL folders combined, deduplicated by timestamp.
    """
    header_bytes: list[bytes] = []
    seen_ts: set[datetime] = set()
    all_rows: list[tuple[datetime, bytes]] = []

    for folder in folders:
        src = folder / filename
        if not src.is_file():
            continue
        if not header_bytes:
            header_bytes = read_header_bytes(src)
        for ts, raw in read_all_rows(src):
            if ts not in seen_ts:
                seen_ts.add(ts)
                all_rows.append((ts, raw))

    all_rows.sort(key=lambda r: r[0])
    return header_bytes, all_rows


# ═══════════════════════════════════════════════════════════════════════════
# ARTIFICIAL BOUNDARY ROW LOGIC
# ═══════════════════════════════════════════════════════════════════════════

def values_from_row(raw: bytes) -> bytes | None:
    """
    Extract everything after the first comma in a data row as raw bytes.
    Returns None if the row has no comma.
    """
    decoded = decode_line(raw)
    idx = decoded.find(",")
    if idx == -1:
        return None
    # Preserve original line ending from raw
    ending = b"\r\n" if raw.endswith(b"\r\n") else b"\n"
    return decoded[idx:].encode("utf-8") + ending


def is_numeric_values(raw: bytes) -> bool:
    """
    Return True if every value field (after the timestamp) in a data row
    contains only numeric data.  Used to auto-detect injection eligibility.
    """
    decoded = decode_line(raw).strip()
    parts = decoded.split(",")
    if len(parts) < 2:
        return False
    for val in parts[1:]:
        val = val.strip()
        if not val:
            continue
        try:
            float(val)
        except ValueError:
            return False
    return True


def should_inject(filename: str, sample_rows: list[tuple[datetime, bytes]]) -> bool:
    """
    Decide whether artificial boundary rows should be injected into this file.

    Rules (both must pass):
      1. Filename is NOT in the hard skip-list.
      2. Auto-detect: at least one in-window data row has purely numeric
         value columns.
    """
    if filename.lower() in _SKIP_INJECT_NAMES:
        return False
    for _, raw in sample_rows:
        if not is_numeric_values(raw):
            return False
    return len(sample_rows) > 0


def make_artificial_row(ts: datetime, value_bytes: bytes) -> bytes:
    """
    Build one artificial row: timestamp_string + value_suffix_bytes.
    Preserves the original line ending from value_bytes.
    """
    ts_str = format_ts(ts).encode("utf-8")
    # value_bytes already starts with "," and ends with line terminator
    return ts_str + value_bytes


# ═══════════════════════════════════════════════════════════════════════════
# CORE FILE PROCESSING
# ═══════════════════════════════════════════════════════════════════════════

def process_timestamped_file(
    filename: str,
    folders: list[Path],
    dest: Path,
    start: datetime,
    end: datetime,
) -> tuple[int, int]:
    """
    Collect rows from one or more source folders, filter to [start, end],
    optionally inject artificial boundary rows, and write the result.

    Returns (data_rows_written, artificial_rows_injected).
    """
    header_bytes, all_rows = collect_rows_across_folders(folders, filename)

    if not header_bytes and not all_rows:
        return 0, 0

    # ── Split rows into: before-window, in-window, after-window ──────────
    before: list[tuple[datetime, bytes]] = []
    window: list[tuple[datetime, bytes]] = []

    for ts, raw in all_rows:
        if ts < start:
            before.append((ts, raw))
        elif ts <= end:
            window.append((ts, raw))
        # rows after end are discarded

    # ── Determine artificial row eligibility ─────────────────────────────
    inject = should_inject(filename, window)

    # No in-window rows → write header only, no artificial rows
    if not window and not inject:
        dest.parent.mkdir(parents=True, exist_ok=True)
        with open(dest, "wb") as fh:
            for hb in header_bytes:
                fh.write(hb)
        return 0, 0

    art_start_raw: bytes | None = None
    art_end_raw:   bytes | None = None

    if inject:
        # Artificial start row: values from last row BEFORE start time
        if before:
            val = values_from_row(before[-1][1])
            if val is not None:
                art_start_raw = make_artificial_row(start, val)

        # Artificial end row: values from last in-window row
        if window:
            val = values_from_row(window[-1][1])
            if val is not None:
                art_end_raw = make_artificial_row(end, val)

    # ── Write output file ─────────────────────────────────────────────────
    dest.parent.mkdir(parents=True, exist_ok=True)
    art_count = 0

    with open(dest, "wb") as fh:
        for hb in header_bytes:
            fh.write(hb)

        if art_start_raw is not None:
            fh.write(art_start_raw)
            art_count += 1

        for _, raw in window:
            fh.write(raw)

        if art_end_raw is not None:
            fh.write(art_end_raw)
            art_count += 1

    return len(window), art_count


# ═══════════════════════════════════════════════════════════════════════════
# PER-VISIT ORCHESTRATION
# ═══════════════════════════════════════════════════════════════════════════

def process_visit(
    visit: dict,
    base_dir: Path,
    dated_siblings: dict[date, Path],
    output_base: Path,
) -> Path:
    """
    Create the output folder for one sample visit and populate it.
    EPIC log files go into an EPIC_logs/ subdirectory.
    Returns the growth-run folder path.
    """
    dest_dir = resolve_output_folder(output_base, visit["folder_name"])
    epic_dir = dest_dir / "EPIC_logs"
    epic_dir.mkdir(parents=True, exist_ok=True)

    start: datetime = visit["start"]
    end:   datetime = visit["end"]

    print(
        f"\n  → {dest_dir.name}"
        f"  ({start.strftime('%d/%m/%Y %H:%M:%S')} – {end.strftime('%d/%m/%Y %H:%M:%S')})"
    )

    # Determine which source folders cover the date span
    source_folders = folders_for_span(start, end, base_dir, dated_siblings)

    if len(source_folders) > 1:
        print(f"     Cross-day span — merging from {len(source_folders)} folder(s):")
        for f in source_folders:
            print(f"       {f}")
    else:
        missing = []
        cur = start.date()
        while cur <= end.date():
            if dated_siblings and cur not in dated_siblings:
                missing.append(str(cur))
            cur += timedelta(days=1)
        if missing:
            print(f"     WARNING: no dated folder found for: {', '.join(missing)}"
                  f" — using available data only.")

    # Collect the union of all filenames across all source folders
    all_filenames: set[str] = set()
    for folder in source_folders:
        try:
            for f in folder.iterdir():
                if f.is_file():
                    all_filenames.add(f.name)
        except OSError:
            pass

    for filename in sorted(all_filenames):
        # Skip Messages.txt — it's used for GC event detection only, not data output
        if filename.lower() == "messages.txt":
            continue

        dest_file = epic_dir / filename
        label = f"     {filename:45s}"

        # Find an accessible copy to check readability and type
        src_example = next(
            (folder / filename for folder in source_folders
             if (folder / filename).is_file()),
            None,
        )
        if src_example is None:
            continue

        if not try_read_file(src_example):
            print(f"{label}  SKIPPED — file is open in another program; "
                  f"close it and re-run to include it")
            continue

        try:
            if is_timestamped_file(src_example):
                data_rows, art_rows = process_timestamped_file(
                    filename, source_folders, dest_file, start, end
                )
                art_note = f" + {art_rows} artificial" if art_rows else ""
                print(f"{label}  filtered   ({data_rows} rows{art_note})")
            else:
                # Non-timestamped: copy the first available source as-is
                shutil.copy2(src_example, dest_file)
                print(f"{label}  copied as-is (no timestamp data)")

        except PermissionError:
            print(f"{label}  SKIPPED — file is open in another program; "
                  f"close it and re-run to include it")
        except OSError as exc:
            print(f"{label}  SKIPPED — OS error: {exc}")

    return dest_dir


# ═══════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════

def main() -> None:
    # Load config
    cfg = {}
    if _CONFIG_PATH.exists():
        try:
            with open(_CONFIG_PATH, "rb") as fh:
                cfg = yaml.safe_load(fh) or {}
        except Exception:
            pass

    # ── Resolve auxiliary folder ──────────────────────────────────────────
    # Config has a single folder path; we copy ALL files from it.
    aux_folder: Path | None = None
    aux_cfg = cfg.get("auxiliary_files", [])
    if aux_cfg:
        p = Path(aux_cfg[0])
        if not p.is_absolute():
            p = (_CONFIG_PATH.parent / p).resolve()
        if p.is_dir():
            aux_folder = p
        elif p.is_file():
            # Backward compat: if a file was given, use its parent folder
            aux_folder = p.parent

    # ── Resolve log folder path ─────────────────────────────────────────
    if len(sys.argv) >= 2:
        base_dir = Path(sys.argv[1]).resolve()
    else:
        cfg_path = cfg.get("log_path", "")
        if cfg_path:
            p = Path(cfg_path)
            if not p.is_absolute():
                p = (_CONFIG_PATH.parent / p).resolve()
            if p.exists() and p.is_dir():
                print(f"  Config log_path: {p}")
                if yn_prompt("  Use this folder?", "y"):
                    base_dir = p
                else:
                    base_dir = Path(input("  Enter log folder path: ").strip()).resolve()
            else:
                print(f"  Config log_path '{p}' does not exist.")
                base_dir = Path(input("  Enter log folder path: ").strip()).resolve()
        else:
            base_dir = Path(input("  Enter log folder path: ").strip()).resolve()

    if not base_dir.exists():
        print(f"ERROR: Path does not exist: {base_dir}")
        sys.exit(1)
    if not base_dir.is_dir():
        print(f"ERROR: Path is not a folder: {base_dir}")
        sys.exit(1)

    print("=" * 60)
    print("EPIC Log Extractor  v3.0")
    print("=" * 60)
    print(f"Log folder : {base_dir}")

    output_base_cfg = cfg.get("output", {}).get("base_path")
    if output_base_cfg:
        p = Path(output_base_cfg)
        if not p.is_absolute():
            p = (_CONFIG_PATH.parent / p).resolve()
        output_base = p
    else:
        output_base = base_dir.parent
    print(f"Output in  : {output_base}")

    # ── Discover dated sibling folders (for cross-day data collection) ───
    dated_siblings = find_dated_siblings(base_dir)

    # ── Parse Messages.txt from the TARGET folder ONLY ───────────────────
    print("\nParsing Messages.txt ...")
    mf = base_dir / "Messages.txt"
    if not mf.is_file():
        print(f"\nERROR: Messages.txt not found in: {base_dir}")
        sys.exit(1)

    messages_sources = [mf]
    print(f"  Using Messages.txt from: {base_dir}")

    all_events: list[dict] = []
    seen_events: set[tuple] = set()
    for msg_file in messages_sources:
        for ev in parse_messages(msg_file):
            key = (ev["sample"], ev["direction"], ev["timestamp"])
            if key not in seen_events:
                seen_events.add(key)
                all_events.append(ev)
    all_events.sort(key=lambda e: e["timestamp"])
    events = all_events
    print(f"  {len(events)} GC move event(s) found.")

    # ── Show auxiliary folder info ────────────────────────────────────────
    if aux_folder:
        aux_files = [f for f in aux_folder.iterdir() if f.is_file()]
        print(f"\nAuxiliary folder: {aux_folder}")
        print(f"  {len(aux_files)} file(s) will be copied to each output folder")
    else:
        print(f"\nNo auxiliary folder configured")

    # ── Build visits ─────────────────────────────────────────────────────
    visits, warnings = build_sample_visits(events)
    for w in warnings:
        print(f"\n  {w}")

    if not visits:
        print("\nNo complete GC visits found. Nothing to do.")
        sys.exit(0)

    print(f"\n  {len(visits)} complete GC visit(s):")
    for v in visits:
        span_days = (v["end"].date() - v["start"].date()).days
        cross = f"  [spans {span_days + 1} days]" if span_days > 0 else ""
        print(
            f"    {v['folder_name']:30s}  "
            f"{v['start'].strftime('%d/%m/%Y %H:%M:%S')}  →  "
            f"{v['end'].strftime('%d/%m/%Y %H:%M:%S')}{cross}"
        )

    # ── Process each visit ───────────────────────────────────────────────
    print("\nProcessing ...")
    built_folders = []
    for visit in visits:
        result = process_visit(visit, base_dir, dated_siblings, output_base)
        # Copy ALL auxiliary files from the configured folder into EPIC_logs/
        if aux_folder and aux_folder.is_dir():
            for src in aux_folder.iterdir():
                if src.is_file():
                    try:
                        shutil.copy2(src, result / "EPIC_logs" / src.name)
                    except Exception:
                        pass
        built_folders.append(result)

    print("\n" + "=" * 60)
    print(f"Done.  {len(visits)} folder(s) created in: {output_base}")
    print("=" * 60)

    # Optional NOMAD upload
    if not built_folders:
        return

    try:
        want_upload = yn_prompt("\nUpload any folder to NOMAD?", "n")
    except (EOFError, KeyboardInterrupt):
        return
    if not want_upload:
        return

    base_url, upload_id, token = setup_auth()
    if not token:
        print("  No valid credentials — skipping upload.")
        return

    for folder in built_folders:
        try:
            if yn_prompt(f"\n  Upload {folder.name}?", "y"):
                upload_folder(folder, base_url, upload_id, token)
        except (EOFError, KeyboardInterrupt):
            print("\n  Skipping remaining uploads.")
            break


if __name__ == "__main__":
    main()
