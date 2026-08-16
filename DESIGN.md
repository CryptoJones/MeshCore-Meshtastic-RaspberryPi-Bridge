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
- [ ] **Finish the two `# TODO(hardware)` inbound wirings** (needs a real node of
      each) so commands and relay run on hardware.
- [ ] **Per-node history** (Phase 2, multiple repeaters): `!bridge <metric> <node> <window>`.

Proudly Made in Nebraska. Go Big Red! 🌽 https://xkcd.com/2347/
