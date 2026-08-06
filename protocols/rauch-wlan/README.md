# RAUCH WLAN application protocols

This document describes two TCP protocols recovered from RAUCH Android app
version `05.16.00` (`de.ms.neusta.rauch_streutabelle_android`) and confirmed in
live tests.

The reference spreader used for the tests is a **RAUCH AXIS 30.2 H**. The APK
encodes an AXIS machine-family value in XF1 rather than an exact model number,
so other AXIS spreaders are likely to use the same protocol and record layout.
Compatibility with each individual AXIS model remains to be confirmed.

## Connections

| Direction | Endpoint | Purpose |
|---|---|---|
| Android app -> terminal | `152.21.0.31:8172/TCP` | Export one 206-byte XF1 spreading-settings record |
| terminal -> Android app | `152.21.0.31:58200/TCP` | Stream the remaining hopper weight |

The two ports are independent. Neither protocol is LHFS.

## Running the logger and emulator

The script listens on both ports by default. It decodes incoming XF1 records
and can simultaneously emulate a weighing terminal:

```bash
python3 protocols/rauch-wlan/rauch_wlan.py \
  --host 152.21.0.31 --weight-kg 123.45
```

If the address is not configured on the host:

```bash
sudo ip address add 152.21.0.31/24 dev <interface>
```

Useful options:

```bash
python3 protocols/rauch-wlan/rauch_wlan.py --host 0.0.0.0
python3 protocols/rauch-wlan/rauch_wlan.py --port 8172
python3 protocols/rauch-wlan/rauch_wlan.py \
  --port 58200 --weight-kg 123.45 --weight-interval 0.25
python3 protocols/rauch-wlan/rauch_wlan.py --reply-hex 4f4b0d0a
```

Sessions are written under `protocols/rauch-wlan/captures/port_8172` and
`protocols/rauch-wlan/captures/port_58200`. This directory is ignored by Git.

## Receiving spreading settings from the app

The WLAN terminal acts as a TCP server and the Android application acts as a
client. To receive a fertilizer profile, the server must be reachable by the
phone at `152.21.0.31:8172`.

### Using the included receiver

1. Connect the phone and computer to the same test network used by the RAUCH
   application.
2. Assign the expected terminal address to the computer's network interface:

   ```bash
   sudo ip address add 152.21.0.31/24 dev <interface>
   ```

3. Start the receiver from the repository root:

   ```bash
   python3 protocols/rauch-wlan/rauch_wlan.py \
     --host 152.21.0.31 --port 8172
   ```

4. In the RAUCH app, select the AXIS machine, fertilizer, working width, and
   spreading configuration, then use the function that sends/transfers the
   settings to the WLAN terminal.
5. The console prints the raw hexadecimal frame followed by all decoded fields,
   including fertilizer name, manufacturer, width, feed points, RPM values,
   mounting height, disc, Flow Factor, and OptiPoint distance factor.

The raw connection is also saved as a `.bin` file and the timestamped hexdump
as a `.txt` file under:

```text
protocols/rauch-wlan/captures/port_8172/
```

To receive settings and emulate the weight input at the same time, omit the
single `--port` option and provide a weight:

```bash
python3 protocols/rauch-wlan/rauch_wlan.py \
  --host 152.21.0.31 --weight-kg 123.45
```

### Implementing another receiver

TCP does not preserve message boundaries. Do not assume that one `recv()` call
returns all 206 bytes. The first four bytes contain the complete frame length,
so a receiver should:

1. accept a TCP connection on port 8172;
2. collect at least four bytes;
3. decode `frame_length` as `uint32_le`;
4. continue reading until exactly `frame_length` bytes have been collected;
5. reject unreasonable lengths and frames shorter than the 16-byte header;
6. verify `sum(frame[16:frame_length]) & 0xffff` against the value at offset 4;
7. require XF1 version 3 and exactly one record for the known 206-byte format;
8. decode the record according to the field table below;
9. wait for EOF or close the connection after processing the frame.

Minimal Python example:

```python
import socket
import struct


def receive_exact(conn, size):
    data = bytearray()
    while len(data) < size:
        chunk = conn.recv(size - len(data))
        if not chunk:
            raise ConnectionError("connection closed before the complete XF1 frame")
        data.extend(chunk)
    return bytes(data)


with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("152.21.0.31", 8172))
    server.listen(5)

    while True:
        conn, peer = server.accept()
        with conn:
            header_prefix = receive_exact(conn, 4)
            frame_length = struct.unpack("<I", header_prefix)[0]
            if frame_length != 206:
                raise ValueError(f"invalid XF1 length: {frame_length}")

            frame = header_prefix + receive_exact(conn, frame_length - 4)
            version = struct.unpack_from("<I", frame, 8)[0]
            record_count = struct.unpack_from("<H", frame, 12)[0]
            if version != 3 or record_count != 1:
                raise ValueError("unsupported XF1 version or record count")

            stored_checksum = struct.unpack_from("<I", frame, 4)[0]
            calculated_checksum = sum(frame[16:]) & 0xffff
            if stored_checksum != calculated_checksum:
                raise ValueError("invalid XF1 checksum")

            print(f"XF1 from {peer}: {frame_length} bytes, checksum OK")
            # Decode offsets using the fertilizer-record table below.
```

No reply is required. The current Android app closes the output stream without
reading from the socket. Sending an ACK may be useful for experiments with
other terminal software, but it does not affect the result shown by this app.

The included decoder rejects frames with a mismatched length, unsupported
version, unsupported record count, or invalid checksum. The raw bytes and
hexdump remain available in the capture directory for diagnostics.

## TCP/58200: remaining weight

The app is the TCP client. After connecting, it sends no request and only
reads four-byte samples from the terminal. Its connect timeout is 2 seconds;
after disconnecting it retries after approximately 900 ms.

Each sample is a bare IEEE-754 `float32`, little-endian:

```c
wire_value = float32_le(kilograms * 1000.0f);
kilograms  = float32_le(frame) / 1000.0f;
```

There is no header, sequence number, checksum, or separator. Every four bytes
form one sample in the TCP stream.

| Weight | Wire float | Hexadecimal frame |
|---:|---:|---|
| 0 kg | `0.0f` | `00 00 00 00` |
| 1 kg | `1000.0f` | `00 00 7A 44` |
| 100 kg | `100000.0f` | `00 50 C3 47` |
| 123.45 kg | `123450.0f` | `00 1D F1 47` |
| -5 kg | `-5000.0f` | `00 40 9C C5` |

A live test confirmed that `00 1D F1 47` makes the app display `123 kg`;
the UI rounds or truncates the fractional part.

## TCP/8172: XF1 spreading settings

The app opens a connection with a 5-second timeout, writes exactly one
206-byte record, flushes, and closes the socket. It never reads an application
ACK. A success message in the UI therefore confirms only that the socket write
succeeded.

TCP segmentation is arbitrary. A receiver must first collect four bytes, read
the little-endian length, and then buffer until the complete frame is present.

### Header

The full XF1 object is a 16-byte header followed by a 190-byte record. All
integers are little-endian.

| Offset | Size | APK field | Meaning |
|---:|---:|---|---|
| 0 | 4 | `Dateilaenge` | Total length, always 206 in known samples |
| 4 | 4 | `Checksumme` | Sum of bytes 16..205 modulo 65536 |
| 8 | 4 | `Dateiversion` | Format version, observed value 3 |
| 12 | 2 | `AnzahlDatensaetze` | Record count, observed value 1 |
| 14 | 1 | `MaschinenTyp` | Spreader machine family (`Baureihe`) |
| 15 | 1 | `Ueberschreiben` | Overwrite flag; the app sends 0 |

Checksum calculation:

```python
checksum = sum(frame[16:206]) & 0xffff
```

The checksum occupies a `uint32` field, although the app only calculates the
low 16 bits.

### Fertilizer record

Fixed strings are ISO-8859-1, zero-terminated when shorter than their field,
and padded with zero bytes.

| Offset | Size | APK field | Encoding / meaning |
|---:|---:|---|---|
| 16 | 1 | `Nummer` | Record number; observed value 1 |
| 17 | 1 | `Gueltigkeit` | Validity flag; observed value 1 |
| 18 | 1 | `AGP` | Normal feed point x10 |
| 19 | 1 | `AGPRand` | Edge feed point x10 |
| 20 | 1 | `AGPGrenze` | Border feed point x10 |
| 21 | 1 | `Anbauhoehe` | Mounting-height code |
| 22 | 4 | `Arbeitsbreite` | Working width in metres x1000 |
| 26 | 21 | `ChemischeZusammensetzung` | Chemical composition string |
| 47 | 2 | `DrehzahlNormal` | Normal spreading RPM |
| 49 | 2 | `DrehzahlRand` | Edge spreading RPM |
| 51 | 2 | `DrehzahlGrenze` | Border spreading RPM |
| 53 | 61 | `DuengerHersteller` | Manufacturer string |
| 114 | 61 | `DuengerName` | Fertilizer/product name |
| 175 | 1 | `DuengungsArt` | Fertilization type |
| 176 | 4 | `Fliessfaktor` | Flow Factor x100 |
| 180 | 6 | `FluegelEinstellung` | Vane-setting string |
| 186 | 2 | `OptiWWK` | OptiPoint distance factor |
| 188 | 6 | `FlugeinstellungsRand` | Edge setting string |
| 194 | 6 | `FlugeinstellungsGrenze` | Border setting string |
| 200 | 1 | `TelimatMenge` | TELIMAT amount; app sends 0 |
| 201 | 1 | `Wurfscheibe` | Spreading-disc database key |
| 202 | 2 | `MinSplit` | Lower split/opening range |
| 204 | 2 | `MaxSplit` | Upper split/opening range |

Known mounting-height codes are `0=unknown`, `1=0/6`, `2=40/40`, `3=50/50`,
`4=60/60`, `5=70/70`, and `6=70/76`. This establishes that the observed
`50/50` value is the mounting height, not a normal-spreading distribution.

Database key `Wurfscheibe=47` maps to disc `S 1`. Machine type is the family,
not the exact model identifier; observed type 15 covers multiple AXIS models.

### Confirmed sample

The captured urea profile decoded as:

```text
machine type       15
feed points        normal 6.0, edge 5.0, border 4.0
mounting height    code 3 = 50/50
working width      15000 mm
RPM                normal 900, edge 450, border 350
Flow Factor        0.84
OptiWWK             64
disc key           47 = S 1
```

All five captured 206-byte records passed the APK checksum algorithm.

## Relationship to LHFS and the ECU

The Android app does not implement LHFS and sends no `0x13xx` filesystem
commands. It hands one complete XF1 record to the WLAN terminal. The remaining
conversion is not visible in the APK. The terminal may either save an XF1-like
record through the ECU's NV filesystem or parse it and apply individual ECU
parameters. Capturing the WLAN-terminal-to-ECU link or comparing LHFS snapshots
before and after an import is required to distinguish these possibilities.

## Reverse-engineering sources

The decisive APK classes were:

- `data/export/ExportData.java`: XF1 serialization and field layout;
- `network/ExportTcpClient.java`: TCP/8172 connection and send behavior;
- `network/LibreTcpClient.java`: TCP/58200 weight decoding;
- `BuildConfig.java`: fixed address and port constants.

Local APK and JADX output live under `../../research/apk/` and are excluded
from version control.
