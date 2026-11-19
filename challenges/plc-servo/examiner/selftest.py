"""Run the public protocol in a disposable copy, then grade its frozen submissions."""
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def invoke(script, *args, stdin=b"", cwd):
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-X", "utf8", str(script), *args],
        input=stdin, capture_output=True, cwd=cwd, timeout=30, check=True,
    )
    return result.stdout.decode("utf-8")


def exchange(root, actions):
    lines = [json.dumps({"tool": tool, "arguments": args}) for tool, args in actions]
    data = ("\n".join(lines) + "\n").encode("utf-8")
    output = invoke(root / "examiner/blind_exam.py", stdin=data, cwd=root)
    opening, *replies = map(json.loads, output.splitlines())
    assert len(opening["messages"]) == 2 and len(opening["tools"]) == 6
    assert len(replies) == len(actions)
    return replies


def submit_and_grade(root, config, program):
    actions = [
        ("write_file", {"path": "work/station.json", "text": json.dumps(config)}),
        ("write_file", {"path": "work/main.st", "text": program}),
        ("station", {"action": "compile"}),
        ("station", {"action": "download"}),
        ("station", {"action": "checkpoint"}),
        ("submit", {"note": "public release self-test"}),
    ]
    replies = exchange(root, actions)
    assert all(reply["ok"] for reply in replies)
    receipt = replies[-1]["result"]["receipt"]
    report = json.loads(invoke(root / "examiner/blind_grade.py", receipt, "--seed", "31415", cwd=root))
    return report, replies[-2]["result"]["passed"]


def main():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "station"
        for directory in ("candidate", "examiner"):
            shutil.copytree(ROOT / directory, root / directory, ignore=shutil.ignore_patterns("__pycache__"))
        replies = exchange(root, [
            ("list_files", {}),
            ("read_file", {"path": "examiner/reference_modes.st"}),
            ("read_file", {"path": "../README.md"}),
            ("station", {"action": "observe"}),
        ])
        assert "usb/drive.xml" in replies[0]["result"]["files"]
        assert not replies[1]["ok"] and not replies[2]["ok"]
        assert replies[3]["result"]["panel"]["al_state"] != 8

        config = json.loads((root / "candidate/station.json").read_bytes())
        config["station"]["address"] = 0
        config["axis"]["feedback"] = "0x6064:0"
        reference = (root / "examiner/reference_modes.st").read_text(encoding="utf-8")
        full, checkpoint = submit_and_grade(root, config, reference)
        assert checkpoint and full["checkpoint_on_record"] and full["passed"]
        assert set(full["stages"]) == {"jog", "position", "speed", "torque"}

        jog_only = "D103 := D11; D102 := D102 + 1; M100 := D0 <> 0 AND D1 = 0; D101 := D0;"
        partial, checkpoint = submit_and_grade(root, config, jog_only)
        passed = [stage for stage, result in partial["stages"].items() if result["passed"]]
        assert checkpoint and not partial["passed"] and passed == ["jog"]

        config["axis"]["counts_per_rev"] *= 2
        scale_error, checkpoint = submit_and_grade(root, config, reference)
        assert not checkpoint and not scale_error["passed"]
        print("PASS: fresh opening, material boundary, frozen submission and independent grading")
        print("PASS: jog checkpoint and position, speed, torque on one download")
        print("PASS: jog-only and incorrect encoder-scale submissions rejected")


if __name__ == "__main__":
    main()
