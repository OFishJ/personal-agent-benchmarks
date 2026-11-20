"""Read a submitted FreeCAD assembly in memory. The source file is never saved."""
import argparse
import itertools
import json
from pathlib import Path
import FreeCAD as App
from geometry import LAYOUT, parts, world


def overlaps(a,b):
    left,right=a.BoundBox,b.BoundBox
    return all(min(getattr(left,k+"Max"),getattr(right,k+"Max"))-
               max(getattr(left,k+"Min"),getattr(right,k+"Min"))>1e-5 for k in "XYZ")


def clashes(objects):
    collisions=[]
    for (a,sa),(b,sb) in itertools.combinations(objects.items(),2):
        if overlaps(sa,sb):
            volume=sa.common(sb).Volume
            if volume>0.05:
                collisions.append({"pair":[a,b],"common_mm3":round(volume,4)})
    return collisions


def world_shape(component):
    """Shapes already include the instance placement. Apply its containing frame once."""
    shape = component.Shape.copy()
    frame = component.getParentGeoFeatureGroup()
    if frame is not None:
        shape.Placement = frame.getGlobalPlacement().multiply(shape.Placement)
    return shape


def main(model,mapping):
    document=App.openDocument(str(model.resolve()))
    try:
        ids=mapping["instances"]
        required={entry[0] for entry in LAYOUT}
        if set(ids)!=required or len(set(ids.values()))!=len(required):
            raise ValueError("Map all 18 distinct instances exactly once")
        driver=mapping["driver"]
        control=document.getObject(driver["object"])
        param=driver["property"]
        if driver["unit"] not in {"turns","deg"} or param not in control.PropertiesList:
            raise ValueError("Invalid exposed drive input")
        components={name:document.getObject(value) for name,value in ids.items()}
        nominal=parts()
        results=[]
        for turns in (0,1.25,5,4.5,7.375,10,0):
            setattr(control,param,turns*(360 if driver["unit"]=="deg" else 1))
            document.recompute()
            expected=world(nominal,turns)
            submitted={name:world_shape(obj) for name,obj in components.items()}
            findings={}
            for name,reference in expected.items():
                shape=submitted[name]
                if shape.isNull() or not shape.isValid() or len(shape.Solids)!=1:
                    findings[name]={"status":"invalid or not one solid"}
                    continue
                extra=shape.cut(reference).Volume
                missing=reference.cut(shape).Volume
                if max(extra,missing)>0.02:
                    findings[name]={"extra_mm3":round(extra,5),"missing_mm3":round(missing,5)}
            results.append({"turns":turns,"saddle_xmin_mm":submitted["P03"].BoundBox.XMin,
                            "peg_center_yz_mm":[submitted["P09"].Solids[0].CenterOfMass.y,submitted["P09"].Solids[0].CenterOfMass.z],
                            "non_nominal":findings,"interference":clashes(submitted)})
        return results
    finally:
        App.closeDocument(document.Name)


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model",type=Path)
    parser.add_argument("mapping",type=Path)
    parser.add_argument("--out",type=Path)
    args=parser.parse_args()
    result=main(args.model,json.loads(args.mapping.read_text(encoding="utf-8-sig")))
    output=json.dumps(result,indent=2)
    if args.out:
        args.out.write_text(output+"\n",encoding="utf-8")
    else:
        print(output)
