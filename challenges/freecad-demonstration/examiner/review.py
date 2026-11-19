"""Create examiner evidence for B/C. Geometry and semantic judgments are separate."""
import argparse
from collections import Counter
import hashlib
import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def sha(path):
    data = path.read_bytes()
    if path.suffix in {".md", ".json"}:
        data = data.replace(b"\r\n", b"\n")
    return hashlib.sha256(data).hexdigest()


def index_rows(path, key):
    if not path.is_file():
        return {}, ["submission missing"]
    try:
        rows = load(path)
        if not isinstance(rows, list):
            raise ValueError("top level must be an array")
        items, problems = {}, []
        for i, row in enumerate(rows, 1):
            if not isinstance(row, dict) or type(row.get("step")) is not int:
                problems.append(f"row {i}: invalid step")
                continue
            identity = key(row)
            if identity in items:
                problems.append(f"duplicate key {identity}")
            else:
                items[identity] = row
        return items, problems
    except (OSError, UnicodeError, ValueError, KeyError, TypeError) as exc:
        return {}, [str(exc)]


def overlap(a, b):
    x = max(0, min(a["x"]+a["w"], b["x"]+b["w"])-max(a["x"], b["x"]))
    y = max(0, min(a["y"]+a["h"], b["y"]+b["h"])-max(a["y"], b["y"]))
    return x*y / (a["w"]*a["h"]+b["w"]*b["h"]-x*y)


def boxes(path):
    rows, problems = index_rows(path, lambda r:(r["step"],r["role"]))
    gold = load(ROOT/"examiner/boxes-reference.json")
    actions = load(ROOT/"candidate/B/actions.json")["ground_truth_actions"]
    result = []
    for ref in gold:
        step, role = ref["step"], ref["role"]
        action = actions[step-1]
        x,y = action["coordinate" if role=="click" else role+"_coordinate"]
        row = rows.pop((step,role),{})
        info = dict(step=step,role=role,image=action["image_path"],point=[x,y])
        if not row:
            info["status"] = "missing"
        elif not all(type(row.get(k)) in (int,float) and math.isfinite(row[k]) for k in ("x","y","w","h")):
            info["status"] = "invalid coordinates"
        elif not (row["x"]>=0 and row["y"]>=0 and row["w"]>0 and row["h"]>0 and row["x"]+row["w"]<=1920 and row["y"]+row["h"]<=1080):
            info["status"] = "out of frame"
        elif not (row["x"]<=x<=row["x"]+row["w"] and row["y"]<=y<=row["y"]+row["h"]):
            info["status"] = "point outside"
        else:
            info["iou"] = round(overlap(row,ref),4)
            info["status"] = "matches reference" if all(row[k]==ref[k] for k in ("x","y","w","h")) else "inspect target on image"
        result.append(info)
    if rows:
        problems.append("unexpected keys: " + str(list(rows)))
    return {"schema_problems":problems,"counts":dict(Counter(r["status"] for r in result)),"items":result}


def intents(path):
    rows, problems = index_rows(path,lambda r:r["step"])
    gold = load(ROOT/"examiner/intents-reference.json")
    items = []
    for ref in gold:
        step = ref["step"]
        row = rows.pop(step,{})
        sentence = row.get("human_intent")
        if not isinstance(sentence,str) or not sentence.strip() or sentence.strip()=="TO_FILL":
            status="missing"
        elif sentence == ref["human_intent"]:
            status="matches reference"
        else:
            status="review meaning"
        items.append({"step":step,"status":status})
    if rows:
        problems.append("unexpected steps: "+str(list(rows)))
    return {"schema_problems":problems,"counts":dict(Counter(i["status"] for i in items)),"items":items}


def verify_dataset():
    for part,count in (("B",63),("C",90)):
        folder=ROOT/"candidate"/part
        doc=load(folder/"actions.json")
        actions=doc["ground_truth_actions"]
        assert len(actions)==doc["total_steps"]==count
        assert [a["step"] for a in actions]==list(range(1,count+1))
        assert not any("human_intent" in a for a in actions)
        frames=[a["image_path"] for a in actions]+[doc["final_state_image"]]
        assert frames==[f"frames/step_{i:03d}.png" for i in range(count+1)]
        assert len(list((folder/"frames").glob("*.png")))==len(frames)
    manifest=load(ROOT/"examiner/input-sha256.json")
    expected={p.relative_to(ROOT/"candidate").as_posix() for p in (ROOT/"candidate").rglob("*") if p.is_file()}
    assert set(manifest)==expected
    for name,digest in manifest.items():
        assert sha(ROOT/"candidate"/name)==digest, name


if __name__=="__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("submission",nargs="?",type=Path)
    parser.add_argument("--verify-inputs",action="store_true")
    parser.add_argument("--output",type=Path)
    args=parser.parse_args()
    if args.verify_inputs:
        verify_dataset()
        print("Verified both input sequences and the candidate manifest.")
    if args.submission:
        result={"B":boxes(args.submission/"boxes.json"),"C":intents(args.submission/"intents.json")}
        data=json.dumps(result,ensure_ascii=False,indent=2)
        if args.output:
            args.output.write_text(data+"\n",encoding="utf-8")
        else:
            print(data)
    elif not args.verify_inputs:
        parser.error("supply a submission or use --verify-inputs")
