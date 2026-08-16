# MeshCore ⇄ Meshtastic Raspberry Pi Bridge

A small Raspberry Pi service that relays text messages between a **MeshCore**
mesh and a **Meshtastic** mesh.

MeshCore and Meshtastic both use LoRa in the same band, but they speak different
protocols and **do not talk to each other over the air**. The only place the two
networks can meet is a host that runs one node of each and passes messages
across in software. This is that host.

```
   MeshCore mesh                          Meshtastic mesh
        │                                       │
   ( LoRa )                                 ( LoRa )
        │                                       │
  ┌─────────────┐   USB              USB  ┌─────────────┐
  │ MeshCore    │────────┐      ┌─────────│ Meshtastic  │
  │ companion   │        │      │         │ node        │
  └─────────────┘        ▼      ▼         └─────────────┘
                    ┌───────────────────┐
                    │   Raspberry Pi    │
                    │     bridge.py     │
                    └───────────────────┘
```

## Hardware

- A Raspberry Pi (any model with two free USB ports; a Pi Zero 2 W is plenty —
  the bridge is I/O-bound and mostly idle).
- One **MeshCore** node running *companion* firmware, on USB.
- One **Meshtastic** node, on USB.
- Two antennas with **as much physical separation as practical.** Two ~900 MHz
  LoRa transmitters inches apart will desense each other — when one transmits it
  swamps the other's receiver. Separate the antennas or accept reduced range.

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
After=network.target

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

**Scaffold.** The architecture, config, dedup/loop-prevention, and both
connection paths are in place. Two `# TODO(hardware)` spots in `bridge.py` — the
exact inbound-event subscription on each side — need a real MeshCore node and a
real Meshtastic node on the same Pi to finish and verify, because the precise
event/packet field names vary by library version. Contributions welcome.

## Security

Keep channel keys and admin passwords out of git. `config.toml` is gitignored;
only `config.example.toml` (with placeholders) is tracked.

## License

Apache-2.0. See [LICENSE](LICENSE).

Not affiliated with the MeshCore or Meshtastic projects.

## Credits

CryptoJones and Fable5.
