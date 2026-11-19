"""Examiner-only scoring of a frozen ST submission, using the same isolated runtime."""
import argparse
import hashlib
import json
import random
import secrets
import sqlite3
import sys
import uuid
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parent))
from blind_room import Room, STORE, load_packet
from motion_trials import run_jog, run_position, run_speed, run_torque


def fetch(receipt, store=STORE):
    if uuid.UUID(receipt).hex!=receipt:
        raise ValueError("交卷编号无效")
    db=sqlite3.connect(store.resolve().as_uri()+"?mode=ro",uri=True)
    try:
        row=db.execute("SELECT sha256,data FROM submissions WHERE id=?",(receipt,)).fetchone()
    finally:
        db.close()
    if row is None or hashlib.sha256(row[1]).hexdigest()!=row[0]:
        raise ValueError("交卷不存在或校验失败")
    return json.loads(row[1])


STAGES = {"jog": run_jog, "position": run_position, "speed": run_speed, "torque": run_torque}
WORK_ERRORS = (ValueError, TypeError, KeyError, AttributeError, SyntaxError, OverflowError, RecursionError)


def grade(submission, packet, seed=None):
    seed = secrets.randbits(63) if seed is None else seed
    rng = random.Random(seed)
    stages = {name: {"passed": False, "reason": "程序或组态未能加载"} for name in STAGES}
    report = {"passed": False, "graded_version": 2, "submitted_version": submission.get("exam_version", 1),
              "seed": seed, "checkpoint_on_record": bool(submission.get("jog_checkpoint")), "stages": stages}
    room=Room(packet)
    try:
        for name,data in submission["work"].items():
            room.write(name,data)
        room.compile_work()
        deployed=room.operate("download")
    except WORK_ERRORS:
        return report
    for name, run in STAGES.items():
        try:
            stages[name] = run(room, deployed["requested_marker"], rng)
        except WORK_ERRORS:
            stages[name] = {"passed": False, "reason": "本项运行期间程序输出无效"}
    report["passed"] = all(stage["passed"] for stage in stages.values())
    return report


if __name__=="__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("receipt")
    parser.add_argument("--seed", type=int, help="按已有回执中的给定复测")
    args=parser.parse_args()
    print(json.dumps(grade(fetch(args.receipt),load_packet(),args.seed),ensure_ascii=False,indent=2))
