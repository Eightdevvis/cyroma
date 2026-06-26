# morse — a peer-to-peer Morse-code radio for your terminal

A tiny window with one glowing dot. The dot blinks Morse. Underneath is a
monospace menu where you record/compose messages and replay the ones you've
received. Two people connect **directly** (WebRTC) — there's no server in the
middle, you just email each other a one-line connection code once.

## Send it to someone

Email these four files (or zip them):

```
morse.py     run.sh     run.bat     README.md
```

The recipient runs:

- **macOS / Linux** — open a Terminal in the folder, then `./run.sh`
- **Windows** — double-click `run.bat`

The launcher installs [`uv`](https://docs.astral.sh/uv/) once (no admin
rights), which grabs the two dependencies (`textual`, `aiortc`) into a
throwaway environment and starts the app. First launch downloads a bit; after
that it's instant.

> Already have Python set up? You can skip the launcher:
> `pip install textual aiortc aiohttp && python morse.py`

## Connecting

You connect **directly** — messages flow peer-to-peer and end-to-end
encrypted; nothing in the middle ever sees them. There are two ways to set up
that direct link.

### Auto-connect (recommended — set up once, then it just works)

On first launch the app writes a `config.json` next to itself. Edit it:

```json
{
  "name": "you",        // your name
  "peer": "her",        // her name
  "secret": "X9q...long-random-string"   // pre-filled — must MATCH on both sides
}
```

- Put your name in `name`, her name in `peer`.
- **She does the same in her copy, but swaps the names** (`name: "her"`,
  `peer: "you"`), and uses **the exact same `secret`** — send her the secret
  once (any private channel). The names must differ.

From then on, whenever you both have the app open, it finds the other side and
connects on its own — no codes to exchange. The top bar shows
`link: connected`. (How: the apps meet on a private topic on the free
[ntfy.sh](https://ntfy.sh) broker just to trade the WebRTC handshake. The
broker only relays that handshake, never your messages.)

### Manual handshake (fallback, zero third parties)

Don't want to rely on the broker, or it's down? Press **`c`** and use the
copy/paste flow:

1. **One** of you presses **Create invite** → a code appears (**Save code to
   file** writes `invite.txt`). Send it over.
2. The **other** presses **Join with invite**, pastes it, presses **Connect**
   → an answer code appears. Send that back.
3. The first pastes the answer and presses **Connect**.

Either way, once you see `link: connected` every message you send lands on the
other side, and vice-versa.

### If it won't connect

This uses STUN only (no relay server), so some networks that block
peer-to-peer — office networks, some mobile carriers (symmetric NAT) — can
prevent a direct link. Auto-connect keeps retrying on its own; if it stays on
`searching…`, have one side switch to home Wi-Fi or a **phone hotspot** (and
on manual mode, regenerate the invite) and try again.

## Using it

Top bar = connection status. Then the menu:

- **`1` Record a message**
  - Tap Morse by hand: **`.`/`j`** = dot, **`-`/`k`** = dash, **`space`** =
    next letter, **`w`** = word gap, **`⌫`** = undo. The dot flashes each
    element and the decode updates live.
  - **Enter** sends (and saves + plays it locally on the dot). **Esc** cancels.
- **`2` Received messages** — **↑/↓** to pick, **Enter** to replay on the dot,
  **`s`** to stop, **Esc** back. When a new message arrives, a red counter
  ticks up next to this menu item; nothing pops up and nothing auto-plays —
  open the list and replay it yourself when you want. The counter clears when
  you open the list.
- **`c` Connect to her** · **`q` Quit**

Messages persist in `messages.json` next to the app.

## Notes / limits

- 1-to-1 link (you and one contact at a time).
- Hand-keyed Morse is decoded best-effort for the label, but playback always
  reproduces exactly what was keyed, so a wonky decode never corrupts the blink.
- Terminals don't report key-release, so keying is tap-based (one key per
  element) rather than press-and-hold.
