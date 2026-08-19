"""Reverb conversion: UE3 ReverbVolume -> a UT2004 PhysicsVolume carrying an
I3DL2 room effect.

This converts further than it looks like it should, because both engines speak
the same standard. UE3's `ReverbPreset` enum is the I3DL2 preset list by
another name, and UT2004's `I3DL2Listener` (Engine/Classes/I3DL2Listener.uc)
is the I3DL2 parameter set itself -- so a UT3 ReverbVolume is not approximated
here, it is restated.

Three facts make it work:

* **No zoning is needed.** A converted map is one ZoneInfo with no portals, so
  anything keyed to zones would be hopeless. Reverb is not: a volume overrides
  its zone outright, at Engine/Src/UnAudio.cpp:139 --

      UI3DL2Listener* EAXEffect = Region.Zone->ZoneEffect;
      if( Volume && Volume->VolumeEffect )
          EAXEffect = Volume->VolumeEffect;

* **A concrete class exists to instantiate.** `I3DL2Listener` is abstract, but
  `EFFECT_WaterVolume` extends it, is `editinlinenew`, and adds nothing of its
  own -- it is the stock class with the water preset in its defaults, and every
  parameter is `var()`, so setting them all leaves nothing of the water behind.
  That avoids generating a class into the map's package, and `VolumeEffect` is
  `editinline` (PhysicsVolume.uc:22), which in a t3d means the `Begin Object`
  form the jump pads already use.

* **The preset values are on disk, not guessed.** `I3DL2_ENVIRONMENT_PRESET_*`
  in DirectX9/Include/dsound.h carries all 30 presets in exactly these twelve
  parameters, and every one of UE3's 23 enum names has an entry.

The volume becomes a PhysicsVolume rather than a plain Volume because
`VolumeEffect` lives on PhysicsVolume. That is inert here: its defaults are the
level's own (Gravity -950 matching LevelInfo.DefaultGravity, GroundFriction 8,
FluidFriction 0.3, TerminalVelocity 2500), so standing in one changes nothing
but the sound.

What does not convert: `FadeTime`, since UE2 switches room effects on the
volume boundary with no crossfade, and I3DL2's Density, which has no field on
I3DL2Listener. EAX3-only parameters (RoomLF, DecayLFRatio, echo and modulation,
air absorption) are left at I3DL2Listener's defaults, which are the neutral
EAX GENERIC values -- the I3DL2 preset says nothing about them.
"""

import math

# I3DL2_ENVIRONMENT_PRESET_* from DirectX9/Include/dsound.h, in the order the
# DSFXI3DL2Reverb struct declares (dsound.h:1628):
#
#   lRoom, lRoomHF, flRoomRolloffFactor, flDecayTime, flDecayHFRatio,
#   lReflections, flReflectionsDelay, lReverb, flReverbDelay,
#   flDiffusion, flDensity, flHFReference
PRESETS = {
    "DEFAULT": (-1000, -100, 0.0, 1.49, 0.83, -2602, 0.007, 200, 0.011, 100.0, 100.0, 5000.0),
    "GENERIC": (-1000, -100, 0.0, 1.49, 0.83, -2602, 0.007, 200, 0.011, 100.0, 100.0, 5000.0),
    "PADDEDCELL": (-1000, -6000, 0.0, 0.17, 0.10, -1204, 0.001, 207, 0.002, 100.0, 100.0, 5000.0),
    "ROOM": (-1000, -454, 0.0, 0.40, 0.83, -1646, 0.002, 53, 0.003, 100.0, 100.0, 5000.0),
    "BATHROOM": (-1000, -1200, 0.0, 1.49, 0.54, -370, 0.007, 1030, 0.011, 100.0, 60.0, 5000.0),
    "LIVINGROOM": (-1000, -6000, 0.0, 0.50, 0.10, -1376, 0.003, -1104, 0.004, 100.0, 100.0, 5000.0),
    "STONEROOM": (-1000, -300, 0.0, 2.31, 0.64, -711, 0.012, 83, 0.017, 100.0, 100.0, 5000.0),
    "AUDITORIUM": (-1000, -476, 0.0, 4.32, 0.59, -789, 0.020, -289, 0.030, 100.0, 100.0, 5000.0),
    "CONCERTHALL": (-1000, -500, 0.0, 3.92, 0.70, -1230, 0.020, -2, 0.029, 100.0, 100.0, 5000.0),
    "CAVE": (-1000, 0, 0.0, 2.91, 1.30, -602, 0.015, -302, 0.022, 100.0, 100.0, 5000.0),
    "ARENA": (-1000, -698, 0.0, 7.24, 0.33, -1166, 0.020, 16, 0.030, 100.0, 100.0, 5000.0),
    "HANGAR": (-1000, -1000, 0.0, 10.05, 0.23, -602, 0.020, 198, 0.030, 100.0, 100.0, 5000.0),
    "CARPETEDHALLWAY": (-1000, -4000, 0.0, 0.30, 0.10, -1831, 0.002, -1630, 0.030, 100.0, 100.0, 5000.0),
    "HALLWAY": (-1000, -300, 0.0, 1.49, 0.59, -1219, 0.007, 441, 0.011, 100.0, 100.0, 5000.0),
    "STONECORRIDOR": (-1000, -237, 0.0, 2.70, 0.79, -1214, 0.013, 395, 0.020, 100.0, 100.0, 5000.0),
    "ALLEY": (-1000, -270, 0.0, 1.49, 0.86, -1204, 0.007, -4, 0.011, 100.0, 100.0, 5000.0),
    "FOREST": (-1000, -3300, 0.0, 1.49, 0.54, -2560, 0.162, -613, 0.088, 79.0, 100.0, 5000.0),
    "CITY": (-1000, -800, 0.0, 1.49, 0.67, -2273, 0.007, -2217, 0.011, 50.0, 100.0, 5000.0),
    "MOUNTAINS": (-1000, -2500, 0.0, 1.49, 0.21, -2780, 0.300, -2014, 0.100, 27.0, 100.0, 5000.0),
    "QUARRY": (-1000, -1000, 0.0, 1.49, 0.83, -10000, 0.061, 500, 0.025, 100.0, 100.0, 5000.0),
    "PLAIN": (-1000, -2000, 0.0, 1.49, 0.50, -2466, 0.179, -2514, 0.100, 21.0, 100.0, 5000.0),
    "PARKINGLOT": (-1000, 0, 0.0, 1.65, 1.50, -1363, 0.008, -1153, 0.012, 100.0, 100.0, 5000.0),
    "SEWERPIPE": (-1000, -1000, 0.0, 2.81, 0.14, 429, 0.014, 648, 0.021, 80.0, 60.0, 5000.0),
    "UNDERWATER": (-1000, -4000, 0.0, 1.49, 0.10, -449, 0.007, 1700, 0.011, 100.0, 100.0, 5000.0),
    "SMALLROOM": (-1000, -600, 0.0, 1.10, 0.83, -400, 0.005, 500, 0.010, 100.0, 100.0, 5000.0),
    "MEDIUMROOM": (-1000, -600, 0.0, 1.30, 0.83, -1000, 0.010, -200, 0.020, 100.0, 100.0, 5000.0),
    "LARGEROOM": (-1000, -600, 0.0, 1.50, 0.83, -1600, 0.020, -1000, 0.040, 100.0, 100.0, 5000.0),
    "MEDIUMHALL": (-1000, -600, 0.0, 1.80, 0.70, -1300, 0.015, -800, 0.030, 100.0, 100.0, 5000.0),
    "LARGEHALL": (-1000, -600, 0.0, 1.80, 0.70, -2000, 0.030, -1400, 0.060, 100.0, 100.0, 5000.0),
    "PLATE": (-1000, -200, 0.0, 1.30, 0.90, 0, 0.002, 0, 0.010, 100.0, 75.0, 5000.0),
}

# The stock concrete I3DL2Listener. Every field it sets is overridden below, so
# nothing of the water preset survives -- it is here purely because the base
# class is abstract and no other subclass ships.
EFFECT_CLASS = "EFFECT_WaterVolume"

# UE3 states a volume's wet level separately from its preset. I3DL2 has no
# master gain, but lRoom *is* the room effect level in millibels, so the two
# compose: a wet level of 0.25 is 2000*log10(0.25) = -1204mB of room.
ROOM_MIN = -10000
ROOM_MAX = 0


def preset_name(reverb_type):
    """UE3 `REVERB_StoneCorridor` -> the `STONECORRIDOR` preset key."""
    name = str(reverb_type or "")
    if name.upper().startswith("REVERB_"):
        name = name[len("REVERB_"):]
    return name.upper()


def wet_room(room, volume):
    """A preset's lRoom adjusted for UE3's wet level, in millibels."""
    if volume is None or volume >= 1.0:
        return int(room)
    if volume <= 0.0:
        return ROOM_MIN
    return int(max(ROOM_MIN, min(ROOM_MAX, round(room + 2000.0 * math.log10(volume)))))


def settings_of(props):
    """(preset key, wet level) from a ReverbVolume's Settings struct."""
    settings = props.get("Settings")
    values = getattr(settings, "value", None)
    if values is None:
        return "DEFAULT", None
    fields = {}
    try:
        for name, _index, _type, value in values:
            fields[name] = value
    except (TypeError, ValueError):
        return "DEFAULT", None
    # bApplyReverb=False is a volume the mapper switched off; UE3 keeps the
    # settings on it, so it has to be read rather than inferred from silence.
    if fields.get("bApplyReverb") is False:
        return None, None
    return preset_name(fields.get("ReverbType")), fields.get("Volume")


def effect_object(name, props):
    """The inline I3DL2Listener for a ReverbVolume, or None to leave it out.

    Returns (class, object name, [(property, value)]) -- the shape
    `ut2.t3d.Brush` writes as a `Begin Object` block.
    """
    key, volume = settings_of(props)
    if key is None:
        return None
    preset = PRESETS.get(key)
    if preset is None:
        return None
    (room, room_hf, rolloff, decay, decay_hf, reflections, reflections_delay,
     reverb, reverb_delay, diffusion, _density, hf_reference) = preset
    return (EFFECT_CLASS, "%sEffect" % name, [
        ("Room", "%d" % wet_room(room, volume)),
        ("RoomHF", "%d" % room_hf),
        ("RoomRolloffFactor", "%f" % rolloff),
        ("DecayTime", "%f" % decay),
        ("DecayHFRatio", "%f" % decay_hf),
        ("Reflections", "%d" % reflections),
        ("ReflectionsDelay", "%f" % reflections_delay),
        ("Reverb", "%d" % reverb),
        ("ReverbDelay", "%f" % reverb_delay),
        # I3DL2 states diffusion as a percentage; UE2's field is a fraction.
        ("EnvironmentDiffusion", "%f" % (diffusion / 100.0)),
        ("HFReference", "%f" % hf_reference),
    ])
