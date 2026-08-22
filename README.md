# MeshCore ⇄ Meshtastic Raspberry Pi Bridge

A small Raspberry Pi service that bridges a **MeshCore** mesh and a **Meshtastic**
mesh — with an **authenticated command interface** as its headline feature: you
can query and control the bridge from either network, and it only obeys
allow-listed senders over a direct message.

MeshCore and Meshtastic both use LoRa in the same band but speak different
protocols and **do not talk to each other over the air**. A host running one node
of each can pass messages across in software. Relaying between the two is
[already solved](#prior-art) — what this project focuses on is the piece the
existing relays don't have: **authenticated, per-user command and control.**

> **Just want a simple relay?** Use
> **[Akita Engineering's Meshtastic⇄Meshcore Bridge](https://github.com/AkitaEngineering/Akita-Meshtastic-Meshcore-Bridge)**
> — it's a mature, full-featured relay. This project is for when you also want to
> *command and control* the bridge from the mesh, with authenticated users.

## Background — built for the Minden, Nebraska MeshCore pilot

This grew out of a real project: a solar-powered **MeshCore** relay proposed for
a City of Minden, Nebraska tornado-siren pole
([Minden-MeshCore-Pilot](https://github.com/CryptoJones/Minden-MeshCore-Pilot)).
The **point of that pilot is off-grid community messaging** — a town that can
talk to itself independent of the cell network and internet. Emergency and
disaster resilience is one compelling benefit (and the wedge for pitching a
city), but the mission is everyday carrier-free communication for the community.

To earn permanence, the pilot has to prove itself over a fixed term with
**measured data** — uptime, battery through the season, traffic, and above all
whether the relay is reachable — gathered **without climbing the pole**. The
**authenticated command interface** solves that: an authorized node queries the
rooftop repeater over the mesh (`!bridge status`, uptime, packet counts) and
logs the answers, so the pilot's evaluation data collects itself — hands-off, no
second roof install, and only authorized users can issue those commands. The
MeshCore↔Meshtastic bridging came along because the same Pi can host a node of
each and the two mesh communities shouldn't be siloed. See
[DESIGN.md](DESIGN.md) for the full rationale and roadmap.

Reference deployment (DD-007) — the MeshCore side talks; the Meshtastic side only
listens:

```
   MeshCore mesh                     Meshtastic airspace
   (pole repeater)                   (no local population)
         ▲                                  │
         │ ( LoRa )  TX + RX                │ ( LoRa )  RX only
         ▼                                  ▼
  ┌─────────────┐                    ┌─────────────┐
  │ MeshCore    │                    │ Meshtastic  │
  │ companion   │                    │   observe   │
  └──────┬──────┘                    └──────┬──────┘
         │ USB                          USB │
         └──────────────┐        ┌──────────┘
                        ▼        ▼
                 ┌───────────────────────┐
                 │      Raspberry Pi     │
                 │        bridge.py      │
                 │  historian + commands │
                 └───────────────────────┘
```

Relaying between the two meshes is **off** in this configuration — there is no
Meshtastic population to relay to. The code remains, and enabling it is a
`direction` change if that ever changes.

## Prior art

Plain message relaying between MeshCore and Meshtastic is a solved problem, and
if a relay is all you need, use one of these rather than this project:

- **[Akita-Meshtastic-Meshcore-Bridge](https://github.com/AkitaEngineering/Akita-Meshtastic-Meshcore-Bridge)**
  (Python, GPLv3) — a mature bidirectional bridge with MQTT, robust reconnection,
  rate limiting, TLS, a terminal dashboard, and a REST monitoring API.
- **[meshnard's MT↔MC relay](https://meshnard.com/mesh/mt-mc_relay)** — a simple
  public-channel relay with a message prefix and loop prevention.

**What this project adds** that those don't: an **authenticated command
interface** (below) — the bridge is addressable from either mesh, obeys commands
only from allow-listed senders over a direct message, and replies privately to
the requester. If you want a controllable bridge, not just a pipe, that is the
reason this exists. The relay itself here is deliberately minimal; for a
heavy-duty relay with MQTT/dashboards, prefer Akita.

## Hardware

- A Raspberry Pi (any model with two free USB ports; a Pi Zero 2 W is plenty —
  the bridge is I/O-bound and mostly idle). Mains powered.
- One **MeshCore** node running *companion* firmware, on USB.
- One **Meshtastic** node, on USB — **receive-only** in the reference deployment
  (see DD-007 in [DESIGN.md](DESIGN.md)).
- **Antenna separation depends on whether both radios transmit.**
  - *One transmitter* (the reference deployment: MeshCore companion + a
    receive-only Meshtastic node) — there is no mutual desense. The silent node
    cannot blind anything; it only loses the small fraction of packets that
    arrive while the MeshCore node is transmitting. A couple of feet is fine.
  - *Two transmitters* — two ~900 MHz transmitters inches apart will desense each
    other badly. At 915 MHz, **~10 ft of vertical separation** buys roughly 67 dB
    of isolation; matching that horizontally takes ~191 ft. **Stack them, never
    side by side.** Below ~3 ft, budget for a cavity or notch filter — ceramic and
    SAW filters do not provide enough in-band isolation.

## Setup

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp config.example.toml config.toml      # edit ports + channels (config.toml is gitignored)
python3 bridge.py --config config.toml
```

Use `/dev/serial/by-id/...` paths in the config, not `/dev/ttyACM0` — the by-id
names are stable, so a reboot can't swap which radio is which.

### Run it as a service

```ini
# /etc/systemd/system/mesh-bridge.service
[Unit]
Description=MeshCore <-> Meshtastic bridge
# time-sync.target matters: the bridge writes the Pi's clock onto the MeshCore
# node, and a Pi has no RTC. Starting before NTP has stepped the clock would
# push a stale boot time onto the node. The bridge guards against this itself,
# but ordering here means it does not have to wait and retry.
After=network-online.target time-sync.target
Wants=network-online.target

[Service]
WorkingDirectory=/home/pi/MeshCore-Meshtastic-RaspberryPi-Bridge
ExecStart=/home/pi/MeshCore-Meshtastic-RaspberryPi-Bridge/.venv/bin/python bridge.py --config config.toml
Restart=always
User=pi

[Install]
WantedBy=multi-user.target
```

Enable with `systemctl enable --now mesh-bridge` (as root).

## How it works

- Both client libraries are pure Python (`meshcore`, `meshtastic`). The bridge
  runs one asyncio loop; the synchronous Meshtastic callbacks hand packets into
  that loop thread-safely.
- Relayed messages are **prefixed** (configurable) so readers can tell a message
  came from the other network.
- A short **dedup window** prevents echo loops — a message the bridge just
  injected coming back and being relayed again.
- Direction is configurable: full two-way, or one-way either direction.

## Commands — the bridge answers, too

The bridge is not only a relay; it is **addressable from either mesh**. Send a
message beginning with the command prefix (default `!bridge`) on either network
and the bridge replies to you on the same network instead of relaying it:

```
!bridge status     -> bridge OK · direction=both · MeshCore ch0 · Meshtastic ch0
!bridge uptime     -> bridge up 3h 12m
!bridge ping       -> pong
!bridge help       -> bridge commands: status | uptime | ping | help
```

Add your own commands in `handle_command()` — it returns the reply string and
the bridge sends it back on the mesh the query came from.

### Command authentication (only authorized users)

Commands are privileged — you do not want anyone in radio range reconfiguring
the bridge. Authentication hangs off the one identity signal a mesh actually
provides:

- **Commands must be sent as a direct message.** A message on a *shared channel*
  carries no verified per-sender identity (the sender name is just typed text and
  can be faked), so channel commands are ignored outright.
- **The sender must be on an allow-list.** A direct message carries the sender's
  public key / node id; the bridge obeys a command only if that identity is in
  `authorized_meshcore_keys` / `authorized_meshtastic_ids` in the config.
- **Replies go privately** back to the requesting sender's DM, not to a channel.

Strength of the guarantee differs by network, and the config documents it:

- **MeshCore:** a DM is end-to-end encrypted to the bridge's key, so an
  allow-listed sender key is cryptographically authenticated. Strong.
- **Meshtastic:** trustworthy only with **PKC (public-key) direct messages**
  (firmware 2.5+). Legacy PSK DMs carry a spoofable node id — run PKC if you
  rely on Meshtastic-side command auth.

This identifies the authorized *node*, not a person — but you control the
allow-list, so only nodes whose keys you have added can command the bridge.

## Status

**Scaffold.** The command interface + authentication model, config, minimal
relay, and dedup/loop-prevention are in place. Two `# TODO(hardware)` spots in
`bridge.py` — the exact inbound-event subscription on each side — need a real
MeshCore node and a real Meshtastic node on the same Pi to finish and verify,
since the precise event/packet field names vary by library version. The
authenticated command control is the novel part and the priority; the relay is
intentionally thin (see [Prior art](#prior-art)). Contributions welcome.

## Security

Keep channel keys and admin passwords out of git. `config.toml` is gitignored;
only `config.example.toml` (with placeholders) is tracked.

## Acknowledgements

This project stands on prior art. It does **not** reuse their code (the relay
here is independently written, which is why this repo can be Apache-2.0), but
these projects solved MeshCore⇄Meshtastic relaying first and deserve the credit:

- **[Akita-Meshtastic-Meshcore-Bridge](https://github.com/AkitaEngineering/Akita-Meshtastic-Meshcore-Bridge)** by Akita Engineering — the mature bidirectional bridge (MQTT, dashboard, REST API).
- **[meshnard's MT↔MC relay](https://meshnard.com/mesh/mt-mc_relay)** — the simple public-channel relay with prefix + loop prevention.

## License

Apache-2.0. See [LICENSE](LICENSE).

Not affiliated with the MeshCore or Meshtastic projects.

## Credits

CryptoJones and Fable5.
