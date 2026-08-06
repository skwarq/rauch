#!/usr/bin/env python3
"""Extract bootloaders and the Intel HEX memory map from a TeeJet PEX file."""

import argparse
import json
from pathlib import Path


def contents_between(data: bytes, opening: bytes, closing: bytes, start=0) -> bytes:
    begin = data.index(opening, start)
    begin = data.index(b">", begin) + 1
    end = data.index(closing, begin)
    return data[begin:end]


def parse_intel_hex(text: bytes):
    memory = {}
    upper = 0
    records = []
    saw_eof = False
    for line_number, line in enumerate(text.splitlines(), 1):
        if not line:
            continue
        if saw_eof:
            raise ValueError(f"Line {line_number}: data found after EOF record")
        if not line.startswith(b":"):
            raise ValueError(f"Line {line_number}: missing ':'")
        record = bytes.fromhex(line[1:].decode("ascii"))
        if len(record) != record[0] + 5 or sum(record) & 0xFF:
            raise ValueError(f"Line {line_number}: invalid length or checksum")
        length = record[0]
        address = int.from_bytes(record[1:3], "big")
        record_type = record[3]
        payload = record[4:4 + length]
        if record_type not in range(0x00, 0x06):
            raise ValueError(f"Line {line_number}: unsupported record type {record_type:02X}")
        expected_lengths = {0x01: 0, 0x02: 2, 0x03: 4, 0x04: 2, 0x05: 4}
        if record_type in expected_lengths and length != expected_lengths[record_type]:
            raise ValueError(f"Line {line_number}: invalid length for record type {record_type:02X}")
        if record_type in (0x02, 0x04) and address != 0:
            raise ValueError(f"Line {line_number}: extended-address record has a nonzero address")
        records.append(record)
        if record_type == 0x00:
            absolute = upper + address
            for offset, value in enumerate(payload):
                memory[absolute + offset] = value
        elif record_type == 0x02:
            upper = int.from_bytes(payload, "big") << 4
        elif record_type == 0x04:
            upper = int.from_bytes(payload, "big") << 16
        elif record_type == 0x01:
            saw_eof = True
    if not saw_eof:
        raise ValueError("Intel HEX input has no EOF record")
    return memory, records


def contiguous_segments(memory):
    if not memory:
        return []
    addresses = sorted(memory)
    segments = []
    start = previous = addresses[0]
    chunk = bytearray([memory[start]])
    for address in addresses[1:]:
        if address != previous + 1:
            segments.append((start, bytes(chunk)))
            start = address
            chunk = bytearray()
        chunk.append(memory[address])
        previous = address
    segments.append((start, bytes(chunk)))
    return segments


def main():
    parser = argparse.ArgumentParser(description="Extract firmware from a PEX container")
    parser.add_argument("pex", type=Path)
    default_output = Path(__file__).resolve().parent / "extracted_firmware"
    parser.add_argument("--output", type=Path, default=default_output)
    args = parser.parse_args()

    source = args.pex.read_bytes()
    args.output.mkdir(parents=True, exist_ok=True)
    level1 = contents_between(source, b"<BIN_FILE", b"</BIN_FILE>", source.index(b"<LEVEL1>"))
    level2 = contents_between(source, b"<BIN_FILE", b"</BIN_FILE>", source.index(b"<LEVEL2>"))
    hex_text = contents_between(source, b"<HEX_DATA", b"</HEX_DATA>")
    memory, records = parse_intel_hex(hex_text)
    segments = contiguous_segments(memory)

    for stale_segment in args.output.glob("segment_*.bin"):
        stale_segment.unlink()

    (args.output / "level1.bin").write_bytes(level1)
    (args.output / "level2.bin").write_bytes(level2)
    (args.output / "application.hex").write_bytes(hex_text)
    (args.output / "application_records.bin").write_bytes(b"".join(records))

    manifest = {
        "source": str(args.pex.resolve()),
        "level1_size": len(level1),
        "level2_size": len(level2),
        "record_count": len(records),
        "programmed_bytes": len(memory),
        "segments": [],
    }
    for index, (address, data) in enumerate(segments):
        name = f"segment_{index:03d}_0x{address:08X}_0x{address + len(data) - 1:08X}.bin"
        (args.output / name).write_bytes(data)
        manifest["segments"].append({
            "file": name,
            "start": address,
            "end": address + len(data) - 1,
            "size": len(data),
        })
    (args.output / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
