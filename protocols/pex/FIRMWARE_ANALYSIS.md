# IC18 firmware analysis and RAUCH XF1 correlation

## Objective

The analysis searched the Bogballe/TeeJet IC18 application image for a bridge
between the RAUCH WLAN XF1 fertilizer record and the ECU's internal spread
chart or NV filesystem representation.

## Inputs

- PEX container: Bogballe ISOBUS Calibrator 3.03 for IC18.
- Primary protocol samples: five 206-byte files captured from TCP/8172.
- Supplemental network capture: Android Wi-Fi traffic.
- APK: RAUCH Android application 05.16.00.

Local proprietary inputs and generated images are excluded from Git.

## Extracted application image

`pex_extract.py` validates every Intel HEX checksum, resolves extended linear
addresses, and writes contiguous memory segments plus a manifest. The analyzed
image contained 33,366 records, 1,050,651 programmed bytes, and 122 contiguous
segments.

The target is an Infineon XC167/C166-family processor. The available GNU
`objdump` did not include this architecture, so the current analysis relies on
memory mapping, strings, constants, and data tables. Reliable function-level
pseudocode requires a C166/XC167 processor module for Ghidra, IDA, or another
disassembler.

## Capture selection

The five raw TCP/8172 sessions are the primary source for XF1 correlation.
They include Canvil + Mg, duplicate urea records, clover seed, and slug pellets.

The separate PCAP did not contain TCP/8172 or an XF1 payload. It primarily
contained TLS traffic to RAUCH web services and five unanswered TCP SYN packets
to `152.21.0.31:58200`. It had no TLS key log, so encrypted application data
could not be recovered. It is not evidence for XF1 field mapping.

## Confirmed XF1 format

APK decompilation identified the 206-byte packet as an `xf1` export. The full
wire layout is documented in [`../rauch-wlan/README.md`](../rauch-wlan/README.md).
Important corrections established from the APK are:

- offset 4 is the content checksum, not a record ID;
- offset 8 is XF1 version 3;
- offset 14 is a one-byte machine family;
- `50/50` is mounting height code 3;
- Flow Factor at offset 176 is `uint32_le / 100`;
- `OptiWWK` at 186 is `uint16_le`;
- offset 201 is the spreading-disc key (`47` maps to `S 1`);
- offsets 202 and 204 are `MinSplit` and `MaxSplit`.

All five samples satisfy:

```text
stored_checksum == sum(frame[16:206]) & 0xffff
```

## Search for `FertChart.dat`

No ASCII or UTF-16 occurrence of the following was found in the reconstructed
application image:

```text
FertChart
FertChart.dat
Anwil
ORLEN
Canvil
WLAN
WiFi
Ethernet
socket
```

There were also no literal symbols named `WriteNV`, `CreateNV`, `OpenNV`,
`CloseNV`, `SaveNV`, or `UpdateNV`. Production code can remove or wrap symbol
names, so absence is not proof that the operations do not exist. It does mean
that the current image provides no direct named function for writing
`FertChart.dat`.

## NV filesystem evidence

The image contains generic file-layer diagnostics including:

```text
nv_file FileTable (%s) (%u)
hnd: %4i, dta: %08lX, ptr: %08lX, sz %5lu - %u: %s
```

The first string is near data address `0x000F3F67`. Other file names include
`aux2file.dat`, `@lh_info.xml`, `oem_info.xml`, and `tj_info.xml`. These prove
that the firmware has an NV file table and information-file parsing, but do not
link that layer to a complete fertilizer record.

## Closest firmware concept: `SChart`

Four operation-like identifiers were found:

| Data address | Identifier |
|---:|---|
| `0x0006C4BC` | `R:SChart` |
| `0x0006EDF1` | `S:SChart` |
| `0x0006FCCC` | `A:SChart` |
| `0x0006FF6A` | `W:SChart` |

Related diagnostics include:

```text
Rx: calibrator_dataChartCalibVal_ResSet() = %2.2f
Tx: calibrator_dataSetSpreadChartCalibVal(%2.2f)
```

This indicates a floating-point spread-chart calibration value. `R/S/A/W`
may denote read/set/answer/write operations, but that interpretation remains a
hypothesis until the dispatch table is disassembled. The strings refer to one
calibration value, not a fertilizer name, manufacturer, RPM set, or complete
XF1 record.

## Constant searches

Characteristic values such as 15000, 900, 350, 450, 64, and 0.84 produce many
ambiguous matches in a megabyte-scale embedded image. No candidate region was
found that combines those numbers with the XF1 string-field sizes or ordering.
No complete in-firmware structure matching the 190-byte XF1 record has been
identified.

## Conclusion

The Android-side XF1 structure is completely understood, but the analyzed IC18
application does not expose a matching record or a named `FertChart.dat`
writer. The strongest current evidence is:

1. the phone serializes a database-derived XF1 record and sends it to an
   external WLAN endpoint;
2. the app contains no LHFS implementation;
3. the IC18 image contains an NV filesystem and a smaller `SChart` calibration
   interface, but no recognizable XF1 serializer/parser;
4. therefore the WLAN module or another external terminal most likely converts
   XF1 into ECU-specific operations or storage.

This is an evidence-based inference, not yet a wire-level proof. The decisive
next experiment is to capture the WLAN-module-to-ECU connection during one XF1
import, or compare a complete LHFS NV filesystem snapshot immediately before
and after the import.
