# RAUCH / TeeJet protocol research

This repository contains experimental protocol documentation, parsers, and
device emulators created while studying a RAUCH AXIS / TeeJet IC18 setup.
The project is organized by protocol so each tool can be used independently.

The reference machine used during this research is a **RAUCH AXIS 30.2 H**.
Other AXIS models probably use the same or a closely related WLAN protocol,
because the Android application represents the machine as an AXIS family and
exports a machine-family identifier rather than the exact model number. This
has not yet been verified on every AXIS variant.

## Contents

| Directory | Protocol | Status |
|---|---|---|
| [`protocols/rauch-wlan`](protocols/rauch-wlan/) | RAUCH Android WLAN: XF1 settings on TCP/8172 and weight on TCP/58200 | Both wire formats confirmed against the application |
| [`protocols/lhfs`](protocols/lhfs/) | TeeJet LHFS NV filesystem protocol | Listing, download, upload, overwrite, and delete confirmed |
| [`protocols/pex`](protocols/pex/) | TeeJet PEX firmware-loader protocol | Bootstrapping and transfer partially reconstructed |
| [`research`](research/) | Local reverse-engineering inputs | Ignored by Git; not intended for publication |

## Quick start

The tools require Python 3.10 or newer and only use the standard library.

RAUCH WLAN logger plus weight simulator:

```bash
python3 protocols/rauch-wlan/rauch_wlan.py \
  --host 152.21.0.31 --weight-kg 123.45
```

LHFS filesystem emulator:

```bash
python3 protocols/lhfs/lhfs_emulator.py
```

PEX Loader emulator:

```bash
python3 protocols/pex/pex_emulator.py
```

PEX container extractor:

```bash
python3 protocols/pex/pex_extract.py firmware.pex \
  --output protocols/pex/extracted_firmware
```

Run the regression suite:

```bash
python3 -m unittest discover -s tests -v
```

## Repository policy

Generated captures, received files, APKs, executables, DLLs, PEX images, and
extracted proprietary firmware are excluded by `.gitignore`. The documentation
and emulator source are suitable for a public repository; review any local
captures before overriding these rules.

## Accuracy and scope

This is an interoperability and research project, not an official RAUCH,
TeeJet, or Bogballe implementation. Field names taken from decompiled software
are retained where they help identify the on-wire format. Confirmed behavior is
separated from hypotheses in each protocol document.
