"""UT2004 .t3d writer.

Emits the text map format UnrealEd imports: a Begin Map block of actors, where
brushes carry an inline Begin Brush / PolyList of polygons.
"""

import re

# UE2 EPolyFlags (Engine/Inc/UnObj.h). Only the CSG-relevant flags below carry
# the same meaning in UE3; every other bit differs between the engines and is
# dropped rather than passed through (UE3's 0x800, for instance, would land on
# UE2's PF_NoSmooth).
PF_INVISIBLE = 0x00000001
PF_NOT_SOLID = 0x00000008
PF_SEMISOLID = 0x00000020
PF_TWO_SIDED = 0x00000100
PF_PORTAL = 0x04000000
# Not shared with UE3 -- set by the converter so the enclosing world brush shows
# the skybox rather than a flat texture (Engine/Inc/UnObj.h).
PF_FAKE_BACKDROP = 0x00000080

SHARED_POLY_FLAGS = PF_INVISIBLE | PF_NOT_SOLID | PF_SEMISOLID | PF_TWO_SIDED | PF_PORTAL


def num(v):
    """UnrealEd's canonical fixed-width float, e.g. +00512.000000."""
    return "%+013.6f" % v


def vec(v):
    return "(X=%f,Y=%f,Z=%f)" % (v[0], v[1], v[2])


def rot(r):
    return "(Pitch=%d,Yaw=%d,Roll=%d)" % (r[0], r[1], r[2])


class Polygon:
    def __init__(self, origin, normal, texture_u, texture_v, vertices,
                 texture=None, flags=0, link=-1, item=None, pan_u=0, pan_v=0):
        self.origin = origin
        self.normal = normal
        self.texture_u = texture_u
        self.texture_v = texture_v
        self.vertices = vertices
        self.texture = texture
        self.flags = flags
        self.link = link
        self.item = item
        self.pan_u = pan_u
        self.pan_v = pan_v

    def lines(self, indent):
        pad = " " * indent
        head = "Begin Polygon"
        if self.item:
            head += " Item=%s" % self.item
        if self.texture:
            head += " Texture=%s" % self.texture
        head += " Flags=%d" % self.flags
        if self.link >= 0:
            head += " Link=%d" % self.link
        out = [pad + head]
        inner = pad + "    "
        out.append("%sOrigin   %s,%s,%s" % ((inner,) + tuple(num(c) for c in self.origin)))
        out.append("%sNormal   %s,%s,%s" % ((inner,) + tuple(num(c) for c in self.normal)))
        out.append("%sTextureU %s,%s,%s" % ((inner,) + tuple(num(c) for c in self.texture_u)))
        out.append("%sTextureV %s,%s,%s" % ((inner,) + tuple(num(c) for c in self.texture_v)))
        for v in self.vertices:
            out.append("%sVertex   %s,%s,%s" % ((inner,) + tuple(num(c) for c in v)))
        out.append(pad + "End Polygon")
        return out


class Actor:
    def __init__(self, cls, name, properties=None):
        self.cls = cls
        self.name = name
        self.properties = list(properties or [])  # (key, formatted value)

    def lines(self, indent=0):
        pad = " " * indent
        out = ["%sBegin Actor Class=%s Name=%s" % (pad, self.cls, self.name)]
        for key, value in self.properties:
            out.append("%s    %s=%s" % (pad, key, value))
        out.append("%s    Name=%s" % (pad, self.name))
        out.append("%sEnd Actor" % pad)
        return out


class ObjectActor(Actor):
    """An actor that owns inline objects, the way T3D writes a `Begin Object`.

    UT2004 keeps a ScriptedTrigger's actions and an Emitter's emitters as
    objects the *level* owns, not as subobjects: `Begin Object` inside an actor
    constructs with the level package as outer (Editor/Src/UnEditor.cpp:829),
    which is why the editor's own exports refer to them as `MyLevel.<name>`.
    """

    def __init__(self, cls, name, objects, ref_property, properties=None):
        Actor.__init__(self, cls, name, properties)
        self.objects = objects              # [(class, name, [(key, value)])]
        self.ref_property = ref_property    # "Actions" / "Emitters"

    def lines(self, indent=0):
        pad = " " * indent
        out = ["%sBegin Actor Class=%s Name=%s" % (pad, self.cls, self.name)]
        for cls, name, props in self.objects:
            out.append("%s    Begin Object Class=%s Name=%s" % (pad, cls, name))
            for key, value in props:
                out.append("%s        %s=%s" % (pad, key, value))
            out.append("%s    End Object" % pad)
        for i, (cls, name, _props) in enumerate(self.objects):
            out.append("%s    %s(%d)=%s'MyLevel.%s'"
                       % (pad, self.ref_property, i, cls, name))
        for key, value in self.properties:
            out.append("%s    %s=%s" % (pad, key, value))
        out.append("%s    Name=%s" % (pad, self.name))
        out.append("%sEnd Actor" % pad)
        return out


class Brush(Actor):
    def __init__(self, name, model_name, polygons, csg="CSG_Add", properties=None,
                 cls="Brush", objects=(), ref_property=None):
        Actor.__init__(self, cls, name, properties)
        self.model_name = model_name
        self.polygons = polygons
        self.csg = csg
        # Inline objects, as ObjectActor writes them -- a volume's I3DL2
        # room effect is one. [(class, name, [(key, value)])].
        self.objects = list(objects)
        self.ref_property = ref_property

    def lines(self, indent=0):
        pad = " " * indent
        out = ["%sBegin Actor Class=%s Name=%s" % (pad, self.cls, self.name)]
        out.append("%s    CsgOper=%s" % (pad, self.csg))
        for cls, name, props in self.objects:
            out.append("%s    Begin Object Class=%s Name=%s" % (pad, cls, name))
            for key, value in props:
                out.append("%s        %s=%s" % (pad, key, value))
            out.append("%s    End Object" % pad)
        if self.ref_property and self.objects:
            cls, name, _props = self.objects[0]
            out.append("%s    %s=%s'MyLevel.%s'" % (pad, self.ref_property, cls, name))
        for key, value in self.properties:
            out.append("%s    %s=%s" % (pad, key, value))
        out.append("%s    Begin Brush Name=%s" % (pad, self.model_name))
        out.append("%s        Begin PolyList" % pad)
        for poly in self.polygons:
            out.extend(poly.lines(indent + 12))
        out.append("%s        End PolyList" % pad)
        out.append("%s    End Brush" % pad)
        out.append("%s    Brush=Model'MyLevel.%s'" % (pad, self.model_name))
        out.append("%s    Name=%s" % (pad, self.name))
        out.append("%sEnd Actor" % pad)
        return out


# Engine/Inc/Engine.h: UE2 clamps every coordinate to this, so an actor placed
# outside it does not land where it says -- it lands somewhere wrong, or not at
# all. Repeated here rather than imported so the writer can police itself.
HALF_WORLD_MAX = 262144.0


class T3DMap:
    def __init__(self):
        self.actors = []

    def add(self, actor):
        self.actors.append(actor)
        return actor

    def out_of_world(self, limit=HALF_WORLD_MAX):
        """Actors whose Location is past what UE2 can represent.

        A last line of defence rather than a diagnosis: whatever put one there
        is the real bug, but shipping it guarantees a map that misbehaves in a
        way that is hard to trace back. CTF-FacingWorlds put an entire skybox
        out here once, and the only symptom in game was that the sky was gone.
        """
        found = []
        for actor in self.actors:
            for key, value in actor.properties:
                if key != "Location":
                    continue
                m = re.match(r"\(X=(\S+?),Y=(\S+?),Z=(\S+?)\)", value)
                if m and max(abs(float(c)) for c in m.groups()) > limit:
                    found.append(actor)
                break
        return found

    def drop(self, actors):
        unwanted = {id(a) for a in actors}
        self.actors = [a for a in self.actors if id(a) not in unwanted]

    def text(self):
        out = ["Begin Map"]
        for actor in self.actors:
            out.extend(actor.lines(3))
        out.append("End Map")
        out.append("")
        return "\r\n".join(out)

    def write(self, path):
        # UnrealEd is happiest with CRLF and Latin-1, matching its own exports.
        with open(path, "wb") as f:
            f.write(self.text().encode("latin-1", "replace"))
