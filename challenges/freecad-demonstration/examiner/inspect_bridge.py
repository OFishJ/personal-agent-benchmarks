"""Inspect a submitted FCStd using FreeCAD's own Python and geometry kernel."""
import argparse
import json
import math
from pathlib import Path

import FreeCAD as App
import Part

V = App.Vector


def expected(rise=0):
    base = Part.makeBox(80, 50, 10, V(-40, -25, 0))
    pillars = [Part.makeBox(10, 20, 20 + rise, V(x-5, -10, 10)) for x in (-30, 30)]
    plate = Part.makeBox(80, 20, 6, V(-40, -10, 30+rise))
    center = Part.makeCylinder(6, 30+rise)
    return base.multiFuse([*pillars, plate, center]).removeSplitter()


def compare(shape, target):
    if shape.isNull() or not shape.isValid():
        return {"valid": False, "matches": False}
    extra = shape.cut(target).Volume
    missing = target.cut(shape).Volume
    return {"valid": True, "solids": len(shape.Solids), "volume_mm3": shape.Volume,
            "extra_mm3": extra, "missing_mm3": missing,
            "matches": len(shape.Solids) == 1 and max(extra, missing) < 0.01}


def center_ref(pad):
    if pad.Type != "UpToFace" or not pad.UpToFace:
        return None
    obj, subs = pad.UpToFace
    for name in subs:
        face = obj.Shape.getElement(name)
        bb = face.BoundBox
        if not (abs(bb.ZMin-30) < 1e-5 and abs(bb.ZMax-30) < 1e-5):
            continue
        if face.distToShape(Part.Vertex(V(0, 0, 30)))[0] > 1e-5:
            continue
        sketch = pad.Profile[0]
        profile = sketch.Shape
        if len(profile.Edges) != 1:
            continue
        curve = profile.Edges[0].Curve
        bbp = profile.BoundBox
        if (hasattr(curve, "Radius") and math.isclose(curve.Radius, 6, abs_tol=1e-5)
                and all(abs(a-b)<1e-5 for a,b in zip(
                    [bbp.XMin,bbp.XMax,bbp.YMin,bbp.YMax,bbp.ZMin,bbp.ZMax],[-6,6,-6,6,0,0]))):
            return {"pad": pad.Name, "feature": obj.Name, "face": name}
    return None


def perturbation(history):
    """Return editable dimensions for two recognized modeling histories."""
    joint = [o for o in history if o.TypeId == "PartDesign::Pad" and o.Type == "Length"
             and abs(o.Length.Value-20) < 1e-5 and not o.Shape.isNull()
             and abs(o.Shape.BoundBox.ZMax-30) < 1e-5]
    if len(joint) == 1:
        return [(joint[0], "Length", joint[0].Length.Value + 5)]
    posts, deck = [], []
    for obj in history:
        if obj.TypeId != "PartDesign::AdditiveBox" or obj.MapMode != "Deactivated":
            continue
        dimensions = [obj.Length.Value, obj.Width.Value, obj.Height.Value]
        if all(math.isclose(a,b,abs_tol=1e-5) for a,b in zip(dimensions, [10,20,20])):
            posts.append((obj, "Height", obj.Height.Value + 5))
        elif all(math.isclose(a,b,abs_tol=1e-5) for a,b in zip(dimensions, [80,20,6])):
            raised = obj.Placement
            raised.Base = raised.Base + V(0,0,5)
            deck.append((obj, "Placement", raised))
    return posts + deck if len(posts) == 2 and len(deck) == 1 else []


def inspect(path):
    doc = App.openDocument(str(path.resolve()))
    try:
        for obj in doc.Objects:
            obj.touch()
        doc.recompute()
        bodies = [o for o in doc.Objects if o.TypeId == "PartDesign::Body" and o.Tip]
        results = []
        for body in bodies:
            geometry = compare(body.Shape, expected())
            refs = []
            history = [body.Tip, *body.Tip.OutListRecursive]
            for pad in (o for o in history if o.TypeId == "PartDesign::Pad"):
                try:
                    ref = center_ref(pad)
                except (AttributeError, ValueError, RuntimeError, TypeError):
                    ref = None
                if ref:
                    refs.append(ref)
            result = {"body": body.Name, "geometry": geometry, "center_references": refs,
                      "rise_5mm": "review edit on a disposable copy"}
            # Recognize the ordinary feature chain by geometry and dimensions.
            # Other histories remain eligible for the examiner's manual perturbation.
            changes = perturbation(history) if geometry["matches"] else []
            if changes:
                original = [(obj, name, getattr(obj,name)) for obj,name,_ in changes]
                try:
                    for obj,name,value in changes:
                        setattr(obj,name,value)
                    doc.recompute()
                    moved = compare(body.Shape, expected(5))
                    result["rise_trial"] = moved
                    if moved["matches"]:
                        result["rise_5mm"] = "verified"
                    else:
                        result["rise_5mm"] = "inspect mismatch on this edit"
                finally:
                    for obj,name,value in original:
                        setattr(obj,name,value)
                    doc.recompute()
            results.append(result)
        return {"file": path.name, "bodies": results, "remaining_review":
                "Check unrecognized edit histories, additional visible shapes, GUI origin, recording and save evidence independently."}
    finally:
        App.closeDocument(doc.Name)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("model", type=Path)
    args = parser.parse_args()
    print(json.dumps(inspect(args.model), ensure_ascii=True, indent=2))
