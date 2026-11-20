"""Package only material issued to a candidate, including the previously validated GUI station."""
import argparse
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATION = ROOT.parent/"freecad-demonstration/station"
MATERIALS = [
    "任务书.md", "FS40-装配示意与零件图.pdf", "assembly-map.example.json",
    *[f"incoming/{name}.step" for name in ("P01","P05","P07","P08")],
    *[f"supplied/{name}.step" for name in ("P06","P10","P11")],
]
STATION_FILES = ["README.md","launch.py","bgctl.py","qt_bridge_v5.py"]


def make(path):
    sources = {member:ROOT/"candidate"/member for member in MATERIALS}
    sources.update({"station/"+member:STATION/member for member in STATION_FILES})
    with zipfile.ZipFile(path,"x",compression=zipfile.ZIP_DEFLATED) as archive:
        for member,source in sources.items():
            archive.write(source,member)
    with zipfile.ZipFile(path) as archive:
        assert archive.testzip() is None and set(archive.namelist()) == set(sources)
        for member,source in sources.items():
            assert archive.read(member) == source.read_bytes()
    return list(sources)


if __name__ == "__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination",type=Path,help="a new .zip file")
    print("Verified",len(make(parser.parse_args().destination)),"candidate files.")
