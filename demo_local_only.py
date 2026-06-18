"""
Quick demo: compare default mode vs --local-only mode
"""
import shutil
import subprocess
import sys
from pathlib import Path

DEMO_DIR = Path(__file__).parent / "demo_local_mode"
DAY1 = DEMO_DIR / "2026-03-13"
DAY2 = DEMO_DIR / "2026-03-14"

def setup():
    if DEMO_DIR.exists():
        shutil.rmtree(DEMO_DIR)
    DAY1.mkdir(parents=True)
    DAY2.mkdir(parents=True)
    
    # Day 1: GC entry only
    (DAY1 / "Messages.txt").write_text(
        "'EPIC Message Log\n"
        "'Date&Time,CallerID,Message,Color\n"
        "13/03/2026 22:00:00.000,Location Tab,sample_A@H1 moved from LoadLock to GC,-16777216\n"
    )
    (DAY1 / "Alu.PID.MV.txt").write_text(
        "'EPIC Alu Log\n"
        "Date,Alu.PID.MV\n"
        "13/03/2026 22:15:00.000,100.0\n"
    )
    (DAY1 / "MBE8_config.txt").write_text("BASE_PRESSURE=1e-10\n")
    
    # Day 2: GC exit only
    (DAY2 / "Messages.txt").write_text(
        "'EPIC Message Log\n"
        "'Date&Time,CallerID,Message,Color\n"
        "14/03/2026 02:00:00.000,Location Tab,sample_A@H1 moved from GC to LoadLock,-16777216\n"
    )
    (DAY2 / "Alu.PID.MV.txt").write_text(
        "'EPIC Alu Log\n"
        "Date,Alu.PID.MV\n"
        "14/03/2026 01:30:00.000,105.0\n"
    )
    (DAY2 / "MBE8_config.txt").write_text("BASE_PRESSURE=1e-10\n")

def run_mode(label, day_path, extra_args):
    print(f"\n{'='*60}")
    print(f"MODE: {label}")
    print(f"{'='*60}")
    
    script = Path(__file__).parent / "log_extractor.py"
    cmd = [sys.executable, str(script), *extra_args, str(day_path)]
    print(f"Command: {' '.join(cmd)}\n")
    
    result = subprocess.run(cmd, capture_output=True, text=True)
    print(result.stdout)
    if result.stderr:
        print("STDERR:", result.stderr)
    
    # List output folders
    output_dir = DEMO_DIR
    folders = [f for f in output_dir.iterdir() if f.is_dir() and f != DAY1 and f != DAY2]
    print(f"Output folders created: {[f.name for f in sorted(folders)]}")
    for f in sorted(folders):
        epic = f / "EPIC_logs"
        if epic.is_dir():
            contents = [p.name for p in epic.iterdir()]
            print(f"  {f.name}/EPIC_logs/  ->  {contents}")
    return folders

if __name__ == "__main__":
    print("Setting up demo environment (2 dated folders, cross-day cycle)...")
    setup()
    print(f"  Day 1: {DAY1.name} (GC entry)")
    print(f"  Day 2: {DAY2.name} (GC exit)")
    print("\nScenario: sample_A enters GC on 2026-03-13 at 22:00 and exits on 2026-03-14 at 02:00")
    
    # Scenario 1: Default mode (cross-day support)
    shutil.rmtree(DEMO_DIR / "sample_a", ignore_errors=True)
    folders_default = run_mode(
        "DEFAULT (scans ±1 day dated siblings)",
        DAY1,
        []
    )
    
    # Scenario 2: Local-only mode
    shutil.rmtree(DEMO_DIR / "sample_a", ignore_errors=True)
    folders_local = run_mode(
        "LOCAL-ONLY (ignores other dated folders)",
        DAY1,
        ["--local-only"]
    )
    
    print(f"\n{'='*60}")
    print("SUMMARY")
    print(f"{'='*60}")
    print(f"Default mode created:    {len(folders_default)} folder(s) → {[f.name for f in sorted(folders_default)]}")
    print(f"Local-only mode created: {len(folders_local)} folder(s) → {[f.name for f in sorted(folders_local)]}")
    print("\nExplanation:")
    print("  • Default: Found complete cycle (entry 13-03, exit 14-03) → created 1 folder")
    print("  • Local-only: Found entry 13-03 but no exit in same folder → no cycle → no folder")
    
    # Cleanup
    # shutil.rmtree(DEMO_DIR)
    print(f"\nDemo files left in: {DEMO_DIR}")
