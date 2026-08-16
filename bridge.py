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
import sys
import time
import tomllib
from dataclasses import dataclass, field


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
            return "bridge commands: status | uptime | ping | help"
        if cmd == "ping":
            return "pong"
        if cmd == "uptime":
            secs = int(time.monotonic() - self.started_at)
            return f"bridge up {secs // 3600}h {secs % 3600 // 60}m"
        if cmd == "status":
            return (f"bridge OK · direction={self.cfg.direction} · "
                    f"MeshCore ch{self.cfg.mc_channel} · Meshtastic ch{self.cfg.mt_channel}")
        return f"unknown command '{cmd}' — try: {self.cfg.command_prefix} help"

    async def run(self) -> None:
        self.loop = asyncio.get_running_loop()
        await self._connect_meshcore()
        self._connect_meshtastic()
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
