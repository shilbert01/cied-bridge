# Architecture & Terminology — Consolidation Blueprint

Status: agreed vocabulary + target shape. Naming is **locked**; the mechanical
rename/merge of existing subprojects is a follow-up (see §8). This document is the
source of truth for how the pieces fit together and what they are called.

---

## 1. Vocabulary (the three nouns)

Everything in the system is one of three things. Historic code used ad-hoc,
directional names (`import-relay`, `export-relay`) that proved ambiguous; this is
the consistent replacement.

| Noun | Definition |
|------|------------|
| **Relay** | An edge device with a *fake USB personality* that bridges a physical USB port to a **hub** over WiFi. |
| **Hub** | A Pi3-or-equivalent that serves relays: a durable queue plus one or more web UIs. |
| **Extraction service** | Parses interrogation **data files** into structured results — a more capable parallel to KardiSynch's own parsers. LLM-backed. |

Supporting names (not roles):
- **bigboy** — a *hostname*, the server that currently hosts the extraction
  service (because it uses LLM inference). Not a role; the non-LLM parts of the
  service could run elsewhere, including on a hub.
- **KardiSynch** — the customer's data-file application (its parsers are what the
  extraction service augments/replaces).
- **CIED Central** — the **data hub's** management web UI.
- **CIED Extract** — today's implementation of the **extraction service**.

---

## 2. The Relay

A relay is characterised by two axes plus its hardware.

- **Side** — which way it faces:
  - `ingest` — faces a **cardiac programmer** (captures what the programmer emits)
  - `delivery` — faces a **customer PC** (presents data for the PC to consume)
- **Personality** — the USB device it pretends to be:
  - `printer` — a USB laser printer (captures/deliver print jobs)
  - `storage` — a USB mass-storage device (files written to it, or read from it)
  - `hid` — a vendor HID pipe (for PCs that block storage; see `kardisynch-receiver`)
- **Hardware**:
  - **ESP32-S3** — `printer` personality only.
  - **Pi Zero W** — any personality, and *composite* (several at once, e.g.
    `printer` + `storage` simultaneously via multiple configfs functions).

A relay always talks to exactly one hub, over WiFi, via the shared hub client
(pull for `delivery`, push/`ingest-export` for `ingest`). It never talks to
another relay (hub-and-spoke).

**Canonical relay roles** (`RELAY_ROLE`):

| Role | Side · Personality | Replaces today's |
|------|--------------------|------------------|
| `ingest-printer`   | ingest · printer  | `printer-relay` |
| `ingest-storage`   | ingest · storage  | `export-relay` |
| `delivery-storage` | delivery · storage| `import-relay` |
| `delivery-hid`     | delivery · hid    | `kardisynch-receiver` |

`delivery-storage` / `delivery-hid` say exactly what they do — retiring the
`import`/`export` directional names that were a persistent source of confusion.

---

## 3. The Hub

A hub is a Pi3 (or equivalent) that serves relays. It is one codebase whose roles
are selected by config.

| Hub role | Attached / workflow | Web UIs it exposes |
|----------|---------------------|--------------------|
| `printer` | a physical printer; the print workflow | printer-management UI |
| `data`    | the file-based workflow | CIED Central UI + annotation/training UI |

### Roles are composable, not exclusive

`HUB_ROLE` is a **set of independent toggles**, not a single-value enum: a hub may
enable `printer`, `data`, or **both**. This is modelled on the existing feature
manifest (`clinic.conf` flags), and it mirrors relays, which are themselves
composable (a Pi Zero can be `printer` + `storage` at once). Same codebase deploys
every combination — only the toggles differ.

**Shared when any role is on:** the AP (hostapd / `relayap`), the durable
submit/claim/fetch/ack **queue** (`import-server.py` today, atomic leased claims +
redelivery), and node/relay management. Relays push to (`ingest`) or pull from
(`delivery`) the queue matching their own role; multiple queues coexist on one hub.

**Added per role:**
- `printer` → CUPS + the printer + the printer-management UI.
- `data` → `/ingest-export`, `/data/exports`, the KardiSynch outbox + feeder,
  the CIED Central + annotation/training UIs, and the extraction-service client.

### When to combine vs. split

- **Small site → one combined hub.** A clinic that both prints and runs the file
  workflow uses a single box with both roles: one AP, less hardware, simpler.
- **Larger site → split by role onto separate boxes.** Reasons to split:
  - **Resource** — the `data` role (CIED Central + annotation/training UI,
    non-LLM extraction) is heavier than `printer`; a Pi3 co-hosting both is fine at
    small volume, but at scale give `data` its own (beefier) box.
  - **WiFi coordination** — each hub is its own AP. Two *separate* hubs in the same
    area must not collide on SSID/channel; a combined hub sidesteps this, split
    deployments need distinct SSIDs/channels.
  - **Failure isolation** — splitting keeps a print-side fault from taking down the
    data workflow, and vice-versa.

**Default:** composable; a single combined hub for small sites, split by role for
scale or isolation.

The currently deployed Pi3 (`10.99.0.1`) has only the `printer` role enabled (cups
+ hostapd + the queue).

---

## 4. The Extraction Service

Parses data files (`.hl7 .xml .bnk .pdd .pkg …`) into structured results. It is a
**called component**, not baked into a hub:
- LLM-backed parts run on **bigboy** (today: **CIED Extract**).
- Non-LLM parts could run anywhere, including on a data hub.
- It supplements/replaces KardiSynch's built-in parsers where more capability is
  needed.

The **data hub** invokes it; it is never a relay or a hub itself.

---

## 5. The two workflows

There are **two independent pathways** from the programmer. They do not mix: a
print is not a data file, and KardiSynch consumes only data files.

```
PROGRAMMER  (two USB personalities → two relays, or one composite relay)
│
├─ (A) PRINT WORKFLOW ─────────────────────────────► printer hub → CIED Central
│      relay role = ingest-printer  (ESP32-S3 or Pi Zero W)
│        · programmer "prints"; relay captures the job
│        · forwards to the printer hub (queue) over WiFi
│        · hub prints it and/or renders a PDF for the print/PDF system
│        ✗ never reaches KardiSynch (prints aren't parseable data files)
│
└─ (B) DATA WORKFLOW ──────────────────────────────► data hub → KardiSynch
       relay role = ingest-storage  (Pi Zero W mass-storage)
         · programmer "exports"; writes .hl7/.xml/.bnk/.pdd/.pkg onto the drive
         · relay detects new files, forwards to the data hub (/ingest-export)
         · data hub stores them (/data/exports) and mirrors the KardiSynch-
           parseable set into an outbox (/data/kardisynch-outbox)
         · extraction service may parse them (CIED Extract on bigboy) — more capable
           than KardiSynch's own parsers
         │
         │  DELIVERY to a customer PC that can't reach the data hub on the network:
         ▼
   /data/kardisynch-outbox
         │  outbox-to-hub feeder → POST to the hub queue
         ▼
   hub queue  ◄── claim/fetch/ack ── DELIVERY RELAY (Pi Zero W)
         │                              role = delivery-storage (PC reads a fake USB drive)
         │                                  OR delivery-hid      (locked-down PC; HID + WebHID page)
         ▼
   customer PC → KardiSynch watch folder → KardiSynch imports
```

The **delivery half (B, bottom)** — queue → delivery relay → PC → KardiSynch — is
validated end-to-end (Aug 2026) for `delivery-hid` (HID + the no-install WebHID
companion), byte-perfect.

---

## 6. Current code → consolidated roles

| Today | Consolidated identity |
|-------|-----------------------|
| `printer-relay/` (Pi Zero + ESP32) | relay · `RELAY_ROLE=ingest-printer` |
| `export-relay/` | relay · `RELAY_ROLE=ingest-storage` |
| `import-relay/` | relay · `RELAY_ROLE=delivery-storage` |
| `kardisynch-receiver/` | relay · `RELAY_ROLE=delivery-hid` |
| `printer-relay/pi3-server/`, `printer-config-server.py` | hub · `HUB_ROLE=printer` |
| `central-server/` (CIED Central) | hub · `HUB_ROLE=data` |
| `import-relay/pi3-server/import-server.py` | hub **queue** component (shared) |
| `import-relay/daemon/hub_client.py` | shared relay↔hub client |
| CIED Extract @ bigboy | extraction service |
| `configure-clinic.sh` / `feature-lib.sh` / `clinic.conf` | hub role/feature selection (seed of `HUB_ROLE`) |

---

## 7. Consolidation targets

1. **One relay codebase, role-selected.** The four relay trees share nearly
   everything (configfs gadget, WiFi, `hub_client.py`, systemd, status LED/OLED).
   Collapse into one tree with `RELAY_ROLE` selecting side + personality; support
   *composite* Pi Zero relays (printer + storage together).
2. **One hub codebase, role-selected** (`HUB_ROLE=printer|data`, or both), built on
   the shared queue + the feature-manifest selection already present.
3. **Extraction service as a pluggable called component** (bigboy for LLM;
   elsewhere for non-LLM), invoked by the data hub.
4. **Retire directional names** `import`/`export` in favour of
   `delivery-*`/`ingest-*`.

---

## 8. Naming decisions (locked)

- Top-level nouns: **relay**, **hub**, **extraction service**. (Kept "relay" over
  "bridge".)
- Relay roles: **`side-personality`** — `ingest-printer`, `ingest-storage`,
  `delivery-storage`, `delivery-hid`. `import`/`export` names are retired.
- Hub roles: **`printer`**, **`data`** — **composable** (independent toggles, a hub
  may enable both), not a single-value enum. See §3.
- `bigboy` is a host, not a role; the role it fills is **extraction service**.

The mechanical rename/merge of the existing subprojects to these names is a
**follow-up task**, done non-breaking (keep the current deploys working; introduce
`RELAY_ROLE`/`HUB_ROLE` and migrate one subproject at a time).

---

## 9. Out of scope — the rollout toolset (separate project)

A provisioning UI that captures per-customer configuration + customization and
rolls out relay/hub images. It is the natural evolution of the setup shell scripts
+ feature manifest, and is tracked as its **own future project**.

**Prerequisites** (why it waits on this doc): the toolset is only tractable once
"a relay" and "a hub" are each *one configurable thing* (§7.1, §7.2) rather than
four/two divergent trees. Consolidation first, rollout tooling second.
