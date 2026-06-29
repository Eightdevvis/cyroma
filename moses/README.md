# morse — a Morse-code cassette deck for your terminal

A keyboard-only terminal app (no window, no mouse) with exactly two things to do:

- **Select Cassette** — browse to an audio file (`.mp3`, `.wav`, …) with the
  arrow keys, then watch it play back as a scrolling **signal tape** of tones and
  pauses. No letters are shown: short block = dot, long block = dash, dark =
  pause. You translate it yourself.
- **Record Cassette** — key Morse by hand with `.` and `-` (pauses included,
  every beep audible), then type a path and **save it as an mp3 or wav**.

Press **`c`** in either player to toggle the translation cheatsheet on/off.

## Run it

Open a terminal in this folder:

- **macOS / Linux** — `./run.sh`
- **Windows** — double-click `run.bat` (or run it from a Command Prompt)

The launcher installs [`uv`](https://docs.astral.sh/uv/) once (no admin rights),
which grabs the dependencies (`pygame`, `numpy`, `imageio-ffmpeg`) into a
throwaway environment and starts the app. ffmpeg comes bundled via
`imageio-ffmpeg`, so mp3 just works — nothing to install by hand.

> Already have Python set up? Skip the launcher:
> `pip install pygame numpy imageio-ffmpeg && python morse.py`
> (on Windows also `pip install windows-curses`)
>
> Use a UTF-8 terminal at least ~56×16 in size — the tape and frames use
> block-drawing characters. `pygame` is only used for audio output; no window
> is ever opened.

## Controls

**Menu**
- `↑`/`↓` move · `ENTER` select · `1`/`2` jump straight in · `Q` quit

**Select Cassette** (file browser)
- `↑`/`↓` move · `ENTER` open folder / pick file · `←` parent folder · `ESC` cancel

**Select Cassette** (player)
- `SPACE` play / pause · `←`/`→` seek 3 s
- `c` cheatsheet · `ESC` back

**Record Cassette**
- `.` or `J` = dot · `-` or `K` = dash
- pauses = however long you wait between keys
- `BKSP` undo last element · `ENTER` save (type a path, `ENTER` confirms)
- style of the cassette you're making: `p` pitch (8 steps) · `s` speed (5 steps) ·
  `t` tone (sine/triangle/square/sawtooth) · `e` decay (hard ↔ soft ring-out)
  - each setting is frozen into every element as you key it, so switching part-way
    through mixes styles — e.g. the first tones at 800 Hz, the later ones at 1200 Hz
  - your last-used style is remembered across sessions (in `settings.json`)
- `c` cheatsheet · `ESC` cancel

## Notes

- The cheatsheet is a **toggle** (`c` on, `c`/`ESC` off). Terminals can't report
  key-release, so "hold to peek" isn't possible — hence the toggle.
- `cheatsheet.txt` next to the app holds the table — edit it freely.
- Tone detection in Select mode works best on clean recordings (steady pitch,
  little background noise); it thresholds the volume envelope to decide tone vs.
  pause.
