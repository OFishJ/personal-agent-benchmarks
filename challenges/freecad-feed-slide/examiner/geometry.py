"""Nominal geometry for the examiner, in millimetres. Local part axes are on the drawings."""
import math
import FreeCAD as App
import Part

V = App.Vector
X = V(1, 0, 0)
Y = V(0, 1, 0)
Z = V(0, 0, 1)


def block(x, y, z, dx, dy, dz):
    return Part.makeBox(dx, dy, dz, V(x, y, z))


def axial(x, y, z, diameter, length):
    return Part.makeCylinder(diameter / 2, length, V(x, y, z), X)


def upright(x, y, z, diameter, length):
    return Part.makeCylinder(diameter / 2, length, V(x, y, z), Z)


def pierce(shape, tools):
    return shape.cut(Part.makeCompound(tools)).removeSplitter()


def d_section(x, diameter, length):
    return axial(x, 0, 0, diameter, length).cut(block(x-1, 5, -10, length+2, 12, 20))


def base():
    holes = [upright(x, y, 0, 5, 8) for x in (16, 144) for y in (-33, 33)]
    holes += [upright(x, y, 0, 6.6, 8) for x in (36, 124) for y in (-36, 36)]
    return pierce(block(0, -44, 0, 160, 88, 8), holes)


def pedestal():
    outline = [(-38,0),(38,0),(38,8),(29,8),(29,30),(24,38),
               (-24,38),(-29,30),(-29,8),(-38,8)]
    points = [V(0, y, z) for y, z in outline]
    prism = Part.Face(Part.makePolygon(points + points[:1])).extrude(V(12,0,0))
    holes = [axial(0,y,26,8,12) for y in (-21,21)]
    holes += [upright(6,y,0,5.5,8) for y in (-33,33)]
    return (pierce(prism, holes + [axial(0,0,26,12.2,12)])
            .cut(axial(0,0,26,18,6)).cut(upright(3,0,34,2,4)).removeSplitter())


def saddle(top_pitch=24):
    body = block(0,-28,0,40,56,22).fuse(block(0,-33,22,40,66,6))
    holes = [axial(0,y,12,8.4,40) for y in (-21,21)]
    holes += [axial(0,-12,3,3,8), axial(0,12,21,3,8), axial(0,0,12,22.2,40)]
    holes += [upright(x,y,22,5,6) for x in (20-top_pitch/2,20+top_pitch/2) for y in (-24,24)]
    return pierce(body,holes)


def wheel():
    wheel_shape = axial(0,0,0,44,8).fuse(axial(8,0,0,20,8))
    return pierce(wheel_shape,[d_section(0,12,16), axial(0,14,0,5,8)])


def spindle():
    # Left tip is local x=0. The two shoulders contact the inner pedestal faces.
    shaft = d_section(0,12,16)
    for x, diameter, length in ((16,12,18),(34,16,116),(150,12,12)):
        shaft = shaft.fuse(axial(x,0,0,diameter,length))
    return shaft.removeSplitter()


def nut():
    flange = block(0,-16,-14,4,32,28)
    body = flange.fuse(axial(4,0,0,22,40))
    return pierce(body,[axial(0,0,0,16.2,44),axial(0,-12,-9,3.4,4),axial(0,12,9,3.4,4)])


def bush(width):
    body = axial(0,0,0,18,6).cut(axial(0,0,0,12.2,6))
    groove = axial(3-width/2,0,0,20,width).cut(axial(3-width/2,0,0,16,width))
    oil = Part.makeCylinder(0.75,11,V(3,0,0),Y)
    return body.cut(groove).cut(oil).removeSplitter()


def parts(as_received=False):
    return {"P01":base(), "P02":pedestal(), "P03":saddle(), "P04":wheel(), "P05":spindle(),
            "P06":nut(), "P07":axial(0,0,0,8.6 if as_received else 8,140),
            "P08":bush(1 if as_received else 2),
            "P09":axial(0,0,0,8,16).fuse(axial(16,0,0,5,8)).removeSplitter(),
            "P10":upright(0,0,0,5,16).fuse(upright(0,0,16,8,4)).removeSplitter(),
            "P11":axial(0,0,0,5.5,3).fuse(axial(3,0,0,3,10)).removeSplitter()}


# Each entry is (stable instance, local origin at zero, Z rotation for opposite handed placement).
LAYOUT = [
    ("P01", (0,0,0),0), ("P02L",(10,0,8),0), ("P02R",(150,0,8),180),
    ("P03",(36,0,22),0), ("P04",(-12,0,34),0), ("P05",(-12,0,34),0),
    ("P06",(32,0,34),0), ("P07a",(10,-21,34),0), ("P07b",(10,21,34),0),
    ("P08L",(10,0,34),0), ("P08R",(150,0,34),180), ("P09",(-28,14,34),0),
    ("P10a",(16,-33,0),0), ("P10b",(16,33,0),0), ("P10c",(144,-33,0),0),
    ("P10d",(144,33,0),0), ("P11a",(29,-12,25),0), ("P11b",(29,12,43),0)]
MOVING = {"P03","P06","P11"}
ROTATING = {"P04","P05","P09"}


def pose(inst, xyz, flip, turns):
    code = inst[:3]
    pos = V(*xyz)
    rot = App.Rotation(Z,flip)
    if code in MOVING:
        pos.x += 4*turns
    if code in ROTATING:
        rot = App.Rotation(X,turns*360)
        pivot = V(0,0,34)
        pos = pivot + rot.multVec(pos-pivot)
    return App.Placement(pos,rot)


def world(shapes, turns=0):
    result = {}
    for name,xyz,flip in LAYOUT:
        shape = shapes[name[:3]].copy()
        shape.Placement = pose(name,xyz,flip,turns).multiply(shape.Placement)
        result[name] = shape
    return result
