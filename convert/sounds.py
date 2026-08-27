"""Ambient sound conversion: UE3 AmbientSound* actors -> UT2004 AmbientSound.

UT3 states an ambient sound as an actor owning a SoundNodeAmbient, which names
one SoundNodeWave and gives radii, volume and pitch as distributions. UT2004
puts the same thing directly on the actor -- `AmbientSound`, `SoundRadius`,
`SoundVolume`, `SoundPitch` -- so the actors map across almost one for one. The
waves are the work: they are Ogg Vorbis inside the package and UT2004 stores
sounds as whole WAV files, so each one is decoded out to a mono WAV and pulled
back in by `#exec AUDIO IMPORT`.

Mono is not a preference. ALAudio refuses to build a buffer for a stereo USound
at all -- "Shouldn't use stereo sound", ALAudio/Src/ALAudioSubsystem.cpp:1892 --
and the sound is then silent, so the two stereo beds in DM-HeatRay have to be
folded down.

The radius mapping is the one real judgement call, since the two engines do not
share a falloff curve:

  UE3   ATTENUATION_Logarithmic, full volume inside MinRadius, silent at
        MaxRadius, logarithmic in between -- so half volume falls at the
        geometric mean of the two radii.
  UE2   OpenAL AL_INVERSE_DISTANCE_CLAMPED with rolloff 1
        (ALAudioSubsystem.cpp:383/609), i.e. gain = SoundRadius/distance,
        clamped to full volume inside SoundRadius -- so half volume falls at
        2*SoundRadius, and the engine stops the sound entirely at
        100*SoundRadius (GAudioMaxRadiusMultiplier, Core/Src/Core.cpp:179).

Matching the half-volume distance is what keeps a sound occupying the same part
of the map, so SoundRadius = sqrt(MinRadius*MaxRadius)/2. Half of DM-HeatRay's
ambients declare MinRadius 0 or 1, where the log curve is degenerate and that
formula collapses, so the ratio is capped at MAX_RADIUS_RATIO first.
"""

import os
import shutil
import subprocess
import re

from ut2.t3d import Actor, vec
from ut3.objects.level import ordered_exports
from ut3.objects.sound import distribution_range, distribution_value, read_sound_wave
from ut3.props import read_object_properties

_SANITIZE = re.compile(r"[^A-Za-z0-9_]")

AMBIENT_CLASSES = ("AmbientSoundSimple", "AmbientSoundSimpleToggleable",
                   "AmbientSoundNonLoop")

# Engine.u Default__SoundNodeAmbient: what a node means when it says nothing.
UT3_MIN_RADIUS = 400.0
UT3_MAX_RADIUS = 5000.0
UT3_VOLUME = 1.0
UT3_PITCH = 1.0
UT3_DELAY = 1.0

# UE3's logarithmic falloff has no meaning at MinRadius 0, and neither does the
# geometric mean. Treat a min:max ratio wider than this as if it were this.
MAX_RADIUS_RATIO = 20.0

# Actor.uc: SoundPitch is a byte where 64 is unity, and the engine clamps the
# resulting ratio to 0.5..2.0 (UnAudio.cpp:196).
UT2_PITCH_UNITY = 64
UT2_PITCH_MIN = 32
UT2_PITCH_MAX = 128

# What UE3 volume 1.0 becomes, and the byte ceiling it is clamped to.
#
# 255 was the obvious reading -- `GetAmbientVolume` divides by 255
# (UnActor.cpp:138), so full scale to full scale -- and it is too loud by about
# a factor of two. The line it divides by is
#
#     Attenuation * SoundVolume / 255.f / 2.f   // volume is now in range 0..2
#
# so the byte is not a fraction of unity at all; 255 is half of the engine's
# internal range, and what UT2004 itself calls a normal ambient is the 100 that
# `AmbientSound.uc` ships as its default. That is the reference used here: an
# AmbientSound placed in UnrealEd and left alone plays at 100, so UE3's 1.0
# lands there too. Reported on DM-Dekk, whose 112 ambients came out between 204
# and 229 -- twice what the engine's own class would have played.
#
# The ceiling stays 255, so `--sound-gain` above 1.0 still has somewhere to go.
DEFAULT_VOLUME_GAIN = 1.0
UT2_VOLUME_UNITY = 100
UT2_VOLUME_MAX = 255

FFMPEG = "ffmpeg"


def sanitize(name):
    out = _SANITIZE.sub("_", name or "")
    if out and out[0].isdigit():
        out = "_" + out
    return out or "Sound"


def sound_radius(min_radius, max_radius):
    """UT2004 SoundRadius matching a UE3 min/max pair at half volume."""
    if max_radius <= 0.0:
        return None
    min_radius = max(min_radius, max_radius / MAX_RADIUS_RATIO)
    return (min_radius * max_radius) ** 0.5 / 2.0


class SoundStats:
    def __init__(self):
        self.actors = 0
        self.waves = 0
        self.emitters = 0
        self.skipped_silent = 0
        self.skipped_no_wave = 0
        self.dropped_actors = 0
        self.attached_to_movers = 0
        self.downmixed = 0
        self.bytes = 0
        self.failed = []

    def __str__(self):
        out = "%d ambient sounds from %d waves (%.1f MB)" % (
            self.actors, self.waves, self.bytes / 1e6)
        if self.emitters:
            out += "; %d one-shot emitters" % self.emitters
        if self.attached_to_movers:
            out += "; %d riding a mover" % self.attached_to_movers
        if self.downmixed:
            out += "; %d stereo waves folded to mono" % self.downmixed
        if self.skipped_silent:
            out += "; %d not playing at level start" % self.skipped_silent
        if self.skipped_no_wave:
            out += "; %d actors with no wave" % self.skipped_no_wave
        if self.dropped_actors:
            out += "; %d actors dropped with an undecodable wave" % self.dropped_actors
        return out


class SoundSet:
    """Waves referenced by the level, de-duplicated across the actors using them."""

    def __init__(self, package_name, group="Ambient"):
        self.package_name = package_name
        self.group = group
        self.by_wave = {}    # (package path, export index) -> name
        self.waves = {}      # name -> (Package, export)
        self.failed = []

    def path(self, name):
        if not name:
            return None
        if self.group:
            return "%s.%s.%s" % (self.package_name, self.group, name)
        return "%s.%s" % (self.package_name, name)

    def reference(self, name):
        """The t3d property value for a sound in this package."""
        path = self.path(name)
        return "Sound'%s'" % path if path else None

    def _unique(self, base):
        name = sanitize(base)
        if name not in self.waves:
            return name
        n = 2
        while "%s_%d" % (name, n) in self.waves:
            n += 1
        return "%s_%d" % (name, n)

    def add(self, pkg, index, ref):
        """Register a SoundNodeWave reference; returns its name in the package."""
        if ref is None or ref.is_null:
            return None
        owner, export = index.resolve(pkg, ref)
        if export is None or owner.class_name_of(export) != "SoundNodeWave":
            return None
        key = (owner.path, export.index)
        if key in self.by_wave:
            return self.by_wave[key]
        name = self._unique(export.name)
        self.waves[name] = (owner, export)
        self.by_wave[key] = name
        return name

    def drop(self, name, why):
        self.waves.pop(name, None)
        for key, value in self.by_wave.items():
            if value == name:
                self.by_wave[key] = None
        self.failed.append((name, why))


def _ambient_node(pkg, index, props):
    """The SoundNodeAmbient (or ...NonLoop) an ambient actor hangs its settings on."""
    ref = props.get("AmbientProperties") or props.get("SoundNodeInstance")
    if ref is None or ref.is_null:
        return None, None
    owner, export = index.resolve(pkg, ref)
    if export is None:
        return None, None
    node, start, _end = read_object_properties(owner, export)
    if start is None:
        return None, None
    return owner, node


def _plain_range(node, low_key, high_key):
    """The mean of a plain min/max float pair, or None if it is not stated.

    UDK's `AmbientSoundSimple` states radius, volume and pitch as two plain
    floats under names of its own -- `RadiusMin`/`RadiusMax`,
    `VolumeMin`/`VolumeMax`, `PitchMin`/`PitchMax` -- where UT3 states each as
    one `RawDistributionFloat` called `MinRadius`, `VolumeModulation` and
    `PitchModulation`. Reading only UT3's names meant every TOXIKK ambient
    sound fell back to the defaults: volume 1.0, so all 112 of BL-Dekk's came
    out at SoundVolume 255, and radius sqrt(400*5000)/2 = 707 against a real
    261. Loud, and audible from nearly three times too far.

    The mean of the pair is the same reading `distribution_value` takes of a
    uniform distribution, so the two paths agree about what a range means.
    """
    low, high = node.get(low_key), node.get(high_key)
    if low is None and high is None:
        return None
    try:
        if low is None:
            return float(high)
        if high is None:
            return float(low)
        return (float(low) + float(high)) / 2.0
    except (TypeError, ValueError):
        return None


def _slot_scales(node):
    """(volume scale, pitch scale) averaged over an ambient's sound slots.

    Each `SoundSlot` carries a `VolumeScale` and `PitchScale` that multiply the
    actor's own range, and a `Weight` giving its share of the random draw. Most
    ambients have one slot, so this is usually just that slot's numbers -- but
    ignoring them made every TOXIKK sound louder than authored, the first of
    BL-Dekk's being 0.55 in the actor and 0.5 in the slot for a real 0.275.
    """
    slots = node.get("SoundSlots")
    if slots is None or not len(slots):
        return 1.0, 1.0
    volume = pitch = weight_total = 0.0
    for slot in slots.as_props():
        try:
            weight = float(slot.get("Weight", 1.0))
        except (TypeError, ValueError):
            weight = 1.0
        if weight <= 0.0:
            continue
        def _scale(key):
            try:
                return float(slot.get(key, 1.0))
            except (TypeError, ValueError):
                return 1.0
        volume += _scale("VolumeScale") * weight
        pitch += _scale("PitchScale") * weight
        weight_total += weight
    if weight_total <= 0.0:
        return 1.0, 1.0
    return volume / weight_total, pitch / weight_total


def _levels(owner, index, node, volume_gain):
    """(SoundRadius, SoundVolume, SoundPitch) for a UT2004 actor."""
    min_radius = _plain_range(node, "RadiusMin", "RadiusMin")
    max_radius = _plain_range(node, "RadiusMax", "RadiusMax")
    if min_radius is None or max_radius is None:
        min_radius = distribution_value(owner, index, node.get("MinRadius"),
                                        UT3_MIN_RADIUS)
        max_radius = distribution_value(owner, index, node.get("MaxRadius"),
                                        UT3_MAX_RADIUS)
    volume = _plain_range(node, "VolumeMin", "VolumeMax")
    if volume is None:
        volume = distribution_value(owner, index, node.get("VolumeModulation"),
                                    UT3_VOLUME)
    pitch = _plain_range(node, "PitchMin", "PitchMax")
    if pitch is None:
        pitch = distribution_value(owner, index, node.get("PitchModulation"),
                                   UT3_PITCH)

    volume_scale, pitch_scale = _slot_scales(node)
    volume *= volume_scale
    pitch *= pitch_scale

    radius = sound_radius(min_radius, max_radius)
    sound_volume = max(1, min(UT2_VOLUME_MAX,
                              int(round(UT2_VOLUME_UNITY * volume * volume_gain))))
    sound_pitch = max(UT2_PITCH_MIN, min(UT2_PITCH_MAX,
                                         int(round(UT2_PITCH_UNITY * pitch))))
    return radius, sound_volume, sound_pitch


def _emitters(owner, index, node, sound_set):
    """SoundEmitter entries reproducing a SoundNodeAmbientNonLoop's slots.

    UE3 picks one slot at random every DelayTime; UT2004 runs every emitter on
    its own independent schedule (UnActor.cpp:71). Stretching each interval by
    the slot count gives the same number of sounds per second overall, with the
    slot order randomised by the drift between them rather than by a draw.
    """
    slots = node.get("SoundSlots")
    if slots is None or not len(slots):
        return []
    low, high = distribution_range(owner, index, node.get("DelayTime"), UT3_DELAY)
    try:
        entries = slots.as_props()
    except (ValueError, IndexError):
        return []

    waves = []
    for slot in entries:
        name = sound_set.add(owner, index, slot.get("Wave"))
        if name:
            waves.append(name)
    if not waves:
        return []

    interval = max(0.1, (low + high) / 2.0 * len(waves))
    variance = min(interval * 0.99, abs(high - low) / 2.0 * len(waves))
    return [(name, interval, variance) for name in waves]


def convert_ambient_sounds(pkg, index, sound_set, scale=1.0,
                           volume_gain=DEFAULT_VOLUME_GAIN, stats=None):
    """Collect ambient sound actors; returns (actors, stats).

    Each emitted actor carries a `sound_names` list so `drop_failed` can remove
    the ones whose wave turns out not to decode.
    """
    stats = stats or SoundStats()
    out = []
    names = set()
    for export in ordered_exports(pkg, AMBIENT_CLASSES):
        source_class = pkg.class_name_of(export)
        props, start, _end = read_object_properties(pkg, export)
        if start is None:
            continue
        # A toggleable is switched on by Kismet, which does not convert. The
        # map says which ones start playing anyway: DM-HeatRay's five machine
        # hums set bAutoPlay, its alarm horn does not.
        if source_class == "AmbientSoundSimpleToggleable" and not props.get("bAutoPlay"):
            stats.skipped_silent += 1
            continue
        owner, node = _ambient_node(pkg, index, props)
        if node is None:
            stats.skipped_no_wave += 1
            continue

        radius, volume, pitch = _levels(owner, index, node, volume_gain)
        properties = []
        location = props.get("Location")
        if location is not None and location.value:
            properties.append(("Location", vec([c * scale for c in location.value])))

        sound_names = []
        emitters = _emitters(owner, index, node, sound_set)
        if emitters:
            for i, (wave_name, interval, variance) in enumerate(emitters):
                properties.append((
                    "SoundEmitters(%d)" % i,
                    "(EmitInterval=%f,EmitVariance=%f,EmitSound=%s)"
                    % (interval, variance, sound_set.reference(wave_name))))
                sound_names.append(wave_name)
            stats.emitters += len(emitters)
        else:
            wave_name = sound_set.add(owner, index, node.get("Wave"))
            if wave_name is None:
                stats.skipped_no_wave += 1
                continue
            properties.append(("AmbientSound", sound_set.reference(wave_name)))
            sound_names.append(wave_name)

        if radius is not None:
            properties.append(("SoundRadius", "%f" % (radius * scale)))
        properties.append(("SoundVolume", str(volume)))
        properties.append(("SoundPitch", str(pitch)))

        name = sanitize(export.name)
        if name in names:
            n = 2
            while "%s_%d" % (name, n) in names:
                n += 1
            name = "%s_%d" % (name, n)
        names.add(name)
        actor = Actor("AmbientSound", name, properties)
        actor.sound_names = sound_names
        # What UT3 hung this sound off, so a mover can absorb it later.
        base = props.get("Base")
        actor.base_name = (base.export.name
                           if base is not None and not base.is_null and base.is_export
                           else None)
        out.append(actor)
        stats.actors += 1
    return out, stats


def drop_failed(actors, sound_set, stats):
    """Remove actors whose wave could not be written, so nothing dangles."""
    lost = {name for name, _why in sound_set.failed}
    if not lost:
        return actors
    kept = []
    for actor in actors:
        remaining = [n for n in getattr(actor, "sound_names", []) if n not in lost]
        if not remaining:
            stats.dropped_actors += 1
            stats.actors -= 1
            continue
        if len(remaining) != len(actor.sound_names):
            actor.properties = [(k, v) for k, v in actor.properties
                                if not any("%s'" % sound_set.path(n) in v
                                           for n in lost)]
        kept.append(actor)
    return kept


def _decode(ogg_path, wav_path):
    """Ogg -> mono 16-bit WAV. Returns None on success, else why it failed."""
    # -map_metadata -1 keeps the encoder version out of the LIST chunk, so the
    # same map converts to the same bytes on a different machine.
    result = subprocess.run(
        [FFMPEG, "-y", "-v", "error", "-i", ogg_path, "-map_metadata", "-1",
         "-ac", "1", "-c:a", "pcm_s16le", wav_path],
        capture_output=True, text=True)
    if result.returncode != 0:
        return (result.stderr.strip().splitlines() or ["ffmpeg failed"])[-1][:80]
    if not os.path.exists(wav_path) or os.path.getsize(wav_path) <= 44:
        return "ffmpeg produced no audio"
    return None


def export_sounds(sound_set, out_dir, index=None, group=None, stats=None):
    """Decode every registered wave to WAV; returns the #exec lines for the package."""
    stats = stats or SoundStats()
    group = group or sound_set.group
    package = sound_set.package_name
    sounds_dir = os.path.join(out_dir, package, "Sounds")
    if not sound_set.waves:
        return [], stats
    os.makedirs(sounds_dir, exist_ok=True)

    if shutil.which(FFMPEG) is None:
        for name in list(sound_set.waves):
            sound_set.drop(name, "ffmpeg not installed")
        stats.failed = list(sound_set.failed)
        return [], stats

    lines = []
    for name in sorted(list(sound_set.waves)):
        owner, export = sound_set.waves[name]
        wave = read_sound_wave(owner, export, index)
        if wave is None or not wave.ogg:
            sound_set.drop(name, "no Ogg payload")
            continue
        ogg_path = os.path.join(sounds_dir, "%s.ogg" % name)
        wav_path = os.path.join(sounds_dir, "%s.wav" % name)
        with open(ogg_path, "wb") as f:
            f.write(wave.ogg)
        why = _decode(ogg_path, wav_path)
        os.remove(ogg_path)
        if why:
            sound_set.drop(name, why)
            continue
        if wave.channels > 1:
            stats.downmixed += 1
        lines.append('#exec AUDIO IMPORT FILE="Sounds\\%s" NAME="%s" GROUP="%s"'
                     % (os.path.basename(wav_path), name, group))
        stats.waves += 1
        stats.bytes += os.path.getsize(wav_path)
    stats.failed = list(sound_set.failed)
    return lines, stats
