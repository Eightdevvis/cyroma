#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "textual>=0.60",
#   "aiortc>=1.9",
#   "aiohttp>=3.9",
# ]
# ///
"""
morse — a tiny peer-to-peer Morse-code radio in your terminal.

A single glowing dot blinks Morse. Underneath, a monospace menu lets you
record/compose messages and replay received ones. Two people connect directly
(WebRTC data channel) by emailing each other a one-line connection code — no
server in the middle.

Run:   uv run morse.py        (or: python morse.py  with deps installed)
"""

from __future__ import annotations

import asyncio
import base64
import getpass
import json
import math
import secrets
import time
import uuid
import zlib
from dataclasses import dataclass, asdict
from pathlib import Path

import aiohttp
from rich.color import Color
from rich.style import Style
from rich.text import Text

from textual import events, work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Button, ContentSwitcher, Static, TextArea

from aiortc import (
    RTCConfiguration,
    RTCDataChannel,
    RTCIceServer,
    RTCPeerConnection,
    RTCSessionDescription,
)

# --------------------------------------------------------------------------- #
# Morse code                                                                   #
# --------------------------------------------------------------------------- #

MORSE = {
    "A": ".-", "B": "-...", "C": "-.-.", "D": "-..", "E": ".", "F": "..-.",
    "G": "--.", "H": "....", "I": "..", "J": ".---", "K": "-.-", "L": ".-..",
    "M": "--", "N": "-.", "O": "---", "P": ".--.", "Q": "--.-", "R": ".-.",
    "S": "...", "T": "-", "U": "..-", "V": "...-", "W": ".--", "X": "-..-",
    "Y": "-.--", "Z": "--..",
    "0": "-----", "1": ".----", "2": "..---", "3": "...--", "4": "....-",
    "5": ".....", "6": "-....", "7": "--...", "8": "---..", "9": "----.",
    ".": ".-.-.-", ",": "--..--", "?": "..--..", "'": ".----.", "!": "-.-.--",
    "/": "-..-.", "(": "-.--.", ")": "-.--.-", "&": ".-...", ":": "---...",
    ";": "-.-.-.", "=": "-...-", "+": ".-.-.", "-": "-....-", "_": "..--.-",
    '"': ".-..-.", "@": ".--.-.",
}
INV_MORSE = {v: k for k, v in MORSE.items()}

# one "unit" of Morse timing, in seconds. higher = slower.
UNIT = 0.11


def text_to_morse(text: str) -> str:
    """'HI YO' -> '.... ..  / -.-- ---'  (letters by space, words by ' / ')."""
    words = []
    for word in text.upper().split():
        words.append(" ".join(MORSE[c] for c in word if c in MORSE))
    return " / ".join(w for w in words if w)


def morse_to_text(morse: str) -> str:
    out = []
    for word in morse.strip().split(" / "):
        letters = [INV_MORSE.get(sym, "·") for sym in word.split() if sym]
        out.append("".join(letters))
    return " ".join(w for w in out if w)


def morse_timeline(morse: str) -> list[tuple[bool, int]]:
    """Expand a Morse string into a list of (is_on, units) steps for playback."""
    seq: list[tuple[bool, int]] = []
    words = [w for w in morse.strip().split(" / ") if w]
    for wi, word in enumerate(words):
        letters = [l for l in word.split() if l]
        for li, letter in enumerate(letters):
            for si, sym in enumerate(letter):
                seq.append((True, 3 if sym == "-" else 1))
                if si < len(letter) - 1:
                    seq.append((False, 1))          # gap between elements
            if li < len(letters) - 1:
                seq.append((False, 3))              # gap between letters
        if wi < len(words) - 1:
            seq.append((False, 7))                  # gap between words
    return seq


# --------------------------------------------------------------------------- #
# Stored messages                                                              #
# --------------------------------------------------------------------------- #

STORE = Path(__file__).resolve().parent / "messages.json"


@dataclass
class MorseMessage:
    id: str
    ts: float
    text: str
    morse: str
    direction: str   # "sent" | "recv"

    @property
    def stamp(self) -> str:
        return time.strftime("%H:%M", time.localtime(self.ts))


def load_messages() -> list[MorseMessage]:
    if not STORE.exists():
        return []
    try:
        return [MorseMessage(**m) for m in json.loads(STORE.read_text())]
    except Exception:
        return []


def save_messages(messages: list[MorseMessage]) -> None:
    try:
        STORE.write_text(json.dumps([asdict(m) for m in messages], indent=2))
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# WebRTC signaling helpers (manual copy/paste, no server)                      #
# --------------------------------------------------------------------------- #

def rtc_config() -> RTCConfiguration:
    return RTCConfiguration(iceServers=[
        RTCIceServer(urls=["stun:stun.l.google.com:19302"]),
        RTCIceServer(urls=["stun:stun1.l.google.com:19302"]),
    ])


def sdp_to_blob(desc: RTCSessionDescription) -> str:
    raw = json.dumps({"sdp": desc.sdp, "type": desc.type}).encode()
    return base64.urlsafe_b64encode(zlib.compress(raw, 9)).decode()


def blob_to_desc(blob: str) -> RTCSessionDescription:
    raw = zlib.decompress(base64.urlsafe_b64decode(blob.strip().encode()))
    obj = json.loads(raw)
    return RTCSessionDescription(sdp=obj["sdp"], type=obj["type"])


async def wait_ice_complete(pc: RTCPeerConnection) -> None:
    """aiortc has no trickle ICE — gather fully, then read localDescription."""
    if pc.iceGatheringState == "complete":
        return
    done = asyncio.Event()

    @pc.on("icegatheringstatechange")
    def _on_change() -> None:
        if pc.iceGatheringState == "complete":
            done.set()

    if pc.iceGatheringState == "complete":     # guard against a race
        return
    await done.wait()


# --------------------------------------------------------------------------- #
# Auto-connect config + signaling rendezvous (ntfy.sh public broker)           #
# --------------------------------------------------------------------------- #
#
# The broker only relays the SDP handshake (offer/answer) on a topic derived
# from your shared secret — it never sees your messages, which flow directly
# peer-to-peer over the encrypted WebRTC channel. Both people put each other's
# names + the SAME secret in config.json; the one whose name sorts first is the
# offerer. Once configured, opening the app reconnects on its own.

CONFIG = Path(__file__).resolve().parent / "config.json"
NTFY = "https://ntfy.sh"


def load_config() -> dict | None:
    """Return a valid {name, peer, secret} config, or None (writing a template
    on first run so the user has something to fill in)."""
    if not CONFIG.exists():
        try:
            CONFIG.write_text(json.dumps({
                "name": getpass.getuser() or "me",
                "peer": "",
                "secret": secrets.token_urlsafe(12),
                "_help": "Put her name in 'peer'; share this exact 'secret' "
                         "with her (she swaps name/peer in her own config). "
                         "Then both of you just open the app.",
            }, indent=2))
        except Exception:
            pass
        return None
    try:
        cfg = json.loads(CONFIG.read_text())
    except Exception:
        return None
    if cfg.get("name") and cfg.get("peer") and cfg.get("secret"):
        return cfg
    return None


# --------------------------------------------------------------------------- #
# Textual messages (network callbacks -> UI thread)                            #
# --------------------------------------------------------------------------- #

class OfferReady(Message):
    def __init__(self, blob: str) -> None:
        self.blob = blob
        super().__init__()


class AnswerReady(Message):
    def __init__(self, blob: str) -> None:
        self.blob = blob
        super().__init__()


class ConnState(Message):
    def __init__(self, state: str) -> None:
        self.state = state
        super().__init__()


class Incoming(Message):
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        super().__init__()


# --------------------------------------------------------------------------- #
# The glowing dot                                                              #
# --------------------------------------------------------------------------- #

GRID_W, GRID_H = 27, 11
_RAMP = "  ..::--==++**##@@"


def _build_falloff() -> list[list[float]]:
    cx, cy = (GRID_W - 1) / 2, (GRID_H - 1) / 2
    radius = 5.6
    grid = []
    for y in range(GRID_H):
        row = []
        for x in range(GRID_W):
            dx = (x - cx) * 0.5           # cells are ~2x taller than wide
            dy = y - cy
            d = math.hypot(dx, dy)
            row.append(max(0.0, 1.0 - d / radius) ** 1.3)
        grid.append(row)
    return grid


_FALLOFF = _build_falloff()


def _glow_color(intensity: float) -> Color:
    """Warm amber/gold ember that whitens at full brightness (telegraph vibe)."""
    i = max(0.0, min(1.0, intensity))
    r = int(70 + 150 * i + 35 * i ** 3)
    g = int(35 + 150 * i + 70 * i ** 3)
    b = int(8 + 50 * i + 130 * i ** 3)
    return Color.from_rgb(min(r, 255), min(g, 255), min(b, 255))


class Dot(Widget):
    """A single glowing point. Drive it by setting .target while .active=True."""

    DEFAULT_CSS = "Dot { height: 11; content-align: center middle; }"

    def __init__(self) -> None:
        super().__init__()
        self.brightness = 0.06
        self.target = 0.06
        self.active = False
        self._phase = 0.0

    def on_mount(self) -> None:
        self.set_interval(1 / 30, self._tick)

    def _tick(self) -> None:
        self._phase += 0.06
        if not self.active:
            # gentle idle ember so the dot always feels alive
            self.target = 0.06 + 0.05 * (0.5 + 0.5 * math.sin(self._phase))
        # ease toward the target for a glow rather than a hard on/off
        self.brightness += (self.target - self.brightness) * 0.55
        self.refresh()

    def render(self) -> Text:
        b = self.brightness
        text = Text(justify="center")
        for row in _FALLOFF:
            for f in row:
                inten = b * f
                if inten < 0.05:
                    text.append(" ")
                else:
                    idx = min(len(_RAMP) - 1, int(inten * (len(_RAMP) - 1)))
                    text.append(_RAMP[idx], Style(color=_glow_color(inten)))
            text.append("\n")
        return text


# --------------------------------------------------------------------------- #
# Bottom panels (the swappable monospace menu)                                 #
# --------------------------------------------------------------------------- #

class MainPanel(Static):
    can_focus = True

    def show(self, unread: int) -> None:
        badge = f"  [b red]({unread})[/b red]" if unread else ""
        self.update(
            "[b]MORSE[/b]  ::  pick an option\n\n"
            "  [b]1[/b]  record a message\n"
            f"  [b]2[/b]  received messages{badge}\n"
            "  [b]c[/b]  connect to her (no ssh)\n"
            "  [b]q[/b]  quit"
        )


class RecordPanel(Static):
    can_focus = True

    def reset(self) -> None:
        self.letter: list[str] = []     # current letter being keyed (./-)
        self.letters: list[str] = []    # committed Morse letters / "/" markers
        self.started = time.time()
        self.composing = False
        self.refresh_view()

    def add_symbol(self, sym: str) -> None:
        self.letter.append(sym)
        self.refresh_view()

    def commit_letter(self) -> None:
        if self.letter:
            self.letters.append("".join(self.letter))
            self.letter = []
        self.refresh_view()

    def add_word_gap(self) -> None:
        self.commit_letter()
        if self.letters and self.letters[-1] != "/":
            self.letters.append("/")
        self.refresh_view()

    def backspace(self) -> None:
        if self.letter:
            self.letter.pop()
        elif self.letters:
            self.letters.pop()
        self.refresh_view()

    def current_morse(self) -> str:
        parts = list(self.letters)
        if self.letter:
            parts.append("".join(self.letter))
        out = " ".join(parts).replace(" / ", " / ").replace("/", "/")
        # normalise "/" tokens into proper word separators
        return " ".join(p for p in out.split())

    def refresh_view(self) -> None:
        morse = self.current_morse()
        decoded = morse_to_text(morse) if morse else ""
        elapsed = int(time.time() - self.started)
        live = ("".join(self.letter)) or "…"
        self.update(
            f"[b red]● REC[/b red]  {elapsed:>3}s     keying letter: [b]{live}[/b]\n\n"
            f"  morse  : {morse or '(empty)'}\n"
            f"  decoded: [b]{decoded or '(empty)'}[/b]\n\n"
            "  [b].[/b]/[b]j[/b] dot   [b]-[/b]/[b]k[/b] dash   [b]space[/b] next letter   "
            "[b]w[/b] word gap\n"
            "  [b]⌫[/b] undo   [b]enter[/b] send   [b]esc[/b] cancel"
        )


class MessagesPanel(Static):
    can_focus = True

    def show(self, messages: list[MorseMessage], selected: int) -> None:
        if not messages:
            self.update(
                "[b]RECEIVED MESSAGES[/b]\n\n  (nothing yet)\n\n  [b]esc[/b] back"
            )
            return
        lines = ["[b]RECEIVED MESSAGES[/b]  ::  ↑↓ select   "
                 "[b]enter[/b] replay   [b]esc[/b] back\n"]
        for i, m in enumerate(messages):
            arrow = "[b]▸[/b]" if i == selected else " "
            tag = "← recv" if m.direction == "recv" else "→ sent"
            label = m.text or "(·)"
            line = f" {arrow} {m.stamp}  {tag}  [b]{label}[/b]"
            if i == selected:
                line = f"[reverse]{line}[/reverse]"
            lines.append(line)
        self.update("\n".join(lines))


class ConnectPanel(Static):
    """Copy/paste WebRTC handshake UI."""

    def compose(self) -> ComposeResult:
        yield Static(id="cx-auto")
        yield Static(id="cx-info")
        with Horizontal(id="cx-buttons"):
            yield Button("Create invite", id="cx-offer", variant="success")
            yield Button("Join with invite", id="cx-join", variant="primary")
        yield Static("Your code (send this to your contact):", classes="cx-label")
        yield TextArea("", id="cx-out", read_only=True)
        yield Button("Save code to file", id="cx-save")
        yield Static("Paste the code you received:", classes="cx-label")
        yield TextArea("", id="cx-in")
        yield Button("Connect", id="cx-go", variant="warning")
        yield Static("", id="cx-status")

    def on_mount(self) -> None:
        self.query_one("#cx-info", Static).update(
            "[b]MANUAL HANDSHAKE[/b] (fallback)  ::  one of you presses [b]Create\n"
            "invite[/b] and sends the code; the other presses [b]Join with invite[/b],\n"
            "pastes it, and sends the answer back.  If your network blocks\n"
            "peer-to-peer (office / carrier NAT), try home Wi-Fi or a phone hotspot."
        )


# --------------------------------------------------------------------------- #
# The app                                                                      #
# --------------------------------------------------------------------------- #

class MorseApp(App):
    CSS = """
    Screen { align: center top; }
    #status { height: 1; color: $text-muted; content-align: center middle; }
    #panels { height: auto; padding: 1 2; }
    MainPanel, RecordPanel, MessagesPanel { height: auto; padding: 1 2; }
    ConnectPanel { height: auto; padding: 1 2; }
    .cx-label { color: $text-muted; margin-top: 1; }
    #cx-out, #cx-in { height: 4; }
    #cx-buttons { height: auto; }
    Button { margin: 0 1; }
    """

    BINDINGS = [("ctrl+c", "quit", "quit")]

    def __init__(self) -> None:
        super().__init__()
        self.mode = "main"
        self.messages: list[MorseMessage] = load_messages()
        self.selected = 0
        self.unread = 0
        self.playing = False
        self.pc: RTCPeerConnection | None = None
        self.channel: RTCDataChannel | None = None
        self.role: str | None = None
        self.conn = "offline"
        self.auto: dict | None = None
        self.is_offerer = False
        self._connected: asyncio.Event | None = None
        self._sig_in: asyncio.Queue | None = None

    # -- layout ----------------------------------------------------------- #

    def compose(self) -> ComposeResult:
        yield Dot()
        yield Static("offline", id="status")
        with ContentSwitcher(initial="main", id="panels"):
            yield MainPanel(id="main")
            yield RecordPanel(id="record")
            yield MessagesPanel(id="messages")
            yield ConnectPanel(id="connect")

    def on_mount(self) -> None:
        self.dot = self.query_one(Dot)
        self._connected = asyncio.Event()
        self._sig_in = asyncio.Queue()
        self.set_interval(20.0, self._keepalive)
        self.auto = load_config()
        self.switch_to("main")
        self.refresh_auto_line()
        if self.auto:
            self.auto_connect()

    def update_status(self) -> None:
        self.query_one("#status", Static).update(f"link: {self.conn}")
        self.refresh_auto_line()

    def refresh_auto_line(self) -> None:
        try:
            line = self.query_one("#cx-auto", Static)
        except Exception:
            return
        if self.auto:
            role = "host" if self.is_offerer else "guest"
            line.update(
                f"[b]AUTO-CONNECT[/b]  {self.auto['name']} ↔ {self.auto['peer']}"
                f"  ({role})  ::  link {self.conn}\n")
        else:
            line.update(
                "[b]AUTO-CONNECT: off[/b]  ::  set [b]peer[/b] in config.json and share "
                "its [b]secret[/b]\nwith her to enable hands-free reconnect. Manual "
                "handshake below works now.\n")

    def switch_to(self, mode: str) -> None:
        self.mode = mode
        self.query_one(ContentSwitcher).current = mode
        if mode == "record":
            self.query_one(RecordPanel).reset()
            self.query_one(RecordPanel).focus()
        elif mode == "messages":
            self.unread = 0
            self.selected = min(self.selected, max(0, len(self.messages) - 1))
            self.query_one(MessagesPanel).show(self.messages, self.selected)
            self.query_one(MessagesPanel).focus()
        elif mode == "main":
            self.query_one(MainPanel).show(self.unread)
            self.query_one(MainPanel).focus()
        self.update_status()

    # -- key routing ------------------------------------------------------ #

    def on_key(self, event: events.Key) -> None:
        if isinstance(self.focused, TextArea):
            return  # let the connect-panel paste boxes type freely

        key = event.key
        if key == "s" and self.playing:
            self.stop_playback()
            event.stop()
            return

        if self.mode == "main":
            self._key_main(event)
        elif self.mode == "record":
            self._key_record(event)
        elif self.mode == "messages":
            self._key_messages(event)
        elif self.mode == "connect" and key == "escape":
            self.switch_to("main")
            event.stop()

    def _key_main(self, event: events.Key) -> None:
        k = event.key
        if k == "1":
            self.switch_to("record")
        elif k == "2":
            self.switch_to("messages")
        elif k == "c":
            self.switch_to("connect")
        elif k == "q":
            self.action_quit()
        else:
            return
        event.stop()

    def _key_record(self, event: events.Key) -> None:
        panel = self.query_one(RecordPanel)
        k = event.key
        if k in (".", "j", "full_stop"):
            panel.add_symbol(".")
            self.flash_symbol(".")
        elif k in ("-", "k", "minus"):
            panel.add_symbol("-")
            self.flash_symbol("-")
        elif k == "space":
            panel.commit_letter()
        elif k == "w":
            panel.add_word_gap()
        elif k == "backspace":
            panel.backspace()
        elif k == "enter":
            self.send_recorded()
        elif k == "escape":
            self.switch_to("main")
        else:
            return
        event.stop()

    def _key_messages(self, event: events.Key) -> None:
        k = event.key
        if not self.messages:
            if k == "escape":
                self.switch_to("main")
                event.stop()
            return
        if k in ("up", "k"):
            self.selected = (self.selected - 1) % len(self.messages)
        elif k in ("down", "j"):
            self.selected = (self.selected + 1) % len(self.messages)
        elif k == "enter":
            self.play_message(self.messages[self.selected])
        elif k == "escape":
            self.switch_to("main")
            event.stop()
            return
        else:
            return
        self.query_one(MessagesPanel).show(self.messages, self.selected)
        event.stop()

    # -- recording / sending ---------------------------------------------- #

    def send_recorded(self) -> None:
        panel = self.query_one(RecordPanel)
        panel.commit_letter()
        morse = panel.current_morse()
        if not morse:
            self.switch_to("main")
            return
        self.dispatch_message(morse_to_text(morse), morse)
        self.switch_to("main")

    def dispatch_message(self, text: str, morse: str) -> None:
        msg = MorseMessage(
            id=uuid.uuid4().hex, ts=time.time(),
            text=text, morse=morse, direction="sent",
        )
        self.messages.append(msg)
        save_messages(self.messages)
        if self.channel is not None and self.channel.readyState == "open":
            try:
                self.channel.send(json.dumps(
                    {"t": "msg", "text": text, "morse": morse}))
            except Exception as exc:
                self.notify(f"send failed: {exc}", severity="error")
        else:
            self.notify("not connected — nothing was sent",
                        severity="warning")
        self.play_message(msg)

    # -- playback --------------------------------------------------------- #

    def play_message(self, msg: MorseMessage) -> None:
        self.play_morse(msg.morse)

    @work(exclusive=True, group="player")
    async def play_morse(self, morse: str) -> None:
        self.playing = True
        self.dot.active = True
        try:
            for is_on, units in morse_timeline(morse):
                self.dot.target = 1.0 if is_on else 0.0
                await asyncio.sleep(units * UNIT)
        except asyncio.CancelledError:
            pass
        finally:
            self.dot.target = 0.0
            self.dot.active = False
            self.playing = False

    def stop_playback(self) -> None:
        self.workers.cancel_group(self, "player")
        self.dot.active = False
        self.dot.target = 0.0
        self.playing = False

    @work(exclusive=True, group="flash")
    async def flash_symbol(self, sym: str) -> None:
        self.dot.active = True
        self.dot.target = 1.0
        await asyncio.sleep((3 if sym == "-" else 1) * UNIT)
        self.dot.target = 0.0
        await asyncio.sleep(UNIT)
        self.dot.active = False

    def _keepalive(self) -> None:
        if self.channel is not None and self.channel.readyState == "open":
            try:
                self.channel.send(json.dumps({"t": "ka"}))
            except Exception:
                pass

    # -- connection wiring ------------------------------------------------ #

    def _wire_pc(self, pc: RTCPeerConnection) -> None:
        @pc.on("connectionstatechange")
        async def _on_state() -> None:
            if pc is not self.pc:
                return  # ignore events from a superseded connection
            self.post_message(ConnState(pc.connectionState))

    def _wire_channel(self, channel: RTCDataChannel) -> None:
        self.channel = channel

        @channel.on("open")
        def _on_open() -> None:
            if self._connected is not None:
                self._connected.set()
            self.post_message(ConnState("connected"))

        @channel.on("message")
        def _on_message(data) -> None:
            try:
                self.post_message(Incoming(json.loads(data)))
            except Exception:
                pass

        @channel.on("close")
        def _on_close() -> None:
            self.post_message(ConnState("closed"))

    @work(exclusive=True, group="webrtc")
    async def start_offer(self) -> None:
        self.role = "offer"
        await self._reset_pc()
        pc = RTCPeerConnection(rtc_config())
        self.pc = pc
        self._wire_pc(pc)
        self._wire_channel(pc.createDataChannel("morse", ordered=True))
        await pc.setLocalDescription(await pc.createOffer())
        await wait_ice_complete(pc)
        self.post_message(OfferReady(sdp_to_blob(pc.localDescription)))

    @work(exclusive=True, group="webrtc")
    async def start_answer(self, offer_blob: str) -> None:
        self.role = "answer"
        await self._reset_pc()
        pc = RTCPeerConnection(rtc_config())
        self.pc = pc
        self._wire_pc(pc)

        @pc.on("datachannel")
        def _on_dc(channel: RTCDataChannel) -> None:
            self._wire_channel(channel)

        await pc.setRemoteDescription(blob_to_desc(offer_blob))
        await pc.setLocalDescription(await pc.createAnswer())
        await wait_ice_complete(pc)
        self.post_message(AnswerReady(sdp_to_blob(pc.localDescription)))

    @work(group="webrtc")
    async def accept_answer(self, answer_blob: str) -> None:
        if self.pc is None:
            return
        await self.pc.setRemoteDescription(blob_to_desc(answer_blob))

    async def _reset_pc(self) -> None:
        if self.pc is not None:
            try:
                await self.pc.close()
            except Exception:
                pass
        self.pc = None
        self.channel = None

    # -- auto-connect via public broker (ntfy) ---------------------------- #

    @work(exclusive=True, group="auto")
    async def auto_connect(self) -> None:
        cfg = self.auto
        topic = f"morse-{cfg['secret']}"
        me, peer = cfg["name"], cfg["peer"]
        self.is_offerer = me < peer
        self.conn = "searching…"
        self.update_status()
        async with aiohttp.ClientSession() as session:
            sub = asyncio.create_task(self._sig_subscribe(session, topic, me))
            try:
                if self.is_offerer:
                    await self._offerer_loop(session, topic, me)
                else:
                    await self._answerer_loop(session, topic, me)
            finally:
                sub.cancel()

    async def _sig_publish(self, session, topic, obj) -> None:
        try:
            await session.post(
                f"{NTFY}/{topic}", data=json.dumps(obj),
                timeout=aiohttp.ClientTimeout(total=15))
        except Exception:
            pass

    async def _sig_subscribe(self, session, topic, me) -> None:
        url = f"{NTFY}/{topic}/json"
        while True:
            try:
                async with session.get(
                    url, timeout=aiohttp.ClientTimeout(total=None)) as resp:
                    async for raw in resp.content:
                        raw = raw.strip()
                        if not raw:
                            continue
                        try:
                            evt = json.loads(raw)
                            if evt.get("event") != "message":
                                continue
                            msg = json.loads(evt.get("message", ""))
                        except Exception:
                            continue
                        if msg.get("from") == me:
                            continue
                        await self._sig_in.put(msg)
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(3)   # stream dropped — reconnect

    async def _wait_sig(self, typ: str, timeout: float, sid: str | None):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return None
            try:
                msg = await asyncio.wait_for(self._sig_in.get(), remaining)
            except asyncio.TimeoutError:
                return None
            if msg.get("type") == typ and (sid is None or msg.get("sid") == sid):
                return msg

    async def _offerer_loop(self, session, topic, me) -> None:
        delays = [5, 5, 8, 13, 21]     # republish cadence; quick, then easing off
        while True:
            await self._reset_pc()
            pc = RTCPeerConnection(rtc_config())
            self.pc = pc
            self._wire_pc(pc)
            self._wire_channel(pc.createDataChannel("morse", ordered=True))
            await pc.setLocalDescription(await pc.createOffer())
            await wait_ice_complete(pc)
            self._connected.clear()
            self.conn = "searching…"
            self.update_status()
            sid = secrets.token_hex(4)
            offer = {"from": me, "type": "offer", "sid": sid,
                     "sdp": sdp_to_blob(pc.localDescription)}
            connected = False
            for d in delays:
                await self._sig_publish(session, topic, offer)
                ans = await self._wait_sig("answer", d, sid)
                if not ans:
                    continue
                try:
                    await pc.setRemoteDescription(blob_to_desc(ans["sdp"]))
                    await asyncio.wait_for(self._connected.wait(), 15)
                    connected = True
                except Exception:
                    connected = False
                break
            if connected:
                while self._connected.is_set():   # hold until the link drops
                    await asyncio.sleep(2)
            else:
                await asyncio.sleep(8)            # regenerate a fresh offer

    async def _answerer_loop(self, session, topic, me) -> None:
        while True:
            msg = await self._sig_in.get()
            if msg.get("type") != "offer" or self._connected.is_set():
                continue
            await self._reset_pc()
            pc = RTCPeerConnection(rtc_config())
            self.pc = pc
            self._wire_pc(pc)

            @pc.on("datachannel")
            def _on_dc(channel: RTCDataChannel) -> None:
                self._wire_channel(channel)

            try:
                await pc.setRemoteDescription(blob_to_desc(msg["sdp"]))
                await pc.setLocalDescription(await pc.createAnswer())
                await wait_ice_complete(pc)
                await self._sig_publish(session, topic, {
                    "from": me, "type": "answer", "sid": msg.get("sid"),
                    "sdp": sdp_to_blob(pc.localDescription)})
            except Exception:
                pass

    # -- connect-panel buttons ------------------------------------------- #

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        status = self.query_one("#cx-status", Static)
        if bid == "cx-offer":
            self.workers.cancel_group(self, "auto")   # manual takes over
            status.update("generating invite…")
            self.start_offer()
        elif bid == "cx-join":
            self.workers.cancel_group(self, "auto")
            self.role = "answer"
            status.update("paste the invite below, then press Connect")
        elif bid == "cx-go":
            blob = self.query_one("#cx-in", TextArea).text.strip()
            if not blob:
                status.update("[red]paste a code first[/red]")
                return
            if self.role == "offer":
                status.update("connecting…")
                self.accept_answer(blob)
            else:
                status.update("generating answer…")
                self.start_answer(blob)
        elif bid == "cx-save":
            self._save_code()

    def _save_code(self) -> None:
        blob = self.query_one("#cx-out", TextArea).text.strip()
        if not blob:
            return
        name = "invite.txt" if self.role == "offer" else "answer.txt"
        path = Path(__file__).resolve().parent / name
        path.write_text(blob)
        self.query_one("#cx-status", Static).update(f"saved → {path.name}")

    # -- message handlers (UI thread) ------------------------------------- #

    def on_offer_ready(self, msg: OfferReady) -> None:
        self.query_one("#cx-out", TextArea).text = msg.blob
        self.query_one("#cx-status", Static).update(
            "invite ready — email it, then paste their reply and press Connect")

    def on_answer_ready(self, msg: AnswerReady) -> None:
        self.query_one("#cx-out", TextArea).text = msg.blob
        self.query_one("#cx-status", Static).update(
            "answer ready — email it back; you'll connect once they paste it")

    def on_conn_state(self, msg: ConnState) -> None:
        self.conn = msg.state
        self.update_status()
        if msg.state == "connected":
            self.notify("connected — you're on the air", title="link up")
            if self.mode == "connect":
                self.switch_to("main")
        elif msg.state in ("failed", "closed", "disconnected"):
            if self._connected is not None:
                self._connected.clear()
            if msg.state == "failed" and not self.auto:
                self.notify(
                    "connection failed — a network may block P2P; "
                    "try home Wi-Fi or a phone hotspot",
                    severity="error", timeout=10)
            self.channel = None

    def on_incoming(self, msg: Incoming) -> None:
        payload = msg.payload
        if payload.get("t") == "ka":
            return
        text = payload.get("text", "")
        morse = payload.get("morse", "")
        if not morse:
            return
        m = MorseMessage(
            id=uuid.uuid4().hex, ts=time.time(),
            text=text, morse=morse, direction="recv",
        )
        self.messages.append(m)
        save_messages(self.messages)
        if self.mode == "messages":
            # they're already looking at the list — refresh it, no badge
            self.query_one(MessagesPanel).show(self.messages, self.selected)
        else:
            self.unread += 1
            if self.mode == "main":
                self.query_one(MainPanel).show(self.unread)

    # -- shutdown --------------------------------------------------------- #

    async def action_quit(self) -> None:
        self.workers.cancel_group(self, "auto")
        await self._reset_pc()
        self.exit()


def main() -> None:
    MorseApp().run()


if __name__ == "__main__":
    main()
