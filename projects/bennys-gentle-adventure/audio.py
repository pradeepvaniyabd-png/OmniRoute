#!/usr/bin/env python3
"""Benny's Gentle Adventure — narration, music and mix.

Produces in $OUT:
  timeline.json   scene + cue timings consumed by the visual renderer
  mix.wav         48 kHz stereo narration + music (ducked), pre-loudnorm
  subtitles.srt   YouTube-ready captions
"""
import json
import os
import subprocess
import sys
import tempfile

import numpy as np
import soundfile as sf

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.environ.get("OUT", os.path.join(HERE, "out"))
MODELS = os.environ.get("MODELS", os.path.join(HERE, "models"))
SR = 48000

# speaker -> (kokoro voice, speed, pitch factor)
VOICES = {
    "N": ("af_heart", 0.86, 1.00),  # warm narrator
    "B": ("am_puck", 0.92, 1.16),  # Benny: friendly, slightly higher = cub-like
    "R": ("af_sky", 0.95, 1.30),  # little bunny: tiny and sweet
    "M": ("af_kore", 0.92, 1.10),  # mama bunny
}

# Scene list: (scene id, lead-in seconds, tail seconds, lines)
# line: (speaker, text, cue id or None, gap after seconds)
SCRIPT = [
    ("title", 1.2, 2.0, [("N", "Benny's Gentle Adventure.", "title", 0)]),
    ("tree_ext", 1.0, 1.0, [
        ("N", "Once upon a time, high up in a big, green tree, there was a cozy little treehouse.", None, 0.6),
        ("N", "And in that cozy little treehouse lived a friendly little bear, named Benny.", "intro_benny", 0),
    ]),
    ("tree_int", 1.0, 1.4, [
        ("N", "One bright morning, the warm sun peeked through the window, and gently said hello.", "sunbeam", 0.5),
        ("N", "Benny opened his eyes, and gave a great, big stretch.", "wake", 0.6),
        ("B", "Good morning, sunshine! What a beautiful day!", "gm", 0.6),
        ("N", "Benny decided to go for a gentle walk, in the meadow.", "decide", 0),
    ]),
    ("meadow", 1.2, 0.8, [
        ("N", "The meadow was full of colorful flowers, swaying softly in the breeze.", None, 0.6),
        ("N", "Look! Here come some friendly butterflies. Let's count them together!", "bf_intro", 0.8),
        ("N", "One.", "bf1", 0.9),
        ("N", "Two.", "bf2", 0.9),
        ("N", "Three.", "bf3", 0.9),
        ("N", "Four.", "bf4", 0.9),
        ("N", "Five!", "bf5", 0.8),
        ("N", "Five friendly butterflies! Great counting!", "bf_done", 0.8),
        ("N", "Benny found a pretty pink flower. He took a slow, deep breath.", "smell", 0.5),
        ("B", "Mmm! It smells so sweet!", "sweet", 0),
    ]),
    ("bunny_meet", 1.2, 1.0, [
        ("N", "Just then, Benny saw a little bunny, looking this way, and that way.", "see_bunny", 0.5),
        ("B", "Hello, little bunny! Are you looking for your family?", "hello", 0.4),
        ("R", "Yes! I hopped a little too far. Can you help me find my way home?", "ask", 0.4),
        ("B", "Of course! Friends help each other. Would you like a strawberry?", "offer", 0.5),
        ("N", "Benny shared his yummy strawberries, and the little bunny smiled.", "share", 0.4),
        ("R", "Yummy! Thank you, Benny!", "thanks", 0),
    ]),
    ("bunny_home", 1.0, 1.2, [
        ("N", "Together, they followed the little path, past the daisies and the sunflowers.", "walk", 0.5),
        ("R", "Look! There's my family, under the big oak tree!", "family", 0.4),
        ("N", "The bunny family gave the little bunny a big, warm hug.", "hug", 0.4),
        ("M", "Thank you for helping, Benny! You are a kind friend.", "mama", 0.5),
        ("N", "Helping a friend made Benny's heart feel warm and happy.", "warm", 0),
    ]),
    ("sunset_ext", 1.0, 1.0, [
        ("N", "As the sun began to set, the sky turned pink, and orange, and gold.", None, 0.5),
        ("N", "Benny walked home to his cozy treehouse, humming a happy tune.", "home", 0),
    ]),
    ("sunset_int", 1.0, 1.4, [
        ("N", "He sat by his window, and watched the first little stars twinkle.", "stars", 0.6),
        ("B", "What a wonderful day. Being kind makes everyone smile.", "reflect", 0),
    ]),
    ("ending", 1.0, 4.5, [
        ("B", "Keep smiling, keep learning, and remember to be kind!", "end1", 0.5),
        ("B", "See you next time!", "end2", 0),
    ]),
]

XFADE = 1.2  # visual crossfade between scenes (overlaps scene boundaries)


def run(cmd):
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def synth_lines():
    from kokoro_onnx import Kokoro

    k = Kokoro(os.path.join(MODELS, "kokoro.onnx"), os.path.join(MODELS, "voices.bin"))
    clips = {}
    tmp = tempfile.mkdtemp(prefix="benny_vo_")
    n = 0
    for scene, _lead, _tail, lines in SCRIPT:
        for i, (spk, text, _cue, _gap) in enumerate(lines):
            voice, speed, pitch = VOICES[spk]
            samples, sr = k.create(text, voice=voice, speed=speed, lang="en-us")
            raw = os.path.join(tmp, f"{n:03d}_raw.wav")
            proc = os.path.join(tmp, f"{n:03d}.wav")
            sf.write(raw, samples, sr)
            # pitch shift (keep duration), gentle EQ + light compression, resample to 48k
            af = []
            if abs(pitch - 1.0) > 1e-3:
                af += [f"asetrate={int(sr * pitch)}", f"aresample={sr}", f"atempo={1 / pitch:.5f}"]
            af += [
                "highpass=f=90",
                "equalizer=f=3000:t=q:w=1.2:g=2",
                "acompressor=threshold=-20dB:ratio=2.5:attack=8:release=120",
                f"aresample={SR}",
                "silenceremove=start_periods=1:start_threshold=-50dB:stop_periods=-1:stop_threshold=-50dB:stop_duration=0.25",
            ]
            run(["ffmpeg", "-y", "-i", raw, "-af", ",".join(af), "-ac", "1", proc])
            audio, _ = sf.read(proc, dtype="float32")
            # 15 ms fades to avoid clicks
            f = int(0.015 * SR)
            if len(audio) > 2 * f:
                audio[:f] *= np.linspace(0, 1, f)
                audio[-f:] *= np.linspace(1, 0, f)
            clips[(scene, i)] = audio
            n += 1
            print(f"  [{spk}] {text}", file=sys.stderr)
    return clips


def build_timeline(clips):
    t = 0.0
    scenes, cues, subs, spoken = [], {}, [], []
    vo_events = []
    for scene, lead, tail, lines in SCRIPT:
        start = t
        t += lead
        for i, (spk, text, cue, gap) in enumerate(lines):
            d = len(clips[(scene, i)]) / SR
            vo_events.append((t, clips[(scene, i)]))
            subs.append((t, t + d, text))
            spoken.append({"spk": spk, "start": round(t, 3), "end": round(t + d, 3)})
            if cue:
                cues[cue] = {"start": round(t, 3), "end": round(t + d, 3)}
            t += d + gap
        t += tail
        scenes.append({"id": scene, "start": round(start, 3), "end": round(t, 3)})
    total = t
    timeline = {
        "scenes": scenes,
        "cues": cues,
        "lines": spoken,
        "total": round(total, 3),
        "xfade": XFADE,
    }
    return timeline, vo_events, subs


# ---------------------------------------------------------------- music ----
def note_hz(semis_from_a4):
    return 440.0 * 2 ** (semis_from_a4 / 12)


def NOTE(name):
    names = {"C": -9, "D": -7, "E": -5, "F": -4, "G": -2, "A": 0, "B": 2, "Bb": 1}
    letter, octave = name[:-1], int(name[-1])
    return note_hz(names[letter] + (octave - 4) * 12)


def pad_tone(freq, dur, amp):
    n = int(dur * SR)
    t = np.arange(n) / SR
    vib = 1 + 0.002 * np.sin(2 * np.pi * 4.5 * t)
    ph = 2 * np.pi * freq * np.cumsum(vib) / SR
    w = np.sin(ph) + 0.25 * np.sin(2 * ph) + 0.08 * np.sin(3 * ph)
    a = int(0.8 * SR)
    r = int(1.0 * SR)
    env = np.ones(n)
    env[:a] = np.linspace(0, 1, a) ** 2
    env[-r:] *= np.linspace(1, 0, r) ** 2
    return (amp * w * env).astype(np.float32)


def bell_tone(freq, amp, dur=2.2):
    n = int(dur * SR)
    t = np.arange(n) / SR
    w = (
        np.sin(2 * np.pi * freq * t) * np.exp(-t * 2.2)
        + 0.35 * np.sin(2 * np.pi * freq * 2.0 * t) * np.exp(-t * 4.0)
        + 0.12 * np.sin(2 * np.pi * freq * 3.01 * t) * np.exp(-t * 7.0)
    )
    att = int(0.004 * SR)
    w[:att] *= np.linspace(0, 1, att)
    return (amp * w).astype(np.float32)


def make_music(total):
    """Soft music-box lullaby in F major: F - C - Dm - Bb, 76 BPM."""
    rng = np.random.default_rng(7)
    beat = 60 / 76
    bar = 4 * beat
    n = int((total + 3) * SR)
    L = np.zeros(n, np.float32)
    R = np.zeros(n, np.float32)
    chords = [
        (["F3", "A3", "C4"], ["F4", "A4", "C5", "F5"]),
        (["C3", "E3", "G3"], ["E4", "G4", "C5", "E5"]),
        (["D3", "F3", "A3"], ["D4", "F4", "A4", "D5"]),
        (["Bb2", "D3", "F3"], ["D4", "F4", "Bb4", "D5"]),
    ]
    # gentle written melody motif (scale degrees per bar), repeats with variation
    motifs = [
        [2, None, 1, 0, None, 1, 2, None],
        [1, None, 2, 3, None, 2, 1, None],
        [0, None, 1, 2, None, 3, 2, None],
        [1, None, 0, 1, None, None, 0, None],
    ]
    b = 0
    t0 = 0.0
    while t0 < total + 1:
        bass, mel = chords[b % 4]
        for i, nm in enumerate(bass):
            tone = pad_tone(NOTE(nm), bar + 1.2, 0.045)
            s = int(t0 * SR)
            e = min(n, s + len(tone))
            pan = 0.35 + 0.15 * i
            L[s:e] += tone[: e - s] * (1 - pan)
            R[s:e] += tone[: e - s] * pan
        motif = motifs[(b // 4 + b) % 4] if (b // 4) % 2 else motifs[b % 4]
        for k, deg in enumerate(motif):
            if deg is None:
                continue
            if rng.random() < 0.12:
                continue
            f = NOTE(mel[deg])
            tone = bell_tone(f, 0.06 + 0.02 * rng.random())
            s = int((t0 + k * beat / 2) * SR)
            e = min(n, s + len(tone))
            if s >= n:
                continue
            pan = 0.3 + 0.4 * rng.random()
            L[s:e] += tone[: e - s] * (1 - pan)
            R[s:e] += tone[: e - s] * pan
        t0 += bar
        b += 1
    # soft feedback echo for space
    d = int(0.33 * SR)
    for _ in range(3):
        L[d:] += 0.25 * R[:-d]
        R[d:] += 0.25 * L[:-d]
    m = np.stack([L, R], 1)
    m /= np.max(np.abs(m)) + 1e-9
    # fade in/out
    fi, fo = int(2.5 * SR), int(4.0 * SR)
    m[:fi] *= np.linspace(0, 1, fi)[:, None]
    end = int(total * SR)
    m = m[:end]
    m[-fo:] *= np.linspace(1, 0, fo)[:, None] ** 1.5
    return m


def chime(amp=0.12):
    """Sparkly transition chime (ascending F major arpeggio)."""
    notes = ["F5", "A5", "C6", "F6"]
    out = np.zeros(int(2.8 * SR), np.float32)
    for i, nm in enumerate(notes):
        tn = bell_tone(NOTE(nm), amp * (0.9 - 0.12 * i), 2.0)
        s = int(i * 0.09 * SR)
        out[s : s + len(tn)] += tn[: len(out) - s]
    return out


def main():
    os.makedirs(OUT, exist_ok=True)
    print("Synthesizing narration...", file=sys.stderr)
    clips = synth_lines()
    timeline, vo_events, subs = build_timeline(clips)
    total = timeline["total"]
    n = int(total * SR)

    vo = np.zeros(n, np.float32)
    for t, a in vo_events:
        s = int(t * SR)
        vo[s : s + len(a)] += a[: n - s]
    vo /= np.max(np.abs(vo)) + 1e-9
    vo *= 0.85

    print("Composing music...", file=sys.stderr)
    music = make_music(total)
    music = music[:n] if len(music) >= n else np.pad(music, ((0, n - len(music)), (0, 0)))

    # duck music under voice: smooth envelope of voice activity
    act = (np.abs(vo) > 0.02).astype(np.float32)
    win = int(0.35 * SR)
    kern = np.ones(win, np.float32) / win
    act = np.convolve(act, kern, mode="same")
    act = np.clip(act * 3, 0, 1)
    # smooth again for slow release
    act = np.convolve(act, np.ones(int(0.6 * SR), np.float32) / int(0.6 * SR), mode="same")
    music_gain = 0.20 - 0.11 * act  # ~ -14 dB when idle, ~ -19 dB under voice
    mix = music * music_gain[:, None]

    # transition chimes at scene boundaries (soft)
    ch = chime()
    for sc in timeline["scenes"][1:]:
        s = int((sc["start"] - 0.3) * SR)
        e = min(n, s + len(ch))
        mix[s:e, 0] += ch[: e - s] * 0.5
        mix[s:e, 1] += ch[: e - s] * 0.5

    mix += vo[:, None]
    mix /= max(1.0, np.max(np.abs(mix)) / 0.95)
    sf.write(os.path.join(OUT, "mix.wav"), mix, SR, subtype="PCM_24")

    with open(os.path.join(OUT, "timeline.json"), "w") as fh:
        json.dump(timeline, fh, indent=2)

    def ts(x):
        h, r = divmod(x, 3600)
        m, s = divmod(r, 60)
        return f"{int(h):02d}:{int(m):02d}:{int(s):02d},{int((s % 1) * 1000):03d}"

    with open(os.path.join(OUT, "subtitles.srt"), "w") as fh:
        for i, (a, b, text) in enumerate(subs, 1):
            fh.write(f"{i}\n{ts(a)} --> {ts(b + 0.3)}\n{text}\n\n")
    print(f"Total duration: {total:.1f}s", file=sys.stderr)


if __name__ == "__main__":
    main()
