import os
import shutil
import subprocess
import sys
from pathlib import Path

TEST_ENV = Path(__file__).parent / "mock_lab_environment"


def create_mock_environment():
    """Generates a multi-day lab folder layout to test cross-day boundary conditions."""
    if TEST_ENV.exists():
        shutil.rmtree(TEST_ENV)

    # 1. Setup two consecutive dated folders for an overnight run
    day1_dir = TEST_ENV / "2026-03-13"
    day2_dir = TEST_ENV / "2026-03-14"
    day1_dir.mkdir(parents=True)
    day2_dir.mkdir(parents=True)

    # 2. Write Messages.txt across both days tracking an overnight sample run
    # Sample "m8test_overnight" enters GC late March 13 and leaves early March 14
    msg_day1 = (
        "'EPIC Message Log File\n"
        "'Date&Time,CallerID,Message,Color\n"
        "13/03/2026 12:00:00.000,System,Idle state,-16777216\n"
        "13/03/2026 22:30:00.000,Location Tab,m8test_overnight@Holder1 moved from LoadLock to GC,-16777216\n"
    )
    msg_day2 = (
        "'EPIC Message Log File\n"
        "'Date&Time,CallerID,Message,Color\n"
        "14/03/2026 02:15:00.000,Location Tab,m8test_overnight@Holder1 moved from GC to Chamber2,-16777216\n"
        "14/03/2026 05:00:00.000,System,Shutting down,-16777216\n"
    )
    with open(day1_dir / "Messages.txt", "w", encoding="utf-8") as f: f.write(msg_day1)
    with open(day2_dir / "Messages.txt", "w", encoding="utf-8") as f: f.write(msg_day2)

    # 3. Write Sensor data spanning across both files (Alu.PID.MV.txt)
    alu_day1 = (
        "'EPIC Alu Log\n"
        "Date,Alu.PID.MV\n"
        "13/03/2026 20:00:00.000,800.0\n"  # Look-back baseline value before start
        "13/03/2026 23:00:00.000,802.5\n"  # Naturally inside window
    )
    alu_day2 = (
        "'EPIC Alu Log\n"
        "Date,Alu.PID.MV\n"
        "14/03/2026 01:00:00.000,805.0\n"  # Naturally inside window (Becomes last known row for end padding)
        "14/03/2026 04:00:00.000,750.0\n"  # Outside window
    )
    with open(day1_dir / "Alu.PID.MV.txt", "w", encoding="utf-8") as f: f.write(alu_day1)
    with open(day2_dir / "Alu.PID.MV.txt", "w", encoding="utf-8") as f: f.write(alu_day2)

    # 4. Write a non-numeric file (Shutters.txt) which should NOT receive artificial padding rows
    shutters_day1 = (
        "'EPIC Shutters log\n"
        "Date,ShutterState\n"
        "13/03/2026 22:45:00.000,Alu_Opened\n"
    )
    with open(day1_dir / "Shutters.txt", "w", encoding="utf-8") as f: f.write(shutters_day1)

    # 5. Write a completely un-timestamped static file (MBE8_config.xlsx)
    with open(day1_dir / "MBE8_config_M84266.xlsx", "w", encoding="utf-8") as f:
        f.write("HARDWARE_SETTINGS: CELL_A=1, CELL_B=2")

    return day1_dir


def verify_outputs():
    print("🧐 Beginning verification assertions on generated outputs...")
    
    output_target_folder = TEST_ENV / "m8test_overnight"
    assert output_target_folder.is_dir(), "❌ Test Failure: Output folder for overnight sample run not created."

    # Test 1: Check Config file copy integrity
    config_file = output_target_folder / "MBE8_config_M84266.xlsx"
    assert config_file.is_file(), "❌ Test Failure: Un-timestamped static configuration file was dropped."
    with open(config_file, "r") as f:
        assert "HARDWARE_SETTINGS" in f.read(), "❌ Test Failure: Content corruption inside static binary copy."
    print("✅ Test Passed: Non-timestamped file matched and cloned perfectly.")

    # Test 2: Check non-numeric skip-injection compliance
    shutter_file = output_target_folder / "Shutters.txt"
    assert shutter_file.is_file()
    with open(shutter_file, "r") as f:
        shutter_lines = f.readlines()
        # It should only have 3 lines total: comment, column header, and the 1 natural data row
        assert len(shutter_lines) == 3, "❌ Test Failure: Artificial rows were incorrectly forced onto non-numeric logs."
    print("✅ Test Passed: Automatic skip-injection list accurately protects non-numeric files.")

    # Test 3: Check Overnight Merge and Oliver's Boundary Padding
    alu_file = output_target_folder / "Alu.PID.MV.txt"
    assert alu_file.is_file()
    with open(alu_file, "r") as f:
        lines = [line.strip() for line in f.readlines() if line.strip()]
        
        # Expected rows after processing:
        # Header comments
        # [Artificial Start] 13/03/2026 22:30:00.000,800.0 (uses 20:00 baseline)
        # [Natural Day 1]    13/03/2026 23:00:00.000,802.5
        # [Natural Day 2]    14/03/2026 01:00:00.000,805.0
        # [Artificial End]   14/03/2026 02:15:00.000,805.0 (uses 01:00 payload)
        
        print("\n--- Processed Sensor Output (Alu.PID.MV.txt) ---")
        for line in lines:
            print(f"  {line}")
        print("-------------------------------------------------")

        assert any("13/03/2026 22:30:00.000,800.0" in l for l in lines), "❌ Test Failure: Artificial start padding mismatch."
        assert any("14/03/2026 02:15:00.000,805.0" in l for l in lines), "❌ Test Failure: Artificial end padding mismatch."
        assert not any("14/03/2026 04:00:00.000" in l for l in lines), "❌ Test Failure: Extraneous data row leaked out of bounds."

    print("✅ Test Passed: Cross-day chronological merging and boundary padding conform to Oliver's requirements.")


def main():
    print("🚀 Initializing Simulation Environment...")
    day1_folder = create_mock_environment()

    script_name = Path(__file__).parent / "log_extractor.py"
    if not script_name.is_file():
        print(f"❌ Execution aborting: Could not find code file 'log_extractor.py'")
        sys.exit(1)

    print(f"🏃 Launching main log extractor pipeline on target: {day1_folder.name}")
    process = subprocess.run(
        [sys.executable, str(script_name), str(day1_folder)],
        capture_output=True,
        text=True
    )

    print(process.stdout)
    if process.stderr:
        print("Standard Errors Logged:\n", process.stderr)

    verify_outputs()
    print("\n🎉 PIPELINE STABILITY CONFIRMED. Version 3.0 matches all rules perfectly.")

    # Clean sandbox files
    shutil.rmtree(TEST_ENV)


if __name__ == "__main__":
    main()