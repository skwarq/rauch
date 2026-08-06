# TeeJet LHFS filesystem protocol

This directory contains an experimental device emulator for TeeJet File
Loader and `LHFS.dll`. The implementation was reconstructed from the DLL and
confirmed with the real desktop application.

Confirmed operations include initialization, NV statistics, directory
listing, downloading a file, uploading/replacing a file, and deletion.

> LHFS and PEX Loader are different protocols. The `A5` bootstrap byte used by
> PEX Loader is not an LHFS response.

## Running the emulator

The Windows serial link was bridged to TCP during testing. The emulator connects
to `127.0.0.1:11520` by default:

```bash
python3 protocols/lhfs/lhfs_emulator.py
python3 protocols/lhfs/lhfs_emulator.py --host 127.0.0.1 --port 11520
```

Uploaded files are stored under `protocols/lhfs/received_files/` and loaded
again at startup. The directory is ignored by Git.

The emulator exits automatically when the bridge closes the connection. Add
`--interactive` only when manual hexadecimal transmission through the `HEX>`
prompt is needed.

## Transport and framing

TCP is only a test transport for a serial byte stream. A frame may be split
across reads, and multiple frames may arrive in one read.

```text
uint16_le command
uint8     payload_length
uint8     payload[payload_length]
uint16_le checksum
```

The total frame length is `payload_length + 5`. The checksum is the 16-bit sum
of every command, length, and payload byte; checksum bytes are excluded:

```python
body = command.to_bytes(2, "little") + bytes([len(payload)]) + payload
frame = body + (sum(body) & 0xffff).to_bytes(2, "little")
```

Example wake-up request:

```text
00 00 01 01 02 00
^^^^^ ^^ ^^ ^^^^^
 cmd   N data checksum
```

## ACK

A valid request is acknowledged with a special two-byte token:

```text
00 0F
```

It is not a regular framed message. Requests returning data normally produce:

```text
File Loader -> device: request frame
device -> File Loader: 00 0F
device -> File Loader: response frame
```

Termination commands may receive only the ACK.

## Known commands

| Request | Response | Operation | Emulator |
|---:|---:|---|---|
| `0x0000` | `0x0001` | Wake up / identify device | yes |
| `0x1300` | `0x1301` | Get NV file handle | yes |
| `0x1302` | `0x1303` | Start reading an NV file | yes |
| `0x1304` | `0x1305` | Read an NV data packet | yes |
| `0x1306` | ACK | Terminate read | yes |
| `0x1307` | `0x1308` | Create NV file | yes |
| `0x1309` | `0x130A` | Send file data packet | yes |
| `0x130B` | `0x130C` | Store/finalize file | yes |
| `0x131D` | `0x131E` | Number of open NV files | yes |
| `0x131F` | `0x1320` | Maximum open NV files | yes |
| `0x1321` | `0x1322` | Allocated NV file buffer | yes |
| `0x1323` | `0x1324` | Delete NV file | yes |
| `0x1327` | `0x1328` | File exists | yes |
| `0x1329` | `0x132A` | Extended file-exists query | yes |
| `0x132D` | `0x132E` | Get file size | yes |
| `0x132F` | `0x1330` | Extended size query | yes |
| `0x1331` | `0x1332` | Used NV data size | provisional |
| `0x1333` | `0x1334` | Begin basic directory list | yes |
| `0x1335` | `0x1336` | Basic directory record | yes |
| `0x1337` | ACK | Close basic list | yes |
| `0x1338` | `0x1339` | Begin extended list | yes |
| `0x133A` | `0x133B` | Extended directory record | yes |
| `0x133C` | ACK | Close extended list | yes |
| `0x1341` | `0x1342` | File information by name | yes |

Command names are based on symbols and diagnostic strings in `LHFS.dll`.

## Wake-up sequence

```text
RX  00 00 01 01 02 00
TX  00 0F
TX  01 00 02 01 01 05 00
```

The response payload `01 01` appears to identify a device class and software
version, but its exact semantics remain provisional.

## Directory listing

The application begins with command `0x1333`. For a directory containing one
file in one packet, the emulator returns:

```text
RX  33 13 00 46 00
TX  00 0F
TX  34 13 05 00 01 00 01 00 4E 00
```

The `0x1334` payload is:

```text
status:u8 | file_count:u16_le | packet_count:u16_le
```

The loader requests records with `0x1335`; `0x1336` starts with a three-byte
packet header followed by zero or more 48-byte file records:

| Payload offset | Size | Meaning |
|---:|---:|---|
| 0 | 2 | Directory packet number, little-endian |
| 2 | 1 | Total byte length of the following records |
| 3 | N x 48 | File records |

Each 48-byte file record is:

| Record offset | Size | Meaning |
|---:|---:|---|
| 0 | 4 | Attributes, little-endian |
| 4 | 12 | Zero-padded ASCII file name |
| 16 | 2 | Reserved |
| 18 | 10 | Creation date/time structure with padding |
| 28 | 10 | Modification date/time structure with padding |
| 38 | 2 | File handle, little-endian |
| 40 | 4 | Logical file size, little-endian |
| 44 | 4 | Allocated/file size, little-endian |

For one record, the response payload is therefore 51 bytes. The emulator sets
both trailing size fields to the current data length because the DLL copies
both values into its internal file-information structure.

The confirmed test response contained `TEST.TXT`. The list is closed with
`0x1337`, which receives only `00 0F`.

## Downloading a file

The confirmed sequence is:

```text
0x1329 -> 0x132A   check existence by name
0x1300 -> 0x1301   resolve file handle
0x1302 -> 0x1303   begin read; obtain size and packet count
0x1304 -> 0x1305   fetch packets, starting at packet 0
0x1306 -> ACK      terminate read
0x1341 -> 0x1342   fetch final metadata
```

Names are encoded as `name_length:u8 | name_bytes`. The emulator uses chunks
of at most 100 bytes. TeeJet File Loader successfully downloaded the test
contents `RAUCH LHFS test file\r\n`.

## Uploading and replacing a file

A full upload is transactional:

```text
existence check
optional DELETE_NV_FILE
CREATE_NV_FILE
one or more numbered data packets
STORE_FILE
```

The `0x1309` packet number begins at zero. Packets do not contain an arbitrary
file offset or a handle to an existing file. The implementation therefore has
no append primitive. To append, a client must download the old file, modify it
locally, delete/replace it, and upload the complete result.

The confirmed read/create transaction represents file size as `uint16`, so one
LHFS transfer is limited to 65,535 bytes. Files exceeding this limit are
ignored when the emulator loads `received_files/`, and an oversized in-memory
entry returns an error instead of terminating the receiver.

An upload of `HELLO.TXT` containing `hello world!` and subsequent directory
refresh were confirmed with the real loader.

## Deletion

`0x1323/0x1324` deletes a file identified by its length-prefixed name. Deletion
was confirmed by refreshing the remote directory and observing the file
disappear.

## Public `LHFS.dll` API

The DLL exports initialization and port discovery calls, device information
and statistics calls, directory iteration, file existence/metadata queries,
copy-to-device, copy-from-device, delete, and extended error reporting. It does
not export `AppendFile`, `Seek`, or a random-access write API. The imported
Windows `WriteFile` symbol is used for local/serial I/O and does not establish
an LHFS append operation.

## Confidence

- Confirmed live: framing, byte order, checksum, ACK, directory listing,
  download, upload/replace, delete, name encoding, and 100-byte chunks.
- Confirmed from DLL parsing: field offsets used by the file and timestamp
  records.
- Provisional: exact identity payload semantics, several NV statistics, some
  directory metadata fields, and differences between basic/extended lists.
