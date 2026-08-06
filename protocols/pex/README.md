# TeeJet PEX firmware-loader protocol

This directory contains an experimental emulator and extractor for the
protocol used by `TeeJetPexLoader.exe`. PEX is unrelated to LHFS framing.

The protocol was reconstructed from the 32-bit loader and live emulator
sessions. Discovery, Level 1, Level 2, and the beginning of application
programming are confirmed. A full successful flash session has not yet been
captured, so this is not a complete implementation specification.

## Tools

Start the loader emulator:

```bash
python3 protocols/pex/pex_emulator.py
python3 protocols/pex/pex_emulator.py --host 127.0.0.1 --port 11520 \
  --device-id a5
```

Extract a PEX container:

```bash
python3 protocols/pex/pex_extract.py firmware.pex \
  --output protocols/pex/extracted_firmware
```

Runtime captures are stored under `protocols/pex/captures/`. Extracted images
are stored under `protocols/pex/extracted_firmware/`. Both directories are
ignored by Git.

The extractor validates Intel HEX record types, type-specific lengths,
checksums, extended-address records, EOF presence, and trailing records after
EOF. Before writing a valid extraction, it removes stale `segment_*.bin` files
from the selected output directory while preserving unrelated files.

## Container structure

The analyzed IC18 PEX contains:

- a Level 1 bootstrap image (`LEVEL1/BIN_FILE` or path);
- a Level 2 flash loader (`LEVEL2/BIN_FILE` or path);
- the application as Intel HEX (`HEX_DATA`);
- flash-sector and DataStore erase metadata;
- product, version, platform, language, build, and checksum metadata.

The analyzed `iso_calibrator-Ver3.03.pex` described Bogballe ISOBUS Calibrator
3.03 for IC18. It contained a 32-byte Level 1 image, a 972-byte Level 2 image,
33,366 Intel HEX application records, and 1,050,651 programmed bytes across
122 contiguous address segments.

## Connection parameters

Testing used a serial-to-TCP bridge at `127.0.0.1:11520`. An observed INI file
specified COM1, 57,600 bit/s, FTDI latency timer 1 ms, and a 2-second discovery
timeout. The GUI also displayed 115,200 bit/s in some runs, so baud rate is a
runtime setting rather than a protocol constant.

## Transfer sequence

```text
PEX Loader                         device
    | 00  discovery poll              |
    |-------------------------------> |
    | A5, C5, or D5 device ID          |
    | <-------------------------------|
    | Level 1 image                    |
    |-------------------------------> |
    | 31 ('1')                         |
    | <-------------------------------|
    | Level 2 image, padded to 1024 B  |
    |-------------------------------> |
    | 32 ('2')                         |
    | <-------------------------------|
    | Level 2 erases flash             |
    | 46 ('F'), then status            |
    | <-------------------------------|
    | binary Intel HEX records         |
    |-------------------------------> |
    | one-byte status per record       |
    | <-------------------------------|
```

The loader polls with one `00` byte approximately every 125 ms. Valid bootstrap
identifiers are `A5`, `C5`, and `D5`; exact model mapping is unknown.

Level 1 is followed by ASCII `1` (`31`). Level 2 is followed by ASCII `2`
(`32`). The analyzed 972-byte Level 2 payload is padded with 52 zero bytes to
1024 bytes on the wire.

The emulator uses these confirmed fixed wire sizes as phase boundaries: 32 B
for Level 1 and 1024 B for Level 2. It does not interpret a temporary pause in
the TCP stream as the end of either stage. The legacy `--idle` option is kept
for command-line compatibility but no longer controls framing.

After Level 2 starts, erase completion appears to be ASCII `F` (`46`). Some
loader paths tolerate receiving a final `A` without observing `F`. The exact
erase/status state machine still requires a complete capture.

## Application records

Intel HEX text is converted to its binary record representation:

```text
length:u8 | address:u16_be | type:u8 | data[length] | checksum:u8
```

Example extended-linear-address record:

```text
02 00 00 04 00 3D BD
```

Type `04` changes the upper 16 address bits; it does not end the transfer.
Only record type `01` is EOF. Sector information and application HEX use the
same binary framing, so an emulator must preserve extended-address state.

Observed one-byte status interpretations include:

| Byte | Meaning |
|---:|---|
| `A` (`41`) | Success |
| `C` (`43`) | Intel HEX checksum error |
| `E` (`45`) | Invalid record type |
| `G` (`47`) | Success through a second state-machine path |
| `M` (`4D`) | Flash verification failed |
| `V` (`56`) | Flash programming failed |

Other responses in the `A`-`W` range remain unmapped.

## Current limitations

The emulator is suitable for observing Loader behavior and capturing each
stage. It is not a safe replacement bootloader and should not be used to flash
real machinery. Timing workarounds used by the emulator are experimental, and
the final erase/program/verify completion sequence is not fully confirmed.

See [`FIRMWARE_ANALYSIS.md`](FIRMWARE_ANALYSIS.md) for the IC18 application
image analysis and its relationship to RAUCH XF1 records.
