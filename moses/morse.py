#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = [
#   "pygame>=2.5",
#   "numpy>=1.24",
#   "imageio-ffmpeg>=0.4",
#   "windows-curses; sys_platform == 'win32'",
# ]
# ///
"""
morse — a Morse-code "cassette" deck that lives in your terminal.

Keyboard only, no mouse, no window. Two things:

  • Select Cassette — browse to an audio file (mp3/wav/…), then watch it play
    back as a scrolling tape of tones and pauses. No letters: short block = dot,
    long block = dash, dark = pause. You translate it yourself. Press  s  to
    slow the playback down (pitch kept) when a cassette runs too fast to read.

  • Record Cassette — key Morse by hand ( . and - ), pauses included, hear every
    beep, then type a path and save it as an mp3 or wav.

Press  c  in either player to toggle the translation cheatsheet.

Run:   uv run morse.py        (or: python morse.py  with deps installed)
"""

import os
import sys
import json
import time
import wave
import locale
import curses
import subprocess
import tempfile
import threading

# Keep pygame silent and headless — we only use its audio mixer, never a window.
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

import numpy as np
import pygame
import imageio_ffmpeg

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()

HERE = os.path.dirname(os.path.abspath(__file__))
CHEATSHEET = os.path.join(HERE, "cheatsheet.txt")
SETTINGS = os.environ.get("MORSE_SETTINGS") or os.path.join(HERE, "settings.json")

AUDIO_EXTS = {".mp3", ".wav", ".flac", ".ogg", ".m4a", ".aac", ".opus", ".wma"}


# ───────────────────────────── tuning ──────────────────────────────────────

RATE = 44100                # audio sample rate
TONE_FREQ = 620             # default beep pitch (Hz)
DOT, DASH = 0.09, 0.27      # default element lengths (s) — dash is 3× a dot
TRACK_HZ = 100              # resolution of the on/off tape (samples per second)
CPS = 22                    # Select tape scroll speed (columns per second)
CPU = 2                     # Record tape: columns per unit (dot=2, dash=6)

# Select playback speeds — a tempo slow-down (pitch preserved) for reading a
# fast cassette. (label, factor); 1.0 = the cassette's own speed.
SEL_SPEEDS = [("1x", 1.0), ("0.75x", 0.75), ("0.5x", 0.5), ("0.35x", 0.35)]

# recording style presets (the "sound" of the cassette you make)
PITCHES = [300, 400, 500, 600, 700, 800, 1000, 1200]      # Hz
WAVES = ["sine", "triangle", "square", "sawtooth"]        # timbre
SPEEDS = [("v.slow", 0.16), ("slow", 0.12), ("normal", 0.08),
          ("fast", 0.06), ("v.fast", 0.04)]               # (label, dot seconds)
SHAPES = [("hard", 0.002, 0.004), ("normal", 0.006, 0.02),
          ("soft", 0.012, 0.07), ("long", 0.012, 0.16)]   # (label, attack, release)


# ─────────────────────────── audio helpers ─────────────────────────────────

def make_tone(dur, freq=TONE_FREQ, wave="sine", amp=0.5,
              attack=0.006, release=0.02, rate=RATE):
    """A single beep of the given pitch/timbre.

    `attack` is the fade-in (a short one keeps the onset crisp); `release` is a
    decay tail appended *after* the tone — short = hard/choppy, long = a soft
    ring-out ("Nachklang")."""
    n = max(1, int(dur * rate))
    rel = max(1, int(release * rate))
    total = n + rel
    ph = 2 * np.pi * freq * np.arange(total) / rate
    if wave == "square":
        w = np.sign(np.sin(ph)) * 0.7        # softer, it's harmonically rich
    elif wave == "triangle":
        w = 2 / np.pi * np.arcsin(np.sin(ph))
    elif wave == "sawtooth":
        w = (2 * (ph / (2 * np.pi) % 1.0) - 1) * 0.7
    else:
        w = np.sin(ph)
    w = w * amp
    env = np.ones(total)
    a = min(n, max(1, int(attack * rate)))
    env[:a] = np.linspace(0, 1, a)
    env[n:] = np.linspace(1, 0, rel) ** 2    # convex decay = soft ring-out
    return w * env


def float_to_sound(wave_):
    """Turn a float waveform into a pygame Sound (mono mixer assumed)."""
    return pygame.sndarray.make_sound((wave_ * 32767).astype(np.int16))


def _run_ffmpeg(args):
    subprocess.run([FFMPEG, "-nostdin", "-y", *args],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def convert_to_wav(src, dst, rate=RATE):
    """Decode any audio file into a mono PCM wav at `rate` via ffmpeg."""
    _run_ffmpeg(["-i", src, "-ac", "1", "-ar", str(rate), dst])


def _atempo_chain(speed):
    """An ffmpeg atempo filter string that slows tempo to `speed` (pitch kept).

    A single atempo only accepts 0.5–2.0, so factors below 0.5 are reached by
    chaining (e.g. 0.35 → atempo=0.5,atempo=0.7)."""
    factors, s = [], speed
    while s < 0.5 - 1e-9:
        factors.append(0.5)
        s /= 0.5
    factors.append(s)
    return ",".join(f"atempo={f:.4f}" for f in factors)


def tempo_wav(src, dst, speed):
    """Write `src` slowed to `speed` (tempo only, pitch unchanged) via ffmpeg."""
    _run_ffmpeg(["-i", src, "-filter:a", _atempo_chain(speed), dst])


def load_wav(path):
    """Read a PCM wav → (mono float32 samples in [-1,1], sample_rate)."""
    with wave.open(path, "rb") as w:
        rate = w.getframerate()
        raw = w.readframes(w.getnframes())
    samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    return samples, rate


def decode_audio(path):
    """Load any audio file → (mono float32 samples in [-1,1], sample_rate)."""
    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False).name
    try:
        convert_to_wav(path, tmp)
        return load_wav(tmp)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def detect_tape(samples, rate):
    """
    Reduce a recording to a boolean on/off tape at TRACK_HZ.

    Rectify, smooth into an amplitude envelope, then threshold: loud = a tone is
    sounding, quiet = a pause.
    """
    env = np.abs(samples)
    win = max(1, int(0.02 * rate))                  # 20 ms smoothing
    env = np.convolve(env, np.ones(win) / win, mode="same")
    thr = 0.18 * np.percentile(env, 99)             # adaptive to the recording
    on = env > max(thr, 1e-4)

    duration = len(samples) / rate
    n = max(1, int(duration * TRACK_HZ))
    idx = np.clip((np.arange(n) / TRACK_HZ * rate).astype(int), 0, len(on) - 1)
    return on[idx], duration


def _write_wav(path, pcm, rate=RATE):
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())


def export_morse(events, path, freq=TONE_FREQ, wave="sine", amp=0.5):
    """Render events into a beep track and write mp3/wav.

    Each event is (start, end) — synthesized with the freq/wave args — or
    (start, end, freq, wave[, attack, release]) to carry its own per-element style.
    """
    if not events:
        return

    def style(ev):
        f, w = (ev[2], ev[3]) if len(ev) >= 4 else (freq, wave)
        at, rl = (ev[4], ev[5]) if len(ev) >= 6 else (0.006, 0.02)
        return f, w, at, rl

    total = max(ev[1] + style(ev)[3] for ev in events) + 0.2   # room for ring-out
    buf = np.zeros(int(total * RATE), dtype=np.float32)
    for ev in events:
        f, w, at, rl = style(ev)
        tone = make_tone(ev[1] - ev[0], f, w, amp, at, rl)
        i = int(ev[0] * RATE)
        buf[i:i + len(tone)] += tone[: len(buf) - i]
    pcm = (np.clip(buf, -1, 1) * 32767).astype(np.int16)

    if path.lower().endswith(".wav"):
        _write_wav(path, pcm)
    else:
        tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False).name
        try:
            _write_wav(tmp, pcm)
            _run_ffmpeg(["-i", tmp, path])
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)


def sample_tape(track, track_len, t):
    """Is a tone on at time t (seconds) on the given tape? Bounds-safe."""
    if track is None or t < 0:
        return False
    i = int(t * TRACK_HZ)
    if 0 <= i < track_len:
        return bool(track[i])
    return False


def fmt_time(secs):
    secs = max(0, int(secs))
    return f"{secs // 60:01d}:{secs % 60:02d}"


# ─────────────────────────────── the app ───────────────────────────────────

MENU, SELECT, RECORD = "menu", "select", "record"

# drawing glyphs — Unicode by default, swapped to ASCII on non-UTF-8 terminals
BLOCK, V, H = "█", "│", "─"
TL, TR, BL, BR = "┌", "┐", "└", "┘"
CARET, AXIS = "▼", "·"


def use_ascii_glyphs():
    """Fall back to ASCII so the tape/frames still draw on a non-UTF-8 terminal."""
    global BLOCK, V, H, TL, TR, BL, BR, CARET, AXIS
    BLOCK, V, H = "#", "|", "-"
    TL = TR = BL = BR = "+"
    CARET, AXIS = "v", "."

# CSI escape tails → normalized arrow tokens (some terminals send ESC O x instead
# of ESC [ x in application-cursor mode, so we map both).
_ESC_SEQ = {"[A": "UP", "[B": "DOWN", "[C": "RIGHT", "[D": "LEFT",
            "OA": "UP", "OB": "DOWN", "OC": "RIGHT", "OD": "LEFT"}


class App:
    def __init__(self, scr):
        self.scr = scr
        curses.curs_set(0)
        scr.keypad(True)
        try:
            curses.set_escdelay(25)   # disambiguate a lone ESC from arrow keys fast
        except (curses.error, AttributeError):
            pass
        self._init_colors()
        self._timeout = -1
        # special curses keycodes → normalized tokens (used when keypad DOES decode)
        self._keymap = {
            curses.KEY_UP: "UP", curses.KEY_DOWN: "DOWN",
            curses.KEY_LEFT: "LEFT", curses.KEY_RIGHT: "RIGHT",
            curses.KEY_ENTER: "\n", curses.KEY_BACKSPACE: "\x7f",
        }

        # audio: mixer only, no display/window
        self.audio = True
        try:
            pygame.mixer.init(RATE, -16, 1, 512)
        except Exception:                            # noqa: BLE001 — degrade silently
            self.audio = False
        self.dot_snd = self.dash_snd = None

        # recording style (pitch / speed / timbre / decay) — restored from session
        (self.pitch_i, self.speed_i, self.wave_i,
         self.shape_i) = self._load_settings()
        self._apply_style()

        self.cheat_lines = self._load_cheatsheet()

        self.state = MENU
        self.menu_idx = 0
        self.show_cheat = False
        self.status = ""
        self.running = True
        self.last = time.monotonic()

        # select
        self.tmp_wav = None            # original-rate decoded wav (== speed 1x)
        self.sel_speed_i = 0           # index into SEL_SPEEDS
        # slowed variants are pre-rendered in a background thread on open, so
        # switching speed just swaps in a ready file (no ffmpeg on the keypress)
        self.speed_wavs = {}           # speed_i -> ready wav path
        self._speed_lock = threading.Lock()
        self._speed_thread = None
        self._speed_cancel = False
        self.sel_track = None
        self.sel_len = 0
        self.sel_duration = 0.0
        self.sel_head = 0.0
        self.sel_playing = False

        # record — a composed timeline stored in units: list of (type, gap_before)
        self.rec = []
        self.last_press = 0.0

    # ---- setup ----

    @property
    def freq(self):
        return PITCHES[self.pitch_i]

    @property
    def wave(self):
        return WAVES[self.wave_i]

    @property
    def unit(self):
        return SPEEDS[self.speed_i][1]

    @property
    def attack(self):
        return SHAPES[self.shape_i][1]

    @property
    def release(self):
        return SHAPES[self.shape_i][2]

    def _apply_style(self):
        """Regenerate the dot/dash beeps for the current pitch/speed/timbre/decay."""
        if not self.audio:
            return
        self.dot_snd = float_to_sound(
            make_tone(self.unit, self.freq, self.wave, 0.5, self.attack, self.release))
        self.dash_snd = float_to_sound(
            make_tone(3 * self.unit, self.freq, self.wave, 0.5, self.attack, self.release))

    def _load_settings(self):
        """Restore the style indices saved last session (defaults if absent/bad)."""
        idx = {"pitch_i": 3, "speed_i": 2, "wave_i": 0, "shape_i": 1}
        sizes = {"pitch_i": len(PITCHES), "speed_i": len(SPEEDS),
                 "wave_i": len(WAVES), "shape_i": len(SHAPES)}
        try:
            with open(SETTINGS, encoding="utf-8") as fh:
                data = json.load(fh)
            for key in idx:
                val = int(data[key])
                if 0 <= val < sizes[key]:
                    idx[key] = val
        except (OSError, ValueError, KeyError, TypeError):
            pass
        return idx["pitch_i"], idx["speed_i"], idx["wave_i"], idx["shape_i"]

    def _save_settings(self):
        try:
            with open(SETTINGS, "w", encoding="utf-8") as fh:
                json.dump({"pitch_i": self.pitch_i, "speed_i": self.speed_i,
                           "wave_i": self.wave_i, "shape_i": self.shape_i}, fh)
        except OSError:
            pass

    def _init_colors(self):
        # neutral: no colours, just plain/dim/bold/reverse so it reads like any
        # ordinary terminal program.
        self.NORM = curses.A_NORMAL
        self.DIM = curses.A_DIM
        self.HEAD = curses.A_BOLD
        self.SEL = curses.A_REVERSE

    def _load_cheatsheet(self):
        try:
            with open(CHEATSHEET, encoding="utf-8") as fh:
                return [ln.rstrip("\n") for ln in fh]
        except OSError:
            return ["cheatsheet.txt not found"]

    # ---- low-level drawing ----

    def put(self, y, x, s, attr=0):
        # curses.error: off-screen write; UnicodeError: glyph the locale can't encode
        try:
            self.scr.addstr(y, x, s, attr)
        except (curses.error, UnicodeError):
            pass

    def frame(self, y, x, h, w, title=None):
        self.put(y, x, TL + H * (w - 2) + TR)
        for r in range(1, h - 1):
            self.put(y + r, x, V)
            self.put(y + r, x + w - 1, V)
        self.put(y + h - 1, x, BL + H * (w - 2) + BR)
        if title:
            self.put(y, x + 2, f" {title} ")

    # ---- input ----

    def _set_timeout(self, v):
        self._timeout = v
        self.scr.timeout(v)

    def read_key(self):
        """One normalized key, or None on timeout.

        We decode arrow/ESC sequences ourselves because keypad() doesn't reliably
        reassemble them across terminals when an input timeout is set.
        """
        try:
            k = self.scr.get_wch()
        except (curses.error, ValueError):
            return None
        if isinstance(k, int):
            return self._keymap.get(k, k)
        if k == "\x1b":
            self.scr.timeout(0)       # drain the rest of a CSI sequence, if any
            seq = ""
            try:
                for _ in range(4):
                    try:
                        c = self.scr.get_wch()
                    except (curses.error, ValueError):
                        break
                    if isinstance(c, int):
                        break
                    seq += c
                    if c.isalpha() or c == "~":
                        break
            finally:
                self.scr.timeout(self._timeout)
            return _ESC_SEQ.get(seq, "\x1b")   # bare ESC if not an arrow
        return k

    # ---- main loop ----

    def run(self):
        self._set_timeout(33)         # ~30 fps
        while self.running:
            k = self.read_key()
            if k is not None:
                self.on_key(k)
            self.update()
            self.render()
        if self.audio:
            pygame.mixer.quit()

    def update(self):
        now = time.monotonic()
        dt = now - self.last
        self.last = now
        if self.state == SELECT and self.sel_playing:
            self.sel_head += dt * self._speed()   # slowed playback → head crawls
            if self.sel_head >= self.sel_duration:
                self.sel_head = self.sel_duration
                self.sel_playing = False

    # ---- key handling ----

    @staticmethod
    def _is_enter(k):
        return k in ("\n", "\r")

    @staticmethod
    def _is_back(k):
        return k in ("\x7f", "\b")

    def on_key(self, k):
        # cheatsheet toggle (terminals can't report key-release, so it's a toggle)
        if k in ("c", "C") and self.state in (SELECT, RECORD):
            self.show_cheat = not self.show_cheat
            return
        if self.show_cheat and k == "\x1b":
            self.show_cheat = False
            return

        if self.state == MENU:
            self.key_menu(k)
        elif self.state == SELECT:
            self.key_select(k)
        elif self.state == RECORD:
            self.key_record(k)

    def key_menu(self, k):
        if k == "UP":
            self.menu_idx = (self.menu_idx - 1) % 2
        elif k == "DOWN":
            self.menu_idx = (self.menu_idx + 1) % 2
        elif self._is_enter(k):
            (self.start_select if self.menu_idx == 0 else self.start_record)()
        elif k == "1":
            self.menu_idx = 0
            self.start_select()
        elif k == "2":
            self.menu_idx = 1
            self.start_record()
        elif k in ("q", "Q", "\x1b"):
            self.running = False

    def key_select(self, k):
        if k == "\x1b":
            self.stop_select()
        elif k == " ":
            self.toggle_play()
        elif k == "LEFT":
            self.seek(-3)
        elif k == "RIGHT":
            self.seek(3)
        elif k in ("s", "S"):
            self.cycle_speed()

    def key_record(self, k):
        if k == "\x1b":
            self.state = MENU
            self.status = ""
        elif k in (".", "j", "J"):
            self.key_morse(".")
        elif k in ("-", "k", "K"):
            self.key_morse("-")
        elif k in ("p", "P"):
            self._cycle("pitch_i", PITCHES)
        elif k in ("s", "S"):
            self._cycle("speed_i", SPEEDS)
        elif k in ("t", "T"):
            self._cycle("wave_i", WAVES)
        elif k in ("e", "E"):
            self._cycle("shape_i", SHAPES)
        elif self._is_back(k):
            if self.rec:
                self.rec.pop()
                self.status = f"removed last element  ({len(self.rec)} left)"
            else:
                self.status = "nothing to remove"
        elif self._is_enter(k):
            self.save_record()

    # ---- select flow ----

    def start_select(self):
        path = self.browse()
        if not path:
            return
        self.status = "decoding …"
        self.render()
        try:
            self.tmp_wav = tempfile.NamedTemporaryFile(suffix=".wav", delete=False).name
            convert_to_wav(path, self.tmp_wav)
            samples, rate = load_wav(self.tmp_wav)
            self.sel_track, self.sel_duration = detect_tape(samples, rate)
            self.sel_len = len(self.sel_track)
            self.sel_head = 0.0
            self.sel_playing = self.audio
            self.sel_speed_i = 0
            self.speed_wavs = {0: self.tmp_wav}
            self._start_prerender()
            self._play_from(0.0)
            self.state = SELECT
            self.status = os.path.basename(path)
        except Exception as ex:                      # noqa: BLE001 — surface to UI
            self.status = f"error: {ex}"
            self.state = MENU

    def stop_select(self):
        if self.audio:
            pygame.mixer.music.stop()
        self._speed_cancel = True                    # let the prerender thread bail
        with self._speed_lock:
            paths = set(self.speed_wavs.values())
        paths.add(self.tmp_wav)
        for p in paths:
            if p and os.path.exists(p):
                os.unlink(p)
        self.speed_wavs = {}
        self.tmp_wav = None
        self.state = MENU
        self.status = ""

    def _speed(self):
        return SEL_SPEEDS[self.sel_speed_i][1]

    def _start_prerender(self):
        """Render every slowed variant of the current cassette in the background,
        so a later speed change just loads a ready file — no pause on the key."""
        if not self.audio:
            return
        self._speed_cancel = False
        src = self.tmp_wav

        def work():
            for i, (_, factor) in enumerate(SEL_SPEEDS):
                if factor >= 0.999 or self._speed_cancel:
                    continue
                out = tempfile.NamedTemporaryFile(suffix=".wav", delete=False).name
                try:
                    tempo_wav(src, out, factor)
                except Exception:                    # noqa: BLE001 — skip this speed
                    if os.path.exists(out):
                        os.unlink(out)
                    continue
                with self._speed_lock:
                    if self._speed_cancel:           # cassette closed mid-render
                        if os.path.exists(out):
                            os.unlink(out)
                        return
                    self.speed_wavs[i] = out

        self._speed_thread = threading.Thread(target=work, daemon=True)
        self._speed_thread.start()

    def _current_wav(self):
        with self._speed_lock:
            return self.speed_wavs.get(self.sel_speed_i)

    def _play_from(self, head):
        """Cue the current speed's wav to `head` (original-track seconds), honoring
        the paused state. Slowed files run on a stretched clock, so scale."""
        if not self.audio:
            return
        path = self._current_wav()
        if not path:
            return
        try:
            pygame.mixer.music.load(path)
            pygame.mixer.music.play(start=head / self._speed())
            if not self.sel_playing:
                pygame.mixer.music.pause()
        except pygame.error:
            pass

    def cycle_speed(self):
        self.sel_speed_i = (self.sel_speed_i + 1) % len(SEL_SPEEDS)
        label = SEL_SPEEDS[self.sel_speed_i][0]
        self.status = f"speed {label}"
        # variants are pre-rendered on open, so this is normally already there;
        # only if the render hasn't caught up yet do we wait briefly.
        if self.audio and self._speed() < 0.999 and self._current_wav() is None:
            self.status = f"preparing {label} …"
            self.render()
            while self._current_wav() is None and not self._speed_cancel:
                time.sleep(0.03)
            self.status = f"speed {label}"
            self.last = time.monotonic()             # don't count the wait as playback
        self._play_from(self.sel_head)

    def toggle_play(self):
        if not self.audio:
            return
        self.sel_playing = not self.sel_playing
        if self.sel_playing:
            pygame.mixer.music.unpause()
        else:
            pygame.mixer.music.pause()

    def seek(self, secs):
        self.sel_head = max(0.0, min(self.sel_duration, self.sel_head + secs))
        self._play_from(self.sel_head)

    # ---- record flow ----

    def start_record(self):
        self.rec = []
        self.last_press = 0.0
        self.state = RECORD
        self.status = "tap . and - to key; Enter to save"

    def _cycle(self, attr, options):
        setattr(self, attr, (getattr(self, attr) + 1) % len(options))
        self._apply_style()
        self._save_settings()
        self.status = (f"style: pitch {self.freq} Hz   speed {SPEEDS[self.speed_i][0]}"
                       f"   tone {self.wave}   decay {SHAPES[self.shape_i][0]}")

    def _rec_timeline(self):
        """The recording as (start_unit, len_unit) spans plus total length, all in
        dot-units (1 = dot, 3 = dash) — for the tape, which is speed-independent."""
        pos, out = 0.0, []
        for entry in self.rec:
            typ, gap_before = entry[0], entry[1]
            pos += gap_before
            length = 1.0 if typ == "." else 3.0
            out.append((pos, length))
            pos += length
        return out, pos

    def _rec_seconds(self):
        """The timeline in seconds, each element carrying its own style.

        Every element uses the unit/pitch/timbre/decay that were active when it
        was keyed, so a style change part-way through only affects later elements.
        """
        t, out = 0.0, []
        for typ, gap_before, freq, wave, unit, attack, release in self.rec:
            t += gap_before * unit
            dur = (1.0 if typ == "." else 3.0) * unit
            out.append((t, t + dur, freq, wave, attack, release))
            t += dur
        return out, t

    def _rec_end(self):
        return self._rec_seconds()[1]

    def key_morse(self, typ):
        """Append an element, separated from the previous one by a gap = the real
        pause you took (in units), clamped to [1, 7] so fast taps never overlap.
        The current pitch/speed/timbre are frozen into the element."""
        now = time.monotonic()
        if self.rec:
            gap = min(7.0, max(1.0, (now - self.last_press) / self.unit))
        else:
            gap = 0.0
        self.rec.append((typ, gap, self.freq, self.wave, self.unit,
                         self.attack, self.release))
        self.last_press = now
        snd = self.dot_snd if typ == "." else self.dash_snd
        if self.audio and snd:
            snd.play()
        self.status = f"{len(self.rec)} elements"

    def save_record(self):
        if not self.rec:
            self.status = "nothing recorded"
            return
        # default to morse.mp3, or morse_1.mp3, morse_2.mp3 … if that already exists
        default = self._next_free_name("morse", ".mp3")
        path = self.prompt_text("save as (full path):", default)
        if not path:
            self.status = "save cancelled"
            return
        if os.path.splitext(path)[1].lower() not in (".mp3", ".wav"):
            path += ".mp3"
        if os.path.exists(path):
            if not self._confirm(f"{os.path.basename(path)} already exists - overwrite?"):
                self.status = "not saved - existing file kept"
                return
        try:
            events, _ = self._rec_seconds()   # each event carries its own style
            export_morse(events, path)
            self.status = f"saved: {path}"
            self.state = MENU
        except Exception as ex:                      # noqa: BLE001
            self.status = f"save error: {ex}"

    @staticmethod
    def _next_free_name(base, ext):
        """`<cwd>/base.ext`, or base_1.ext, base_2.ext … if earlier ones exist."""
        path = os.path.join(os.getcwd(), base + ext)
        i = 1
        while os.path.exists(path):
            path = os.path.join(os.getcwd(), f"{base}_{i}{ext}")
            i += 1
        return path

    def _confirm(self, question):
        """A blocking y/n modal. Returns True only on an explicit yes."""
        self._set_timeout(-1)
        try:
            while True:
                self.scr.erase()
                R, C = self.scr.getmaxyx()
                self.put(1, 2, "Save cassette", self.HEAD)
                self.put(3, 2, question[:C - 4])
                self.put(R - 2, 2, "y = overwrite    n / esc = keep the file", self.DIM)
                self.scr.refresh()
                k = self.read_key()
                if k in ("y", "Y"):
                    return True
                if k in ("n", "N", "\x1b"):
                    return False
        finally:
            self._set_timeout(33)
            self.last = time.monotonic()

    # ---- modal: keyboard file browser ----

    def _list_dir(self, cwd):
        items = [("..", True, os.path.dirname(cwd) or cwd)]
        try:
            names = sorted(os.listdir(cwd), key=str.lower)
        except OSError:
            names = []
        for n in names:
            if n.startswith("."):
                continue
            full = os.path.join(cwd, n)
            if os.path.isdir(full):
                items.append((n + "/", True, full))
        for n in names:
            if n.startswith("."):
                continue
            full = os.path.join(cwd, n)
            if os.path.isfile(full) and os.path.splitext(n)[1].lower() in AUDIO_EXTS:
                items.append((n, False, full))
        return items

    def browse(self):
        self._set_timeout(-1)       # block for keys while browsing
        cwd = os.getcwd()
        idx = 0
        try:
            while True:
                items = self._list_dir(cwd)
                idx = max(0, min(idx, len(items) - 1))
                self._render_browser(cwd, items, idx)
                k = self.read_key()
                if k == "UP":
                    idx = (idx - 1) % len(items)
                elif k == "DOWN":
                    idx = (idx + 1) % len(items)
                elif self._is_enter(k):
                    name, is_dir, full = items[idx]
                    if is_dir:
                        cwd = os.path.normpath(full)
                        idx = 0
                    else:
                        return full
                elif k == "LEFT" or self._is_back(k):
                    cwd = os.path.dirname(cwd) or cwd
                    idx = 0
                elif k == "\x1b":
                    return None
        finally:
            self._set_timeout(33)
            self.last = time.monotonic()

    def _render_browser(self, cwd, items, idx):
        self.scr.erase()
        R, C = self.scr.getmaxyx()
        self.put(1, 2, "Open cassette", self.HEAD)
        self.put(2, 2, ("dir: " + cwd)[:C - 4], self.DIM)
        top = 4
        rows = R - 7
        start = max(0, idx - rows + 1) if idx >= rows else 0
        for i in range(start, min(len(items), start + rows)):
            name, is_dir, _ = items[i]
            y = top + (i - start)
            mark = "> " if i == idx else "  "
            self.put(y, 2, (mark + name)[:C - 4], self.SEL if i == idx else self.NORM)
        self.put(R - 2, 2, "up/down move   enter open   left parent   esc cancel", self.DIM)
        self.scr.refresh()

    # ---- modal: keyboard text input ----

    def prompt_text(self, title, default=""):
        self._set_timeout(-1)       # block for keys while typing
        curses.curs_set(1)
        buf = list(default)
        try:
            while True:
                self._render_prompt(title, "".join(buf))
                k = self.read_key()
                if self._is_enter(k):
                    return "".join(buf).strip() or None
                if k == "\x1b":
                    return None
                if self._is_back(k):
                    if buf:
                        buf.pop()
                elif isinstance(k, str) and len(k) == 1 and k.isprintable():
                    buf.append(k)
        finally:
            curses.curs_set(0)
            self._set_timeout(33)
            self.last = time.monotonic()

    def _render_prompt(self, title, text):
        self.scr.erase()
        R, C = self.scr.getmaxyx()
        self.put(1, 2, "Save cassette", self.HEAD)
        self.put(3, 2, title)
        self.put(4, 2, "> ")
        self.put(4, 4, text[-(C - 8):])
        self.put(R - 2, 2, "type a path   enter save   esc cancel", self.DIM)
        self.scr.refresh()

    # ---- rendering ----

    def render(self):
        self.scr.erase()
        R, C = self.scr.getmaxyx()
        if R < 16 or C < 56:
            self.put(0, 0, "enlarge terminal (min 56x16)")
            self.scr.refresh()
            return

        if self.state == MENU:
            self._render_menu(R, C)
        elif self.state == SELECT:
            self._render_player(R, C, "Select Cassette", self.sel_head)
        elif self.state == RECORD:
            self._render_player(R, C, "Record Cassette", self._rec_end())
        if self.show_cheat:
            self._render_cheat(R, C)
        self.scr.refresh()

    def _render_menu(self, R, C):
        self.put(1, 2, "morse - cassette deck", self.HEAD)
        labels = ["Select Cassette", "Record Cassette"]
        subs = ["browse to an audio file, read the tape",
                "key it by hand, save as mp3 / wav"]
        for i, (lab, sub) in enumerate(zip(labels, subs)):
            y = 3 + i * 2
            sel = i == self.menu_idx
            mark = ">" if sel else " "
            self.put(y, 2, f"{mark} {i + 1}) {lab}", self.HEAD if sel else self.NORM)
            self.put(y, 26, sub, self.DIM)
        if self.status:
            self.put(8, 2, self.status[:C - 4])
        self.put(R - 2, 2, "up/down select   enter open   1/2 jump   q quit", self.DIM)

    def _render_player(self, R, C, title, head):
        overview = self.state == SELECT and self.sel_track is not None
        if self.state == RECORD:
            title += "   [REC]"
        self.put(1, 2, title, self.HEAD)
        self.put(2, 2, self.status[:C - 4], self.DIM)

        ty, tx = 4, 2
        tw = C - 4
        th = max(4, R - (11 if overview else 8))
        self.frame(ty, tx, th, tw)
        cols = self._tape_columns(tw - 4, head)
        self._paint_tape(ty + 1, tx + 2, th - 2, cols)

        iy = ty + th
        if self.state == SELECT:
            self.put(iy, 2, f"{fmt_time(head)} / {fmt_time(self.sel_duration)}"
                            f"   speed {SEL_SPEEDS[self.sel_speed_i][0]}")
            keys = "space play/pause   left/right seek   s slower   c cheatsheet   esc back"
        else:
            style = (f"pitch {self.freq}Hz   speed {SPEEDS[self.speed_i][0]}   "
                     f"tone {self.wave}   decay {SHAPES[self.shape_i][0]}   "
                     f"[p/s/t/e change]")
            self.put(iy, 2, f"{len(self.rec)} elements   length {fmt_time(head)}")
            self.put(iy + 1, 2, style[:C - 4], self.DIM)
            keys = ". / j dot   - / k dash   bksp undo   enter save   c cheatsheet   esc cancel"

        if overview:
            self._render_overview(iy + 2, tx + 2, tw - 4, head)

        self.put(R - 2, 2, keys[:C - 4], self.DIM)

    def _render_overview(self, oy, x, w, head):
        """A compressed map of the whole track. The pointer sits in the middle =
        'now'; the band slides under it (past on the left, upcoming on the right)
        and jumps when you seek."""
        center = w // 2
        dur = self.sel_duration
        ocps = max(2.0, min(10.0, w / max(dur, 1.0)))   # zoom: fit short tracks
        self.put(oy, x + center, CARET, self.HEAD)
        for j in range(w):
            t = head + (j - center) / ocps
            if t < 0 or t > dur:
                continue                                # off either end: blank
            if sample_tape(self.sel_track, self.sel_len, t):
                self.put(oy + 1, x + j, BLOCK)
            else:
                self.put(oy + 1, x + j, AXIS, self.DIM)

    def _tape_columns(self, w, head):
        """A boolean column per character cell, rightmost = `head`.

        Record draws each element as a fixed-width span (so every dot is the same
        width, no sampling jitter); Select point-samples the decoded envelope.
        """
        cols = [False] * w
        if self.state == RECORD:
            # work in units so element widths stay fixed (dot=2, dash=6) at any speed
            spans, total = self._rec_timeline()
            u_left = total - (w - 1) / CPU
            for s, length in spans:
                c0 = int(round((s - u_left) * CPU))
                cw = max(1, int(round(length * CPU)))
                for j in range(max(0, c0), min(w, c0 + cw)):
                    cols[j] = True
        else:
            for j in range(w):
                t = head - (w - 1 - j) / CPS
                if t >= 0 and sample_tape(self.sel_track, self.sel_len, t):
                    cols[j] = True
        return cols

    def _paint_tape(self, y, x, h, cols):
        mid = h // 2
        for j, on in enumerate(cols):
            if on:
                for r in range(h):
                    self.put(y + r, x + j, BLOCK)
            elif j % 2 == 0:
                self.put(y + mid, x + j, AXIS, self.DIM)

    def _render_cheat(self, R, C):
        w = min(40, C - 4)
        x = C - w - 2
        y, h = 1, R - 2
        for r in range(h):                            # blank the panel area
            self.put(y + r, x, " " * w)
        self.frame(y, x, h, w, "cheatsheet")
        yy = y + 1
        for line in self.cheat_lines:
            if yy >= y + h - 1:
                break
            self.put(yy, x + 2, line[:w - 4])
            yy += 1


def _terminal_is_unicode():
    """Can the terminal's actual codeset (what curses encodes with) carry █?"""
    cs = ""
    try:
        cs = locale.nl_langinfo(locale.CODESET)      # the codeset curses uses
    except (AttributeError, ValueError):
        cs = locale.getpreferredencoding(False) or ""
    try:
        "█".encode(cs or "ascii")
        return True
    except (LookupError, UnicodeError):
        return False


def main():
    try:
        locale.setlocale(locale.LC_ALL, "")
    except locale.Error:
        pass
    if not _terminal_is_unicode():
        use_ascii_glyphs()                           # non-UTF-8 terminal (some SSH/macOS)
    curses.wrapper(lambda scr: App(scr).run())


if __name__ == "__main__":
    main()
