"""Start FreeCAD on a new Windows desktop and attach the GUI-only recorder."""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import win32con
import win32event
import win32process
import win32service


def launch(exe, run):
    exe,run=exe.resolve(strict=True),run.resolve()
    run.mkdir(parents=True,exist_ok=False)
    temp=run/"temp"
    temp.mkdir()
    bridge=Path(__file__).resolve().with_name("qt_bridge_v5.py")
    # Import the copy with the run so observations can be attributed to this version.
    imported=run/"qt_bridge_v5.py"
    shutil.copyfile(bridge,imported)
    macro=run/"start_bridge.FCMacro"
    macro.write_text("import sys\nsys.path.insert(0,"+repr(str(run))+")\nimport qt_bridge_v5\n_recorder=qt_bridge_v5.start()\n",encoding="utf-8")
    name="FreeCADExam_"+uuid.uuid4().hex[:12]
    desktop=win32service.CreateDesktop(name,0,win32con.GENERIC_ALL,None)
    try:
        startup=win32process.STARTUPINFO()
        startup.lpDesktop="winsta0\\"+name
        startup.dwFlags=win32con.STARTF_USESHOWWINDOW
        startup.wShowWindow=win32con.SW_SHOWNORMAL
        env=os.environ.copy()
        env.update(CODEX_CAD_RUN=str(run),CODEX_CAD_DESKTOP=name,
                   CODEX_CAD_EXPECTED_BRIDGE_SHA256=hashlib.sha256(imported.read_bytes()).hexdigest(),
                   QT_SCALE_FACTOR="1",QT_SCREEN_SCALE_FACTORS="1",TEMP=str(temp),TMP=str(temp),
                   PYTHONDONTWRITEBYTECODE="1")
        command=[str(exe),"--user-cfg",str(run/"user.cfg"),"--system-cfg",str(run/"system.cfg"),
                 "--log-file",str(run/"freecad.log"),str(macro)]
        process,thread,pid,_=win32process.CreateProcess(str(exe),subprocess.list2cmdline(command),
                None,None,False,win32con.CREATE_NO_WINDOW,env,str(exe.parent),startup)
        thread.Close()
        try:
            (run/"launch.json").write_text(json.dumps({"pid":pid,"desktop":name,"run":str(run),"exe":str(exe)}),encoding="utf-8")
            end=time.monotonic()+30
            while time.monotonic()<end:
                if (run/"ready.json").is_file():
                    print((run/"ready.json").read_text(encoding="utf-8"))
                    return
                if win32event.WaitForSingleObject(process,0)==win32con.WAIT_OBJECT_0:
                    break
                time.sleep(0.1)
            raise RuntimeError(f"Bridge did not report ready. Inspect {run / 'freecad.log'}.")
        finally:
            process.Close()
    finally:
        desktop.CloseDesktop()


if __name__=="__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("--exe",type=Path,required=True)
    parser.add_argument("--run",type=Path,required=True,help="fresh directory on a disk with space for recording")
    options=parser.parse_args()
    launch(options.exe,options.run)
