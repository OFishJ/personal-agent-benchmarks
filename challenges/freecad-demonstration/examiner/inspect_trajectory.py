"""Check the frame sequence produced by the supplied FreeCAD station."""
import argparse
import json
import struct
from collections import Counter
from pathlib import Path, PureWindowsPath


def inspect(record):
    trace = json.loads((record / "trajectory_000.json").read_text(encoding="utf-8-sig"))
    actions = trace["ground_truth_actions"]
    steps = len(actions)
    errors = []
    if trace["total_steps"] != steps or not steps:
        errors.append("empty trajectory or inconsistent total_steps")
    if [a.get("step") for a in actions] != list(range(1, steps + 1)):
        errors.append("step indices are not contiguous")
    frame_names = [f"step_{i:03d}.png" for i in range(steps + 1)]
    paths = [action.get("image_path") for action in actions] + [trace.get("final_state_image")]
    if [PureWindowsPath(str(item)).name for item in paths] != frame_names:
        errors.append("action or final frame references differ from the expected sequence")
    images = record / "raw_images"
    present = {entry.name for entry in images.glob("*.png")}
    if set(frame_names) != present:
        errors.append("missing or unexpected PNG file")
    sizes = set()
    for name in frame_names:
        if name not in present:
            continue
        with (images / name).open("rb") as source:
            header = source.read(24)
        if (len(header) != 24 or header[:8] != bytes.fromhex("89504e470d0a1a0a")
                or header[12:16] != b"IHDR"):
            errors.append(f"{name} has an invalid PNG header")
            continue
        sizes.add(struct.unpack(">II", header[16:24]))
    if len(sizes) != 1 or any(0 in size for size in sizes):
        errors.append("frames lack a consistent positive size")
    return {"steps": steps, "frame_count": len(present), "frame_sizes": sorted(sizes),
            "actions": dict(Counter(a.get("action_type") for a in actions)),
            "sequence_errors": errors}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("record", type=Path, help="directory containing trajectory_000.json and raw_images")
    result = inspect(parser.parse_args().record)
    print(json.dumps(result, indent=2))
    raise SystemExit(1 if result["sequence_errors"] else 0)
