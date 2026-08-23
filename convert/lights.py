"""Light conversion: UE3 light actors/components -> UT2004 Light actors.

UE3 keeps the interesting values on a LightComponent hanging off the actor;
UE2 keeps them on the actor itself. The unit systems differ in three ways that
matter, all verified against the UT2004 source:

* radius   -- UE2 stores LightRadius such that the world radius is
              25 * (LightRadius + 1)   (Engine/Inc/AActor.h WorldLightRadius)
* cone     -- UE2 stores LightCone such that the half-angle is
              acos(1 - LightCone/256) (Engine/Src/UnRenderVisibility.cpp:400)
* colour   -- UE2 LightSaturation is inverted: 255 is white, 0 is fully
              saturated (Engine/Light.uc defaults)

Brightness has no principled mapping between the engines, so it is a tunable
gain (UE3 defaults to 1.0, UE2's Light defaults to 64).
"""

import colorsys
import math

from ut2.t3d import Actor, vec, rot
from convert import collections
from ut3.objects.level import is_placed_actor
from ut3.props import read_object_properties

# UE3 light actor -> (UT2004 class, extra properties)
LIGHT_CLASSES = {
    "PointLight": ("Light", []),
    "PointLightMovable": ("Light", []),
    "PointLightToggleable": ("TriggerLight", []),
    "SpotLight": ("Spotlight", []),
    "SpotLightMovable": ("Spotlight", []),
    "SpotLightToggleable": ("Spotlight", []),
    "DirectionalLight": ("Sunlight", []),
    "DirectionalLightToggleable": ("Sunlight", []),
}

# UE3 component defaults, used when a property is absent (UE3 only serializes
# what differs from the archetype).
DEFAULT_BRIGHTNESS = 1.0
DEFAULT_RADIUS = 1024.0
DEFAULT_OUTER_CONE = 44.0

# UE3 brightness has no principled conversion to UE2's, so the gain is chosen
# empirically: across DM-HeatRay's 468 lights the median UE3 Brightness is 2.0,
# and a gain of 32 maps that onto 64 -- exactly UE2's default LightBrightness
# (Engine/Light.uc). It also keeps clamping rare (7% of lights, against 30% at
# a gain of 64), which matters because clamped lights lose all dynamic range.
DEFAULT_GAIN = 32.0

# Ambient needs its own gain. UE3's SkyLight feeds a tone-mapped HDR pipeline,
# so its raw Brightness (0.5 on DM-HeatRay) is not comparable to a UE2 ambient
# byte, and the mapping is empirical.
#
# Calibrated against the 311 stock .ut2 maps in this install rather than by eye,
# which is what the first attempt got wrong. UT2004 barely uses zone ambient at
# all: 250 of those maps never set AmbientBrightness, leaving it at 0, and of
# the 367 zones that do, the median is 4 and the 90th percentile is 12. Nothing
# sits in the 30-60 band this constant was originally chosen for -- the light in
# a UT2004 map comes from placed lights, and we place 1797 of them here.
#
# 16 puts WAR-PowerSurge's two summed SkyLights at 15 and DM-HeatRay's overcast
# sky at 4, i.e. the top of the range and the median. At the old 128,
# WAR-PowerSurge came out at 118 -- ten times the 90th percentile, and visibly
# brighter than the UT3 original.
DEFAULT_AMBIENT_GAIN = 16.0

# The least ambient a map with a SkyLight at all is given, whatever the gain
# works out to. One gain cannot serve every map, because what varies is not just
# brightness but how many SkyLights UT3 stacked: WAR-PowerSurge has two summing
# to 15, which reads well, while WAR-Torlan has one contributing 3 and comes out
# too dark to play. Raising the gain until Torlan looked right would put
# PowerSurge back near the 118 that was visibly wrong. So the gain sets the
# relationship and this sets the bottom of it. 15 is the value judged good on
# WAR-PowerSurge rather than a number from the stock maps -- those sit lower
# (median 4) but light themselves with hand-placed lights this pipeline only
# approximates.
MIN_SKYLIGHT_AMBIENT = 15


def light_radius_to_ue2(world_radius):
    """Invert WorldLightRadius() = 25 * (LightRadius + 1)."""
    return max(1.0, min(255.0, world_radius / 25.0 - 1.0))


def cone_angle_to_ue2(outer_cone_degrees):
    """Invert half-angle = acos(1 - LightCone/256)."""
    angle = max(0.0, min(90.0, outer_cone_degrees))
    return int(max(0, min(255, round(256.0 * (1.0 - math.cos(math.radians(angle)))))))


def colour_to_ue2(colour):
    """(R,G,B) 0-255 -> (LightHue, LightSaturation, value 0..1).

    UE2's LightSaturation runs the other way from the usual convention: 255
    means white and 0 means fully saturated.
    """
    r, g, b = [max(0, min(255, c)) / 255.0 for c in colour[:3]]
    h, s, v = colorsys.rgb_to_hsv(r, g, b)
    hue = int(round(h * 255.0)) & 0xFF
    saturation = int(round((1.0 - s) * 255.0))
    return hue, max(0, min(255, saturation)), v


class LightStats:
    def __init__(self):
        self.converted = 0
        self.by_class = {}
        self.skipped_disabled = 0
        self.skipped_dark = 0
        self.skipped_no_component = 0
        self.unsupported = {}
        self.ambient = None  # (brightness, hue, saturation) from any SkyLight
        self.ambient_parts = []  # what each SkyLight contributed
        self.ambient_floored = None  # what it was before the floor, if raised

    def __str__(self):
        kinds = ", ".join("%d %s" % (n, k) for k, n in sorted(self.by_class.items()))
        out = "%d lights (%s)" % (self.converted, kinds or "none")
        if self.skipped_disabled:
            out += "; %d disabled" % self.skipped_disabled
        if self.skipped_dark:
            out += "; %d with zero brightness" % self.skipped_dark
        if self.skipped_no_component:
            out += "; %d without a light component" % self.skipped_no_component
        if self.unsupported:
            out += "; skipped " + ", ".join(
                "%d %s" % (n, k) for k, n in sorted(self.unsupported.items())
            )
        return out


def _component_props(pkg, export, props):
    """The actor's LightComponent properties, if reachable."""
    ref = props.get("LightComponent")
    if ref is not None and ref.is_export:
        comp_props, start, _end = read_object_properties(pkg, ref.export)
        if start is not None:
            return comp_props
    # Fall back to the export's component map.
    for name, index in export.components.items():
        if "Light" in name and "Draw" not in name:
            comp = pkg.ref(index)
            if comp.is_export:
                comp_props, start, _end = read_object_properties(pkg, comp.export)
                if start is not None:
                    return comp_props
    return None


def _light_sources(pkg):
    """Every light, as (export, UE3 class, collected). 

    `collected` is None for a real light actor -- the loop reads its properties
    itself -- and (properties, component) for one lifted out of a
    StaticLightCollectionActor, where the transform is a matrix in the
    collection rather than a property on an actor. See convert/collections.py.
    """
    for export in pkg.exports:
        yield export, pkg.class_name_of(export), None
    for export, cls, props, comp in collections.expand_lights(pkg):
        yield export, cls, (props, comp)


def convert_lights(pkg, scale=1.0, gain=DEFAULT_GAIN, radius_scale=1.0,
                   ambient_gain=DEFAULT_AMBIENT_GAIN, stats=None):
    """Convert every supported light actor into a UT2004 Light actor."""
    stats = stats or LightStats()
    out = []
    for export, cls, collected in _light_sources(pkg):
        if cls in ("SkyLight", "SkyLightToggleable"):
            # UT2004 has no SkyLight actor. Its equivalent is the zone's ambient
            # term, which lives on LevelInfo (a ZoneInfo subclass) and so cannot
            # be created by a t3d import -- report the values to set by hand.
            stats.unsupported[cls] = stats.unsupported.get(cls, 0) + 1
            if collected is not None:
                _props, comp = collected
            else:
                props, start, _end = read_object_properties(pkg, export)
                if start is None:
                    continue
                comp = _component_props(pkg, export, props)
            if comp is None:
                continue
            brightness = comp.get("Brightness", DEFAULT_BRIGHTNESS)
            # The lower hemisphere term is fill light too; average the two.
            brightness = (brightness + comp.get("LowerBrightness", 0.0)) / 2.0
            colour = comp.get("LightColor")
            if colour is not None and colour.value:
                hue, saturation, value = colour_to_ue2(colour.value)
            else:
                hue, saturation, value = 0, 255, 1.0
            ambient = int(max(0, min(255, round(brightness * value * ambient_gain))))
            # Every SkyLight lights the scene, so they add. Taking the largest
            # and dropping the rest loses real light: CTF-FacingWorlds has two,
            # contributing 12 and 20, and reads visibly dark at 20. The hue and
            # saturation follow whichever contributes most rather than being
            # averaged, since hue does not average meaningfully.
            if stats.ambient is None:
                stats.ambient = (ambient, hue, saturation)
                stats.ambient_parts = [ambient]
            else:
                total = min(255, stats.ambient[0] + ambient)
                if ambient > max(stats.ambient_parts):
                    stats.ambient = (total, hue, saturation)
                else:
                    stats.ambient = (total, stats.ambient[1], stats.ambient[2])
                stats.ambient_parts.append(ambient)
            continue
        if cls not in LIGHT_CLASSES:
            continue
        if collected is None and not is_placed_actor(pkg, export):
            continue
        ue2_class, extra = LIGHT_CLASSES[cls]

        if collected is not None:
            props, comp = collected
        else:
            props, start, _end = read_object_properties(pkg, export)
            if start is None:
                continue
            comp = _component_props(pkg, export, props)
        if comp is None:
            stats.skipped_no_component += 1
            continue
        if comp.get("bEnabled", True) is False:
            stats.skipped_disabled += 1
            continue

        brightness = comp.get("Brightness", DEFAULT_BRIGHTNESS)
        if brightness <= 0.0:
            stats.skipped_dark += 1
            continue
        colour_struct = comp.get("LightColor")
        if colour_struct is not None and colour_struct.value:
            hue, saturation, value = colour_to_ue2(colour_struct.value)
        else:
            hue, saturation, value = 0, 255, 1.0

        properties = list(extra)
        location = props.get("Location")
        if location is not None and location.value:
            properties.append(("Location", vec([c * scale for c in location.value])))
        rotation = props.get("Rotation")
        if rotation is not None and rotation.value and any(rotation.value):
            properties.append(("Rotation", rot(rotation.value)))

        properties.append(("LightBrightness", "%.6f" % max(0.0, min(255.0, brightness * value * gain))))
        properties.append(("LightHue", str(hue)))
        properties.append(("LightSaturation", str(saturation)))
        if ue2_class != "Sunlight":
            radius = comp.get("Radius", DEFAULT_RADIUS) * scale * radius_scale
            properties.append(("LightRadius", "%.6f" % light_radius_to_ue2(radius)))
        if ue2_class == "Spotlight":
            outer = comp.get("OuterConeAngle", DEFAULT_OUTER_CONE)
            properties.append(("LightCone", str(cone_angle_to_ue2(outer))))

        out.append(Actor(ue2_class, _unique_name(export.name, out), properties))
        stats.converted += 1
        stats.by_class[ue2_class] = stats.by_class.get(ue2_class, 0) + 1
    # Every SkyLight has now been summed, so the floor can be applied once.
    if stats.ambient is not None and ambient_gain > 0:
        brightness, hue, saturation = stats.ambient
        if brightness < MIN_SKYLIGHT_AMBIENT:
            stats.ambient_floored = brightness
            stats.ambient = (MIN_SKYLIGHT_AMBIENT, hue, saturation)
    return out, stats


def _unique_name(name, existing):
    import re

    base = re.sub(r"[^A-Za-z0-9_]", "_", name) or "Light"
    if base[0].isdigit():
        base = "_" + base
    taken = {a.name for a in existing}
    if base not in taken:
        return base
    n = 2
    while "%s_%d" % (base, n) in taken:
        n += 1
    return "%s_%d" % (base, n)
