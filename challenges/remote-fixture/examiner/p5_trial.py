"""Private examiner time log. Record the submission before reviewing engineering quality."""
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import time

PHASES=("setup","adapt","design","wait")
GATES=("mechanical_assembly","powered_assembly","startup","backdrive","backlash","fabrication","operator_check")
SCHEMA="""CREATE TABLE IF NOT EXISTS events(
    seq INTEGER PRIMARY KEY, trial TEXT NOT NULL, model TEXT NOT NULL, ns INTEGER NOT NULL,
    kind TEXT NOT NULL, value TEXT NOT NULL, note TEXT NOT NULL, digest TEXT NOT NULL
)"""


def events(conn,trial,case):
    return conn.execute("SELECT ns,kind,value,note,digest FROM events WHERE trial=? AND model=? ORDER BY seq",
                        (trial,case)).fetchall()


def record(conn,trial,case,kind,value,note,file=None,now=None):
    conn.execute("BEGIN IMMEDIATE")
    rows=events(conn,trial,case)
    kinds=[r[1] for r in rows]
    digest=""
    if file is not None:
        if not file.is_file() or file.stat().st_size==0:
            raise ValueError("Missing independent evidence")
        digest=hashlib.sha256(file.read_bytes()).hexdigest()
    if kind=="start":
        if rows or not note.strip() or value not in PHASES:
            raise ValueError("Need a new case, phase and station/agent identity")
        if case=="repeat":
            previous=events(conn,trial,"first")
            if not any(r[1]=="submit" for r in previous) or previous[0][3]!=note:
                raise ValueError("Same trial, agent and station required after first submission")
    elif not rows:
        raise ValueError("Case has not started")
    elif kind=="review":
        if "submit" not in kinds or "review" in kinds or file is None:
            raise ValueError("Review needs one prior submission and independent evidence")
        data=json.loads(file.read_text(encoding="utf-8-sig"))
        if not isinstance(data,dict) or set(data)!=set(GATES) or any(not isinstance(g,dict) or type(g.get("passed")) is not bool or
                not isinstance(g.get("evidence"),str) or not g["evidence"].strip() for g in data.values()):
            raise ValueError("Record all engineering gates and their evidence")
        value="pass" if all(g["passed"] for g in data.values()) else "partial"
    elif "submit" in kinds:
        raise ValueError("Candidate clock is closed")
    elif kind=="phase" and value not in PHASES:
        raise ValueError("Unknown phase")
    elif kind=="note" and (value not in ("help","hint","observation","environment") or not note.strip()):
        raise ValueError("Observation type and description required")
    elif kind=="submit" and file is None:
        raise ValueError("Attach the submission or its file/hash manifest")
    elif kind not in ("phase","note","submit"):
        raise ValueError("Unknown event")
    ns=time.time_ns() if now is None else now
    if rows and ns<rows[-1][0]:
        raise ValueError("Examiner wall clock moved backwards")
    conn.execute("INSERT INTO events(trial,model,ns,kind,value,note,digest) VALUES(?,?,?,?,?,?,?)",
                 (trial,case,ns,kind,value,note,digest))
    conn.commit()


def summary(rows):
    if not rows:
        return None
    phase=rows[0][2]
    accumulated={k:0 for k in PHASES}
    submit=next((r for r in rows if r[1]=="submit"),None)
    for a,b in zip(rows,rows[1:]):
        if a[1]=="submit":
            break
        if a[1]=="phase":
            phase=a[2]
        accumulated[phase]+=b[0]-a[0]
    return dict(submitted=bool(submit),review=next((r[2] for r in rows if r[1]=="review"),None),
                observed_seconds={k:v/1e9 for k,v in accumulated.items()},
                elapsed_seconds=(submit[0]-rows[0][0])/1e9 if submit else None,
                help=[dict(kind=r[2],note=r[3]) for r in rows if r[1]=="note"],
                submission_sha256=submit[4] if submit else None)


def compare(conn,trial):
    first=summary(events(conn,trial,"first"))
    repeat=summary(events(conn,trial,"repeat"))
    matched=bool(first and repeat and first["review"]=="pass" and repeat["review"]=="pass")
    ratio=None
    if matched and first["elapsed_seconds"]>0:
        ratio=repeat["elapsed_seconds"]/first["elapsed_seconds"]
    return dict(first=first,repeat=repeat,both_passed=matched,repeat_over_first_wall_time=ratio)


if __name__=="__main__":
    cli=argparse.ArgumentParser(description=__doc__)
    cli.add_argument("database",type=Path)
    cli.add_argument("trial")
    cli.add_argument("model",choices=("first","repeat","pair"))
    cli.add_argument("event",choices=("start","phase","note","submit","review","report"))
    cli.add_argument("--value",default="")
    cli.add_argument("--note",default="")
    cli.add_argument("--evidence",type=Path)
    a=cli.parse_args()
    if a.model=="pair" and a.event!="report":
        cli.error("pair is read-only")
    if a.event=="report" and not a.database.is_file():
        cli.error("No examination database")
    with sqlite3.connect(a.database) as db:
        db.execute(SCHEMA)
        if a.event=="report":
            result=compare(db,a.trial) if a.model=="pair" else summary(events(db,a.trial,a.model))
            print(json.dumps(result,ensure_ascii=False,indent=2))
        else:
            record(db,a.trial,a.model,a.event,a.value,a.note,a.evidence)
