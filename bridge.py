#!/usr/bin/env python3
"""MeshCore <-> Meshtastic bridge for a Raspberry Pi.

MeshCore and Meshtastic do not interoperate over the air — different protocols
on the same LoRa band. But if a single Pi hosts one node of each (two USB LoRa
radios), it can relay text between the two meshes in software. This is that
relay.

Architecture
------------
The two client libraries have different concurrency models, so the bridge runs
one asyncio event loop and adapts each side to it:

  * MeshCore  — the `meshcore` library is asyncio-native. We subscribe to its
    channel-message events directly.
  * Meshtastic — the `meshtastic` library is synchronous and delivers received
    packets through a `pubsub` callback on its own thread. We hand those packets
    to the asyncio loop via `loop.call_soon_threadsafe`.

Every relayed message is tagged (config `*_prefix`) so readers know it crossed
from the other network, and a short-window dedup table prevents echo loops (a
message we just injected coming back and being relayed again).

The bridge also disciplines the MeshCore node's clock. nRF52 nodes have no
battery-backed RTC, so every power cycle resets the node's clock to its build
date while radio settings survive — which would silently corrupt the timestamps
the pilot is collecting. The Pi knows the real time, so it pushes it on connect
and re-checks on an interval (`[meshcore] clock_sync*`).

STATUS: scaffold. The structure and the loop-prevention logic are here; the two
`# TODO(hardware)` spots need a real MeshCore node and a real Meshtastic node on
the same Pi to finish and verify (the exact event/packet field names differ by
library version). It is intentionally small and readable — a Pi that mostly
idles does not need a compiled binary.

    pip install -r requirements.txt
    cp config.example.toml config.toml   # then edit ports/channels
    python3 bridge.py --config config.toml
"""
from __future__ import annotations

import argparse
import asyncio
import ctypes
import os
import sys
import time
import tomllib
from dataclasses import dataclass, field

# A Raspberry Pi has no battery-backed RTC either — the same premise this whole
# feature rests on for the nRF52. After a power cut the Pi can come up at the
# epoch, or at whatever fake-hwclock saved before shutdown, and systemd starts
# services well before NTP has stepped the clock. Writing *that* onto the node
# would overwrite a possibly-correct clock with a confidently-wrong one, which
# is worse than not syncing at all. So the host clock has to earn our trust
# before we push it.
MIN_PLAUSIBLE_EPOCH = 1767225600   # 2026-01-01Z — older than this, the Pi has not been stepped
MAX_PLAUSIBLE_EPOCH = 4102444800   # 2100-01-01Z — newer than this, something is wrong
_CLOCK_CMD_TIMEOUT = 10.0          # bound each node round trip so a wedged node cannot stall us
_STA_UNSYNC = 0x0040               # <linux/timex.h>: clock is not disciplined by NTP
_TIMESYNCD_STAMP = "/run/systemd/timesync/synchronized"


class _Timex(ctypes.Structure):
    """Linux `struct timex`, for a read-only adjtimex(2) query."""

    _fields_ = [
        ("modes", ctypes.c_int), ("offset", ctypes.c_long), ("freq", ctypes.c_long),
        ("maxerror", ctypes.c_long), ("esterror", ctypes.c_long), ("status", ctypes.c_int),
        ("constant", ctypes.c_long), ("precision", ctypes.c_long), ("tolerance", ctypes.c_long),
        ("time_sec", ctypes.c_long), ("time_usec", ctypes.c_long), ("tick", ctypes.c_long),
        ("ppsfreq", ctypes.c_long), ("jitter", ctypes.c_long), ("shift", ctypes.c_int),
        ("stabil", ctypes.c_long), ("jitcnt", ctypes.c_long), ("calcnt", ctypes.c_long),
        ("errcnt", ctypes.c_long), ("stbcnt", ctypes.c_long), ("tai", ctypes.c_int),
        ("padding", ctypes.c_int * 11),
    ]


def kernel_clock_synchronized() -> bool | None:
    """Is an NTP source currently disciplining the system clock?

    Reads the kernel's own NTP state via adjtimex(2), so it is agnostic to which
    daemon is running — systemd-timesyncd, chrony and ntpd all set it. Returns
    None if we cannot tell (not Linux, no libc, unexpected ABI), which callers
    treat as "no opinion" rather than as a failure.
    """
    try:
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        buf = _Timex()
        buf.modes = 0  # read-only
        if libc.adjtimex(ctypes.byref(buf)) < 0:
            return None
        return not (buf.status & _STA_UNSYNC)
    except Exception:
        return None


def host_clock_trusted(require_sync: bool = True) -> tuple[bool, str]:
    """Decide whether the host clock is fit to write to a mesh node."""
    now = time.time()
    if not MIN_PLAUSIBLE_EPOCH <= now <= MAX_PLAUSIBLE_EPOCH:
        return False, f"host clock reads {int(now)}, outside the plausible range"
    if not require_sync:
        return True, "plausible (sync check disabled)"
    # systemd-timesyncd publishes this only once it has actually stepped/slewed.
    if os.path.exists(_TIMESYNCD_STAMP):
        return True, "timesyncd synchronized"
    synced = kernel_clock_synchronized()
    if synced is True:
        return True, "kernel clock synchronized"
    if synced is False:
        return False, "kernel reports the clock is not NTP-disciplined"
    return True, "plausible (no sync state available)"


def _clamp(value, name: str, *, low: float) -> float:
    """Coerce a config number into a sane range, complaining rather than failing.

    A typo here (0, or a negative) would turn the sync loop into a tight spin on
    the serial port the relay shares. Clamping keeps a hard-to-reach node online
    instead of crash-looping it, and the warning says what happened.
    """
    try:
        out = float(value)
    except (TypeError, ValueError):
        print(f"config: {name}={value!r} is not a number, using {low}", file=sys.stderr, flush=True)
        return low
    if out < low:
        print(f"config: {name}={out} is below the minimum, clamping to {low}", file=sys.stderr, flush=True)
        return low
    return out


@dataclass
class Config:
    mc_port: str
    mc_baud: int
    mc_channel: int
    mt_port: str
    mt_channel: int
    direction: str
    mc_prefix: str
    mt_prefix: str
    dedup_window: float
    command_prefix: str
    require_direct: bool
    authorized_mc_keys: set
    authorized_mt_ids: set
    mc_clock_sync: bool = True
    mc_clock_sync_interval: float = 3600.0
    mc_clock_max_skew: float = 30.0
    mc_clock_retry: float = 30.0
    mc_require_host_sync: bool = True

    @classmethod
    def load(cls, path: str) -> "Config":
        with open(path, "rb") as f:
            c = tomllib.load(f)
        return cls(
            mc_port=c["meshcore"]["port"], mc_baud=c["meshcore"].get("baud", 115200),
            mc_channel=c["meshcore"].get("channel", 0),
            mt_port=c["meshtastic"]["port"], mt_channel=c["meshtastic"].get("channel", 0),
            direction=c["bridge"].get("direction", "both"),
            mc_prefix=c["bridge"].get("meshcore_prefix", "[MT] "),
            mt_prefix=c["bridge"].get("meshtastic_prefix", "[MC] "),
            dedup_window=float(c["bridge"].get("dedup_window_seconds", 30)),
            command_prefix=c["bridge"].get("command_prefix", "!bridge"),
            require_direct=c.get("commands", {}).get("require_direct_message", True),
            authorized_mc_keys={k.lower() for k in c.get("commands", {}).get("authorized_meshcore_keys", [])},
            authorized_mt_ids={str(i) for i in c.get("commands", {}).get("authorized_meshtastic_ids", [])},
            mc_clock_sync=bool(c["meshcore"].get("clock_sync", True)),
            mc_clock_sync_interval=_clamp(
                c["meshcore"].get("clock_sync_interval_seconds", 3600),
                "clock_sync_interval_seconds", low=60.0),
            mc_clock_max_skew=_clamp(
                c["meshcore"].get("clock_max_skew_seconds", 30),
                "clock_max_skew_seconds", low=0.0),
            mc_clock_retry=_clamp(
                c["meshcore"].get("clock_retry_seconds", 30),
                "clock_retry_seconds", low=5.0),
            mc_require_host_sync=bool(c["meshcore"].get("require_host_clock_sync", True)),
        )

    @property
    def mc_to_mt(self) -> bool:
        return self.direction in ("both", "meshcore_to_meshtastic")

    @property
    def mt_to_mc(self) -> bool:
        return self.direction in ("both", "meshtastic_to_meshcore")


class Dedup:
    """Remembers recently-relayed (text) so an echoed message is not relayed twice."""

    def __init__(self, window_s: float):
        self.window = window_s
        self._seen: dict[str, float] = {}

    def seen_recently(self, key: str) -> bool:
        now = time.monotonic()
        self._seen = {k: t for k, t in self._seen.items() if now - t < self.window}
        if key in self._seen:
            return True
        self._seen[key] = now
        return False


class Bridge:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.dedup = Dedup(cfg.dedup_window)
        self.mc = None   # meshcore client
        self.mt = None   # meshtastic interface
        self.loop: asyncio.AbstractEventLoop | None = None
        self.started_at = time.monotonic()
        self.clock_syncs = 0                       # how many times we set the node clock
        self.last_clock_sync: float | None = None  # unix epoch of the last successful set
        self._clock_task: asyncio.Task | None = None  # held so the loop is not GC'd
        self._clock_backoff = cfg.mc_clock_retry   # grows while syncing is unhealthy

    # ---- Command interface (bridge is queryable from either mesh) ----

    def is_command(self, text: str) -> bool:
        return text.strip().lower().startswith(self.cfg.command_prefix.lower())

    def is_authorized(self, origin: str, sender_id: str | None, is_direct: bool) -> bool:
        """Only accept commands from an allow-listed sender over a direct message.

        A shared-channel message carries no verified sender identity, so it can
        never be trusted for commands — reject it outright. A direct message
        does carry the sender's public key / node id; accept only if that
        identity is on the configured allow-list.

        Security note: on MeshCore a DM is end-to-end encrypted to the bridge's
        key, so the sender key is cryptographically authenticated. On Meshtastic
        this is only true for PKC (public-key) direct messages — firmware 2.5+
        with a key exchanged; legacy PSK DMs carry a spoofable node id, so run
        PKC if you rely on Meshtastic-side command auth.
        """
        if self.cfg.require_direct and not is_direct:
            return False
        if sender_id is None:
            return False
        allow = self.cfg.authorized_mc_keys if origin == "meshcore" else self.cfg.authorized_mt_ids
        return sender_id.lower() in allow if origin == "meshcore" else sender_id in allow

    def handle_command(self, text: str, origin: str) -> str:
        """Return a reply string for an AUTHORIZED command. Callers must gate on
        is_authorized() first. `origin` is the mesh the query arrived on, which
        is also where the reply is sent back."""
        args = text.strip()[len(self.cfg.command_prefix):].strip().split()
        cmd = args[0].lower() if args else "help"
        if cmd in ("help", ""):
            return "bridge commands: status | uptime | clock | ping | help"
        if cmd == "ping":
            return "pong"
        if cmd == "clock":
            return self.clock_summary()
        if cmd == "uptime":
            secs = int(time.monotonic() - self.started_at)
            return f"bridge up {secs // 3600}h {secs % 3600 // 60}m"
        if cmd == "status":
            return (f"bridge OK · direction={self.cfg.direction} · "
                    f"MeshCore ch{self.cfg.mc_channel} · Meshtastic ch{self.cfg.mt_channel} · "
                    f"{self.clock_summary()}")
        return f"unknown command '{cmd}' — try: {self.cfg.command_prefix} help"

    def clock_summary(self) -> str:
        """One-line clock health, so the pilot can ask over the mesh whether the
        node's timestamps can be trusted — the counters are useless unheard."""
        if not self.cfg.mc_clock_sync:
            return "clock sync off"
        if self.last_clock_sync is None:
            return "clock never synced"
        mins = int(time.time() - self.last_clock_sync) // 60
        return f"clock synced {mins}m ago ({self.clock_syncs}x)"

    # ---- MeshCore clock discipline ----
    #
    # nRF52 MeshCore nodes (e.g. the Seeed XIAO nRF52840) have no battery-backed
    # RTC. Radio settings survive a power cycle; the clock does not — it reverts
    # to the firmware build date. Anything the node timestamps after that is
    # wrong by years, which for this project means the pilot's own evaluation
    # data (uptime, traffic, when a repeater was last heard) is quietly bogus.
    #
    # The Pi is better placed to know the real time, but only once NTP has
    # stepped it — see host_clock_trusted(). Until then we deliberately do
    # nothing, because a wrong write is worse than no write.

    @staticmethod
    def _epoch_from_event(ev) -> int | None:
        """Pull a unix epoch out of a meshcore Event, tolerating payload shapes.

        The library wraps replies in an Event whose `payload` is usually a dict;
        the key name for the device time has moved between versions, so probe
        the plausible ones, most specific first. Every candidate is range-checked
        so an unrelated counter cannot masquerade as a clock reading — accepting
        a bogus value would look like "the node is fine" and silently skip the
        write this whole feature exists to make. bool is rejected explicitly:
        isinstance(True, int) is True in Python, so a flag would otherwise read
        as epoch 1.
        """
        payload = getattr(ev, "payload", ev)
        candidates = []
        if isinstance(payload, dict):
            candidates = [payload.get(k) for k in ("curr_time", "time", "epoch", "secs", "timestamp")]
        else:
            candidates = [payload]
        for val in candidates:
            if isinstance(val, bool) or not isinstance(val, (int, float)):
                continue
            if MIN_PLAUSIBLE_EPOCH <= val <= MAX_PLAUSIBLE_EPOCH:
                return int(val)
        return None

    async def _sync_meshcore_clock(self, *, force: bool = False) -> str:
        """Push host UTC to the MeshCore node.

        Returns one of: "set" (wrote), "ok" (node already close enough),
        "failed", "untrusted-host", "no-client" — the caller uses this to decide
        how soon to look again.
        """
        if self.mc is None:
            return "no-client"

        trusted, why = host_clock_trusted(self.cfg.mc_require_host_sync)
        if not trusted:
            # Deliberately do nothing: see the note above. We will be back soon.
            print(f"clock: not syncing — {why}", file=sys.stderr, flush=True)
            return "untrusted-host"

        node_epoch = None
        try:
            ev = await asyncio.wait_for(self.mc.commands.get_time(), _CLOCK_CMD_TIMEOUT)
            node_epoch = self._epoch_from_event(ev)
        except asyncio.TimeoutError:
            print("clock: timed out reading node time", file=sys.stderr, flush=True)
        except Exception as exc:
            print(f"clock: could not read node time ({exc})", file=sys.stderr, flush=True)

        skew = None if node_epoch is None else node_epoch - int(time.time())
        if not force and skew is not None and abs(skew) <= self.cfg.mc_clock_max_skew:
            return "ok"

        # Re-read the clock here rather than reusing a value sampled before the
        # round trip above, which could be seconds stale on a busy serial link.
        now = int(time.time())
        try:
            await asyncio.wait_for(self.mc.commands.set_time(now), _CLOCK_CMD_TIMEOUT)
        except asyncio.TimeoutError:
            print("clock: timed out setting node time", file=sys.stderr, flush=True)
            return "failed"
        except Exception as exc:
            print(f"clock: set_time failed ({exc})", file=sys.stderr, flush=True)
            return "failed"
        self.clock_syncs += 1
        self.last_clock_sync = now
        drift = "node time unreadable" if skew is None else f"node was {skew:+d}s off"
        print(f"clock: set MeshCore node to {now} ({drift}; host {why})", flush=True)
        return "set"

    def _next_clock_delay(self, result: str) -> float:
        """How long to wait before looking at the node clock again.

        A healthy check waits the full interval. Anything else — a failed write,
        or a host clock NTP has not caught up with yet — retries soon and backs
        off, so we are not stuck an hour behind reality after a power cut.
        """
        if result in ("set", "ok"):
            self._clock_backoff = self.cfg.mc_clock_retry
            return self.cfg.mc_clock_sync_interval
        # Never wait longer than the steady-state interval, even if someone
        # configured clock_retry_seconds larger than clock_sync_interval_seconds.
        delay = min(self._clock_backoff, self.cfg.mc_clock_sync_interval)
        self._clock_backoff = min(self._clock_backoff * 2, self.cfg.mc_clock_sync_interval)
        return delay

    async def _clock_sync_loop(self, first_result: str = "ok") -> None:
        """Re-check the node clock forever. Never let one failure kill the task."""
        delay = self._next_clock_delay(first_result)
        while True:
            await asyncio.sleep(delay)
            try:
                result = await self._sync_meshcore_clock()
            except Exception as exc:
                print(f"clock: sync loop error ({exc})", file=sys.stderr, flush=True)
                result = "failed"
            delay = self._next_clock_delay(result)

    async def run(self) -> None:
        self.loop = asyncio.get_running_loop()
        await self._connect_meshcore()
        self._connect_meshtastic()
        if self.cfg.mc_clock_sync:
            # force=True: on connect we may well have just power-cycled the node,
            # so set the clock regardless of what it claims.
            first = await self._sync_meshcore_clock(force=True)
            # Keep a reference: asyncio only holds a weak one, so a bare
            # create_task() can be garbage-collected mid-flight. The first result
            # is handed on so a failed startup sync retries in seconds, not hours.
            self._clock_task = asyncio.create_task(self._clock_sync_loop(first))
        print("bridge up:", self.cfg.direction, flush=True)
        # Keep the asyncio loop alive; both sides feed callbacks/tasks into it.
        while True:
            await asyncio.sleep(3600)

    # ---- MeshCore side ----

    async def _connect_meshcore(self) -> None:
        from meshcore import MeshCore
        self.mc = await MeshCore.create_serial(self.cfg.mc_port, self.cfg.mc_baud)
        if self.mc is None:
            sys.exit(f"MeshCore node not responding on {self.cfg.mc_port}")
        # TODO(hardware): subscribe to CHANNEL message events and route to on_meshcore_text.
        # The meshcore lib exposes a dispatcher/subscribe API (EventType.CHANNEL_MSG_RECV);
        # wire it so each inbound channel message on cfg.mc_channel calls:
        #     self.on_meshcore_text(text)
        # Confirm the exact EventType + payload key names against the installed version.

    async def on_meshcore_text(self, text: str, sender_key: str | None = None, is_direct: bool = False) -> None:
        if self.is_command(text):
            if not self.is_authorized("meshcore", sender_key, is_direct):
                print(f"MC cmd REJECTED (unauthorized sender={sender_key} direct={is_direct})", flush=True)
                return
            reply = self.handle_command(text, origin="meshcore")
            # TODO(hardware): reply privately to the asker as a DM to sender_key
            #     await self.mc.commands.send_msg(sender_key, reply)
            print(f"MC cmd({sender_key}) -> {reply}", flush=True)
            return
        if not self.cfg.mc_to_mt:
            return
        if self.dedup.seen_recently("mc:" + text):
            return
        out = self.cfg.mt_prefix + text
        # Relay onto Meshtastic (synchronous call; safe from the loop thread).
        self.mt.sendText(out, channelIndex=self.cfg.mt_channel)
        print(f"MC -> MT: {out}", flush=True)

    # ---- Meshtastic side ----

    def _connect_meshtastic(self) -> None:
        import meshtastic.serial_interface
        from pubsub import pub
        self.mt = meshtastic.serial_interface.SerialInterface(devPath=self.cfg.mt_port)

        def on_receive(packet, interface):  # runs on meshtastic's thread
            try:
                decoded = packet.get("decoded", {})
                if decoded.get("portnum") != "TEXT_MESSAGE_APP":
                    return
                text = decoded.get("text") or decoded.get("payload", b"").decode("utf-8", "ignore")
                if text and self.loop is not None:
                    self.loop.call_soon_threadsafe(
                        lambda: asyncio.create_task(self.on_meshtastic_text(text))
                    )
            except Exception as exc:  # never let a bad packet kill the callback
                print(f"meshtastic rx error: {exc}", file=sys.stderr, flush=True)

        pub.subscribe(on_receive, "meshtastic.receive")

    async def on_meshtastic_text(self, text: str, sender_id: str | None = None, is_direct: bool = False) -> None:
        if self.is_command(text):
            if not self.is_authorized("meshtastic", sender_id, is_direct):
                print(f"MT cmd REJECTED (unauthorized sender={sender_id} direct={is_direct})", flush=True)
                return
            reply = self.handle_command(text, origin="meshtastic")
            # TODO(hardware): reply privately as a DM to sender_id
            #     self.mt.sendText(reply, destinationId=sender_id)
            print(f"MT cmd({sender_id}) -> {reply}", flush=True)
            return
        if not self.cfg.mt_to_mc:
            return
        if self.dedup.seen_recently("mt:" + text):
            return
        out = self.cfg.mc_prefix + text
        # TODO(hardware): send a channel message on the MeshCore side, e.g.
        #     await self.mc.commands.send_chan_msg(self.cfg.mc_channel, out)
        # Confirm the method name/signature against the installed meshcore version.
        print(f"MT -> MC: {out}", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser(description="MeshCore <-> Meshtastic bridge")
    ap.add_argument("--config", default="config.toml", help="path to config.toml")
    args = ap.parse_args()
    cfg = Config.load(args.config)
    try:
        asyncio.run(Bridge(cfg).run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
