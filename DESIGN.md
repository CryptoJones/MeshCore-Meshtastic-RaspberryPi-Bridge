# Design notes & roadmap

## Purpose

**The point is off-grid community messaging** — enabling a town to communicate
independent of the cell network and internet. Emergency/disaster resilience is
one benefit (and the wedge for pitching a city on hosting infrastructure), but it
is not the mission. Everything here serves *carrier-free community
communication*; data collection is instrumentation to prove a pilot, not the
product.

## Design decisions

### DD-001 — Purpose is community messaging, not emergency comms

The system exists to let a community message itself off-grid. Emergency comms is
*one* use case and a persuasive pitch to a city (it justifies a siren-pole mount
and unlocks preparedness grants), but framing the project as "emergency comms"
undersells it and misstates the goal. Documentation and product decisions lead
with community messaging; emergency resilience is presented as a benefit.
*(CryptoJones, 2026-08-16.)*

### DD-002 — The bridge is the historian; the repeater has no memory

A MeshCore repeater answers a status request with an **instantaneous snapshot**
only — it stores no history. Therefore historical data must be stored by the
bridge/Pi: poll the node on a schedule and persist each timestamped reading to a
local **SQLite** database (the Pi supplies wall-clock time; the node reports
uptime, not date). Outages are stored as `reachable=0` rows so gaps are real
data. This makes the Pi the single source of truth for time-series.

### DD-003 — Over the air: simple summaries only

A LoRa message is a few hundred bytes, rate-limited, and slow. Bulk history
**cannot** move over the mesh, and we are not going to try. Over-the-air history
is **just a simple summary** that fits in one message — a few aggregate numbers
over a window (e.g. average/min battery, % reachable, packet totals). No raw
rows, no time-series, no pagination. One question, one short answer.

The **complete dataset lives on the Pi** and is pulled off it directly (USB /
SSH / a small local web endpoint on the home network) for the actual reports and
charts. The mesh side is for quick "how's the pole doing?" summaries from
anywhere in radio range; the Pi is for everything detailed.

### DD-004 — Greenfield deployment is MeshCore-only; bridging goes dormant

Minden is starting from zero with **no existing Meshtastic population**. Bridging
two protocols is therefore solving a problem we do not have, while carrying the
two most expensive parts of the build: a second radio colocated with the first
(which forces a ~10 ft vertical antenna stack to avoid desense — see the roadmap
item below) and a second firmware ecosystem to track.

So: deploy **MeshCore-only**. The Pi stays — it is still the historian (DD-002)
and the authenticated command endpoint, which is the actual reason this project
exists and which no repeater can do for itself. What changes is that it hosts
**one** node instead of two.

The bridge code is kept, not deleted. It is written, and the Meshtastic side is
already wired; if a Meshtastic population appears later, enabling it should be a
config change rather than a rewrite. To make that real, the Meshtastic side must
become **optional at startup** — absent a `[meshtastic]` port, the bridge runs
single-mesh instead of failing.

A useful side effect: the two open `TODO(hardware)` items are both on the
MeshCore side, so finishing them now needs **one** node on the bench, not two.
Development is unblocked immediately.

**"Dormant" describes the deployment, not the development.** Finishing and
testing the relay remains an active goal in its own right — it is an interesting
problem and worth building for its own sake. What DD-004 says is only that the
Minden pilot does not *depend* on it, which means the relay can be built and
exercised on the bench without holding up anything the city is waiting on. Those
are good conditions for a side project, not a shelving.
*(CryptoJones, 2026-08-17.)*

### DD-005 — Two sites: solar repeater on the pole, mains historian in a window

The historian does **not** live on the siren pole. It sits indoors in a window on
continuous AC USB power and reaches the pole repeater **over the mesh**. Two
distinct sites:

| Site | Hardware | Power | Role |
|------|----------|-------|------|
| Siren pole | MeshCore repeater, nRF52840 class | Solar + 18650 | Repeat traffic. No memory (DD-002). |
| Window | Raspberry Pi + one MeshCore companion node | Mains, AC USB | Historian + authenticated command endpoint. |

This is what the pilot's promise actually requires: evaluation data gathered
**without a second install on the pole**. The Pi polls the repeater on a
schedule, timestamps each reading with its own wall clock, and persists to
SQLite. Anything the pole cannot remember, the window remembers for it.

Three consequences:

1. **No solar budget for the Pi.** Mains removes the hardest power problem in the
   project, and the solar-host roadmap item is dropped.
2. **The historian sits on the home LAN**, which makes DD-003's out-of-band bulk
   export trivial — a small local HTTP endpoint or `scp` is directly reachable,
   no mesh involved.
3. **Radio placement becomes the risk instead.** An indoor node is behind glass,
   and modern Low-E / metallized coatings attenuate 900 MHz severely (tens of dB
   is realistic). The Pi belongs indoors; its **antenna does not**. Plan on an
   external or at-the-glass antenna with a short coax run, and verify link margin
   to the pole before trusting the polling loop. *(CryptoJones, 2026-08-17.)*

### DD-006 — Two hardware standards: a hardened pole unit, cheap coverage nodes

Not every site gets the same node. The pole and the neighbourhood are different
problems and get different hardware.

**Pole standard — SenseCAP Solar Node P1-Pro (~$150).** Sealed, rated, and ships
pre-flashed with MeshCore repeater firmware, so there is nothing to assemble and
nothing bespoke to defend in a city facilities review. This is the one node where
reliability beats cost, for four reasons:

1. **Cold-weather charging.** Minden sees January lows near −10 °C, and lithium
   cells cannot be charged below 0 °C without plating — permanent capacity loss
   and a safety issue. The cheap TP4056-class controllers in garden-light donors
   have no low-temperature cutoff, so across a Nebraska winter such a node would
   keep trying to charge a frozen cell and quietly destroy it. This is a
   functional failure mode over exactly the season the pilot must survive.
2. **The battery is what's under test.** A donor-light pack is small by the
   author's own account. Since the pilot's evaluation *is* uptime and battery
   through the season, an undersized pack makes the pilot fail for reasons
   unrelated to whether community mesh works — and the data can't separate those
   causes.
3. **Access cost inverts the economics.** A siren pole means city coordination
   and possibly a bucket truck. One service visit costs far more than the ~$115
   saved. The least-accessible node in the network should be the most reliable.
4. **Approval optics.** We are asking a city to host equipment on emergency
   infrastructure. Off-the-shelf rated hardware clears a facilities or risk
   review in a way a gutted garden light does not, regardless of how well it
   works.

**Coverage standard — the $35 People's Repeater.** Everywhere accessible:
neighbours' roofs, fence posts, sheds. Cheap, replaceable, and hot-swappable.
Density is what makes the network useful to a town, and cost per node is the
limit on density.

**Also build one $35 unit immediately**, regardless of the pole decision — it is
the cheapest possible bench unit for validating firmware, the polling loop, and
the historian end-to-end before anything goes up a pole, and it becomes a
coverage node afterwards. *(CryptoJones, 2026-08-17.)*

### DD-007 — The donation is one node; the Pi is personal instrumentation

Scope boundary, stated explicitly because it settles both the ownership and the
privacy question:

| Asset | Owner | Network | Role |
|-------|-------|---------|------|
| SenseCAP P1-Pro on the siren pole | **Donated to the City** | MeshCore | Public repeater |
| Pi + MeshCore companion, in a window | **Personal** | MeshCore | Historian, `!bridge` commands |
| Pi + Meshtastic node, in a window | **Personal** | Meshtastic | RX-only collector |

**Only the pole node is donated.** The Pi is the author's own instrumentation on
his own power in his own window. It is not city infrastructure, not a pilot
deliverable, and not something the city is asked to host, maintain, or trust.
That keeps the civic footprint to a single sealed node with nothing to
administer — the easiest possible thing for a city to say yes to.

**The Meshtastic node is receive-only, and it is a survey instrument.** DD-004
rests on "no Meshtastic population here," which is currently a belief. A silent
Meshtastic receiver *measures* that claim continuously and produces a dated
trigger: if Meshtastic nodes begin appearing in the log, that is when bridging
stops being dormant. Proof that the decision is still correct is worth more in a
pilot report than an assertion that it was correct once.

Two consequences:

1. **No mutual desense.** The earlier ~10 ft vertical separation figure assumed
   *two transmitters* blinding each other. A receive-only node never keys up, so
   the only impairment is the MeshCore companion briefly stomping the Meshtastic
   receiver while it transmits — a small packet loss at a low duty cycle, and
   acceptable for monitoring. Colocating both radios in one window is fine; a
   couple of feet is plenty. The 10 ft spec remains filed against a future
   dual-*repeater* site, which is the only place it was ever load-bearing.
2. **`bridge.py` needs an `observe` direction.** Connect, receive, log, never
   transmit, never relay. Smaller than the "make Meshtastic optional" task in
   DD-004 and it puts the already-wired Meshtastic RX path to work immediately.

**Retention default:** 90-day rolling window, non-infrastructure node IDs
salted-and-hashed at write time. The immediate stakes are low — LongFast is a
public default channel and there is no local population to observe — but a
retention policy is far cheaper to set now than to retrofit, and the project's
credibility rests on community trust (DD-001). Revisit if a real population
appears. *(CryptoJones, 2026-08-17.)*

## Roadmap

- [ ] **Historian storage** — SQLite time-series; a built-in poller (or reuse the
      pilot's `poll-repeater.py`) writing timestamped rows incl. `reachable`.
- [ ] **Summary commands** (authenticated, either mesh, reply to asker) — each a
      single one-message summary, nothing more:
      - `!bridge summary <window>` — battery avg/min, % reachable, packet totals
      - `!bridge uptime <window>` — availability % + outage count
- [ ] **Out-of-band bulk export** — a small local HTTP endpoint (home LAN) and/or
      documented `scp` of the SQLite/CSV for the real pilot report. All detailed
      data lives here, not on the mesh.
- [ ] **Finish the two `# TODO(hardware)` inbound wirings.** Both are on the
      MeshCore side, so under DD-004 this now needs **one** node on the bench,
      not one of each — unblocked immediately.
- [ ] **Add an `observe` direction mode** (DD-007) — connect, receive, log,
      never transmit, never relay. This is what the window's Meshtastic node
      actually runs. Needs: a `direction = "observe"` value, `on_meshtastic_text`
      short-circuiting to the logger before any relay path, and no MeshCore
      transmit calls on that route.
- [ ] **Make the Meshtastic side optional at startup** (DD-004). Absent a
      `[meshtastic]` port, run single-mesh instead of failing: `Config.load`
      must tolerate a missing `[meshtastic]` table, `run()` must skip
      `_connect_meshtastic()`, and `on_meshcore_text` must guard the relay call
      on `self.mt` being present.
- [ ] **Passive observation tables** (DD-007) — `packets` (ts, type, sender,
      SNR, RSSI, hops) and `nodes` (first_seen, last_seen, name, role) kept
      separate from the active `polls` readings of DD-002, since their
      provenance differs. Salt-and-hash non-infrastructure sender IDs at write
      time; 90-day rolling retention.
- [ ] **SQLite on a USB SSD, not the SD card.** Continuous packet logging is the
      classic way to kill an always-on Pi. WAL mode, batched commits,
      `/var/log` on tmpfs.
- [ ] **Per-node history** (Phase 2, multiple repeaters): `!bridge <metric> <node> <window>`.

## Field notes — the "People's Repeater" low-cost solar node

External prior art worth borrowing from for the *coverage* nodes around a bridge
site (not for the bridge host itself, which needs a real computer). From Black
Flag Civilian's build, ["A $35 Solar Mesh Node Anyone Can
Build"](https://www.youtube.com/watch?v=yAmINEghCOc) (2026-07-01); STL files
released free by the author.

**The design.** A solar garden light is gutted and used as the donor for the
panel, the weatherproof enclosure, and the 18650 cell. To that you add a LoRa
board, a small solar charge controller, an SMA bulkhead with a coax pigtail, and
a 3D-printed hub that mates the panel housing to the antenna and keeps water
out. Roughly **$35 all-in** versus $60+ for turn-key solar nodes. The radio is a
**Seeed XIAO nRF52840** — same nRF52840/SX1262 class we already favour for
low-idle-current repeaters, and the same XIAO footprint as our own boards.

**Assembly details worth carrying over:**

- **Attach the antenna before applying power.** Powering a LoRa PA into no load
  risks the radio. This belongs in our own build docs too.
- **Button-top 18650s only** — flat-tops don't seat in the donor compartment.
  The cell is hot-swappable in seconds, so capacity can be upgraded for low-sun
  stretches without disturbing anything else.
- Mounting provisions for both flat surfaces (two screws) and masts/flagpoles
  (two zip ties) — the latter matches our siren-pole case.
- Panel tilt and rotation adjust via a single thumbscrew; the panel should rotate
  freely ~90° without unseating.

**Reported field results.** Strong links past 2 miles from a mast at a modest
12–15 ft above ground on elevated terrain; the author's group reports 100%
message success across 20+ miles on a MeshCore network of ten such nodes.
Treat these as the author's figures, not measurements we have reproduced —
independent verification is exactly what the pilot's historian is for.

### Roadmap items this raises

- [ ] **Adopt the $35 node as the standard coverage node** (DD-006). Build one
      now as the bench unit for firmware, polling-loop, and historian validation
      before anything goes up the pole.
- [x] ~~**Solar-host variant of the bridge.**~~ Dropped under DD-005 — the
      historian runs on mains in a window, so it needs no solar budget at all.
- [ ] **Antenna separation spec** — *deferred under DD-007, required before any
      dual-**transmitter** site.* The window is exempt: its Meshtastic node is
      receive-only, so there is no mutual desense (DD-007). A bridge hosts two ~900 MHz transmitters in one cabinet,
      tethered together by USB, so they cannot be sited apart. At 915 MHz, ~10 ft
      of *vertical* separation buys ≈67 dB of isolation; the same figure needs
      ~191 ft horizontally. Stack, never side-by-side. Replace the README's
      current "as much separation as practical" with the real number if and when
      bridging is switched on.

Proudly Made in Nebraska. Go Big Red! 🌽 https://xkcd.com/2347/
