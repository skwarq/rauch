#!/usr/bin/env python3

import argparse
import sys
import socket
import struct
import threading
import datetime as dt
from pathlib import Path

HOST = "127.0.0.1"
PORT = 11520
BLOCK_SIZE = 32
FILE_LOADER_POLL = bytes.fromhex("00 00 01 01 02 00")
LHFS_ACK = bytes.fromhex("00 0F")
VIRTUAL_FILE_NAME = "TEST.TXT"
VIRTUAL_FILE_DATA = b"RAUCH LHFS test file\r\n"
VIRTUAL_FILE_HANDLE = 1
LHFS_READ_CHUNK_SIZE = 100
LHFS_RECORDS_PER_PACKET = 5
LHFS_MAX_FILE_SIZE = 0xFFFF
RECEIVED_FILES_DIR = Path(__file__).resolve().parent / "received_files"
VIRTUAL_FILES = {
    VIRTUAL_FILE_NAME: {
        "name": VIRTUAL_FILE_NAME,
        "data": VIRTUAL_FILE_DATA,
        "handle": VIRTUAL_FILE_HANDLE,
    }
}
UPLOAD_STATE = None


def timestamp() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="milliseconds")


def hexdump(data: bytes):
    for offset in range(0, len(data), 16):
        chunk = data[offset:offset+16]

        hex_part = " ".join(f"{b:02X}" for b in chunk)
        ascii_part = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)

        print(f"{offset:04X}  {hex_part:<48} |{ascii_part}|")
    print()


def send_auto(sock, reply: bytes, reason: str) -> bool:
    try:
        sock.sendall(reply)
    except OSError as exc:
        print(f"[{timestamp()}] AUTO TX error: {exc}")
        return False

    print(f"[{timestamp()}] AUTO TX ({reason}) {len(reply)} B")
    hexdump(reply)
    return True


def rx_pex(sock, block_reply: str):
    sync_done = False
    buffer = bytearray()

    while True:
        try:
            data = sock.recv(4096)
        except Exception as e:
            print(f"[{timestamp()}] RX error: {e}")
            break

        if not data:
            print(f"[{timestamp()}] Connection closed")
            break

        print(f"\n[{timestamp()}] RX {len(data)} bytes")
        hexdump(data)

        if not sync_done and data.startswith(b"\x00"):
            if not send_auto(sock, b"\xA5", "synchronization"):
                break
            sync_done = True
            data = data[1:]
            if not data:
                continue

        buffer.extend(data)
        print(f"[{timestamp()}] FULL STREAM ({len(buffer)} B): {buffer.hex(' ')}")

        while len(buffer) >= BLOCK_SIZE:
            block = bytes(buffer[:BLOCK_SIZE])
            del buffer[:BLOCK_SIZE]
            print(f"[{timestamp()}] COMPLETE BLOCK {BLOCK_SIZE} B")
            hexdump(block)

            if block_reply == "a5":
                reply = b"\xA5"
            elif block_reply == "00":
                reply = b"\x00"
            else:
                reply = block

            if not send_auto(sock, reply, f"block reply: {block_reply}"):
                return


def build_lhfs_frame(command: int, payload: bytes = b"") -> bytes:
    if len(payload) > 255:
        raise ValueError("An LHFS payload may contain at most 255 bytes")
    body = struct.pack("<HB", command, len(payload)) + payload
    checksum = sum(body) & 0xFFFF
    return body + struct.pack("<H", checksum)


def find_virtual_file(name: str | None = None, handle: int | None = None):
    if name is not None:
        return VIRTUAL_FILES.get(name.upper())
    return next(
        (item for item in VIRTUAL_FILES.values() if item["handle"] == handle),
        None,
    )


def build_file_record(file_info=None) -> bytes:
    """Build the 48-byte file record used by GET_NV_FILE_INFO_LIST."""
    if file_info is None:
        file_info = VIRTUAL_FILES[VIRTUAL_FILE_NAME]
    record = bytearray(48)
    # Layout reconstructed from the LHFS.dll parser. The first DWORD stores
    # attributes rather than size.
    struct.pack_into("<I", record, 0x00, 0)
    name = file_info["name"].encode("ascii", errors="replace")[:12]
    record[4:4 + len(name)] = name

    # Creation time: day, month, year, weekday, hour, minute, second.
    record[0x12] = 6
    record[0x13] = 8
    struct.pack_into("<H", record, 0x14, 2026)
    record[0x16] = 4
    record[0x18] = 18
    record[0x19] = 48
    record[0x1A] = 0

    # Last-modified time uses the same format.
    record[0x1C] = 6
    record[0x1D] = 8
    struct.pack_into("<H", record, 0x1E, 2026)
    record[0x20] = 4
    record[0x22] = 18
    record[0x23] = 48
    record[0x24] = 0

    struct.pack_into("<H", record, 0x26, file_info["handle"])
    # The DLL copies both trailing DWORDs into its file-information structure.
    # Set both to the length to cover logical and allocated-size variants.
    struct.pack_into("<I", record, 0x28, len(file_info["data"]))
    struct.pack_into("<I", record, 0x2C, len(file_info["data"]))
    return bytes(record)


def build_file_timestamps() -> bytes:
    """Return two 0x1342 timestamps in the order expected by LHFS.dll."""
    # hour, minute, second, day, month, year, weekday
    created = struct.pack("<BBBBBHB", 18, 48, 0, 6, 8, 2026, 4)
    modified = struct.pack("<BBBBBHB", 18, 48, 0, 6, 8, 2026, 4)
    return created + modified


def decode_lhfs_filename(payload: bytes) -> str:
    """Decode an LHFS name stored as length:u8 followed by ASCII."""
    if not payload:
        return ""
    name_length = min(payload[0], len(payload) - 1)
    return payload[1:1 + name_length].decode("ascii", errors="replace")


def store_uploaded_file() -> tuple[bool, str]:
    """Finish the active upload, persist it, and add it to the LHFS directory."""
    global UPLOAD_STATE
    if not UPLOAD_STATE:
        return False, "no upload is active"
    state = UPLOAD_STATE
    data = bytes(state["data"][:state["size"]])
    if len(data) != state["size"]:
        return False, f"incomplete file: {len(data)}/{state['size']} B"

    safe_name = Path(state["name"]).name.upper()[:12]
    if not safe_name:
        safe_name = "UPLOAD.BIN"
    existing = find_virtual_file(name=safe_name)
    handle = existing["handle"] if existing else max(
        (item["handle"] for item in VIRTUAL_FILES.values()), default=0
    ) + 1
    file_info = {"name": safe_name, "data": data, "handle": handle}
    VIRTUAL_FILES[safe_name] = file_info
    RECEIVED_FILES_DIR.mkdir(parents=True, exist_ok=True)
    output_path = RECEIVED_FILES_DIR / safe_name
    output_path.write_bytes(data)
    UPLOAD_STATE = None
    return True, f"saved {safe_name}: {len(data)} B -> {output_path}"


def load_received_files():
    """Load files persisted by previous emulator runs."""
    if not RECEIVED_FILES_DIR.exists():
        return
    next_handle = max(item["handle"] for item in VIRTUAL_FILES.values()) + 1
    for path in sorted(RECEIVED_FILES_DIR.iterdir()):
        if not path.is_file():
            continue
        size = path.stat().st_size
        if size > LHFS_MAX_FILE_SIZE:
            print(
                f"[{timestamp()}] Ignoring {path.name}: {size} B exceeds the "
                f"LHFS {LHFS_MAX_FILE_SIZE} B transfer limit"
            )
            continue
        name = path.name.upper()[:12]
        VIRTUAL_FILES[name] = {
            "name": name,
            "data": path.read_bytes(),
            "handle": next_handle,
        }
        next_handle += 1


def lhfs_response(command: int, payload: bytes) -> tuple[bytes, bytes | None, str]:
    """Return an ACK, optional response, and a command description."""
    global UPLOAD_STATE
    status_ok = b"\x00"
    scalar16 = {
        0x131D: (0x131E, 0),  # number of open files
        0x131F: (0x1320, 4),  # maximum number of open files
    }
    scalar32 = {
        0x1321: (0x1322, 0),
        0x1331: (0x1332, sum(len(item["data"]) for item in VIRTUAL_FILES.values())),
    }

    if command == 0x0000:  # WAKE_UP_DUT
        return LHFS_ACK, build_lhfs_frame(0x0001, b"\x01\x01"), "WAKE_UP_DUT"
    if command == 0x1300:  # GET_NV_FILE_HANDLE
        requested_name = decode_lhfs_filename(payload)
        file_info = find_virtual_file(name=requested_name)
        if file_info:
            response_payload = status_ok + struct.pack("<H", file_info["handle"])
            description = f"handle {file_info['handle']} for {requested_name}"
        else:
            response_payload = b"\x01\xFF\xFF"
            description = f"file {requested_name!r} not found"
        return LHFS_ACK, build_lhfs_frame(0x1301, response_payload), description
    if command in (0x132D, 0x132F):  # GET_NV_FILE_SIZE / GET_NV_FILE_SIZE_EX
        handle = struct.unpack_from("<H", payload + b"\x00\x00", 0)[0]
        file_info = find_virtual_file(handle=handle)
        status = 0 if file_info else 1
        size = len(file_info["data"]) if file_info else 0
        response_payload = bytes([status]) + struct.pack("<HI", handle, size)
        return LHFS_ACK, build_lhfs_frame(command + 1, response_payload), f"file size={size} B"
    if command == 0x1302:  # READ_NV_FILE
        handle, requested_size = struct.unpack_from("<HH", payload + b"\x00" * 4, 0)
        file_info = find_virtual_file(handle=handle)
        status = 0 if file_info else 1
        file_size = len(file_info["data"]) if file_info else 0
        if file_size > LHFS_MAX_FILE_SIZE:
            status = 1
            file_size = 0
        packet_count = (file_size + LHFS_READ_CHUNK_SIZE - 1) // LHFS_READ_CHUNK_SIZE
        response_payload = bytes([status]) + struct.pack(
            "<HHH", handle, file_size, packet_count
        )
        description = (
            f"start read: handle={handle}, requested size={requested_size}, "
            f"file={file_size} B, packets={packet_count}"
        )
        return LHFS_ACK, build_lhfs_frame(0x1303, response_payload), description
    if command == 0x1304:  # READ_NV_FILE_DATA_PACKET
        handle, packet_no = struct.unpack_from("<HH", payload + b"\x00" * 4, 0)
        file_info = find_virtual_file(handle=handle)
        start = packet_no * LHFS_READ_CHUNK_SIZE
        data = (
            file_info["data"][start:start + LHFS_READ_CHUNK_SIZE]
            if file_info
            else b""
        )
        status = 0 if file_info and (data or len(file_info["data"]) == 0) else 1
        # The DLL expects status, an 8-bit handle, packet number, data length,
        # and packet data.
        response_payload = (
            bytes([status, handle & 0xFF])
            + struct.pack("<H", packet_no)
            + bytes([len(data)])
            + data
        )
        return LHFS_ACK, build_lhfs_frame(0x1305, response_payload), (
            f"file data: packet={packet_no}, {len(data)} B"
        )
    if command == 0x1306:  # READ_NV_FILE_TERMINATE_READ
        return LHFS_ACK, None, "end file read"
    if command in (0x1327, 0x1329):  # NV_FILE_EXIST / NV_FILE_EXIST_EX
        requested_name = decode_lhfs_filename(payload)
        exists = 1 if find_virtual_file(name=requested_name) else 0
        # The DLL reads status from payload[0] and the result from payload[2].
        response_payload = bytes([0, 0, exists])
        return LHFS_ACK, build_lhfs_frame(command + 1, response_payload), (
            f"file {requested_name!r} {'exists' if exists else 'does not exist'}"
        )
    if command == 0x1323:  # DELETE_NV_FILE
        requested_name = decode_lhfs_filename(payload)
        key = requested_name.upper()
        file_info = VIRTUAL_FILES.pop(key, None)
        if file_info:
            stored_path = RECEIVED_FILES_DIR / file_info["name"]
            if stored_path.is_file():
                stored_path.unlink()
            status = 0
            description = f"deleted {file_info['name']}"
        else:
            status = 1
            description = f"file {requested_name!r} not found for deletion"
        return LHFS_ACK, build_lhfs_frame(0x1324, bytes([status])), description
    if command == 0x1307:  # CREATE_NV_FILE
        if len(payload) < 14:
            return LHFS_ACK, build_lhfs_frame(0x1308, b"\x01"), "invalid CREATE_NV_FILE"
        name_length = min(payload[0], len(payload) - 14)
        name = payload[14:14 + name_length].decode("ascii", errors="replace")
        file_size, packet_count = struct.unpack_from("<HH", payload, 1)
        UPLOAD_STATE = {
            "name": name,
            "size": file_size,
            "packet_count": packet_count,
            "next_packet": 0,
            "data": bytearray(),
        }
        description = f"creating {name!r}: {file_size} B, packets={packet_count}"
        return LHFS_ACK, build_lhfs_frame(0x1308, status_ok), description
    if command == 0x1309:  # CREATE_NV_FILE_DATA_PACKET
        if len(payload) < 3 or UPLOAD_STATE is None:
            response_payload = struct.pack("<HB", 0, 1)
            return LHFS_ACK, build_lhfs_frame(0x130A, response_payload), "packet without CREATE"
        packet_no = struct.unpack_from("<H", payload, 0)[0]
        data_length = min(payload[2], len(payload) - 3)
        data = payload[3:3 + data_length]
        expected_packet = UPLOAD_STATE["next_packet"]
        if packet_no == expected_packet:
            UPLOAD_STATE["data"].extend(data)
            UPLOAD_STATE["next_packet"] += 1
        done = len(UPLOAD_STATE["data"]) >= UPLOAD_STATE["size"]
        next_packet = UPLOAD_STATE["next_packet"]
        response_payload = struct.pack("<HB", next_packet, int(done))
        description = (
            f"upload packet={packet_no}, {len(data)} B, "
            f"total={len(UPLOAD_STATE['data'])}/{UPLOAD_STATE['size']} B"
        )
        return LHFS_ACK, build_lhfs_frame(0x130A, response_payload), description
    if command == 0x130B:  # CREATE_NV_FILE_STORE_FILE
        saved, description = store_uploaded_file()
        status = 0 if saved else 1
        return LHFS_ACK, build_lhfs_frame(0x130C, bytes([status])), description
    if command in scalar16:
        response_command, value = scalar16[command]
        response = build_lhfs_frame(response_command, status_ok + struct.pack("<H", value))
        return LHFS_ACK, response, f"NV value={value}"
    if command in scalar32:
        response_command, value = scalar32[command]
        response = build_lhfs_frame(response_command, status_ok + struct.pack("<I", value))
        return LHFS_ACK, response, f"NV value={value}"
    if command in (0x1333, 0x1338):
        # status, file count, directory packet count
        response_command = command + 1
        file_count = len(VIRTUAL_FILES)
        packet_count = (file_count + LHFS_RECORDS_PER_PACKET - 1) // LHFS_RECORDS_PER_PACKET
        response = build_lhfs_frame(
            response_command, status_ok + struct.pack("<HH", file_count, packet_count)
        )
        return LHFS_ACK, response, f"directory has {file_count} files in {packet_count} packets"
    if command in (0x1335, 0x133A):
        packet_no = struct.unpack_from("<H", payload + b"\x00\x00", 0)[0]
        files = list(VIRTUAL_FILES.values())
        start = packet_no * LHFS_RECORDS_PER_PACKET
        selected = files[start:start + LHFS_RECORDS_PER_PACKET]
        records = b"".join(build_file_record(item) for item in selected)
        response_command = command + 1
        response_payload = struct.pack("<HB", packet_no, len(records)) + records
        response = build_lhfs_frame(response_command, response_payload)
        names = ", ".join(item["name"] for item in selected)
        return LHFS_ACK, response, f"records: {names}"
    if command in (0x1337, 0x133C):
        return LHFS_ACK, None, "end directory listing"
    if command == 0x1341:  # NV_FILE_GET_FILE_INFO_ALL_FILES_EX
        requested_name = decode_lhfs_filename(payload)
        file_info = find_virtual_file(name=requested_name)
        status = 0 if file_info else 1
        handle = file_info["handle"] if file_info else 0xFFFF
        response_payload = (
            bytes([status])
            + struct.pack("<HI", handle, 0)
            + build_file_timestamps()
        )
        return LHFS_ACK, build_lhfs_frame(0x1342, response_payload), (
            f"metadata for {requested_name!r}"
        )

    return LHFS_ACK, None, "unsupported command"


def rx_file_loader(sock):
    buffer = bytearray()

    while True:
        try:
            data = sock.recv(4096)
        except Exception as exc:
            print(f"[{timestamp()}] RX error: {exc}")
            return

        if not data:
            print(f"[{timestamp()}] Connection closed")
            return

        print(f"\n[{timestamp()}] RX {len(data)} bytes")
        hexdump(data)
        buffer.extend(data)

        while len(buffer) >= 3:
            frame_size = buffer[2] + 5
            if len(buffer) < frame_size:
                break
            frame = bytes(buffer[:frame_size])
            del buffer[:frame_size]

            command = struct.unpack_from("<H", frame, 0)[0]
            payload = frame[3:-2]
            received_checksum = struct.unpack_from("<H", frame, len(frame) - 2)[0]
            calculated_checksum = sum(frame[:-2]) & 0xFFFF
            print(
                f"[{timestamp()}] LHFS CMD=0x{command:04X}, "
                f"payload={len(payload)} B, checksum="
                f"{'OK' if received_checksum == calculated_checksum else 'INVALID'}"
            )
            if received_checksum != calculated_checksum:
                continue

            ack, response, description = lhfs_response(command, payload)
            print(f"[{timestamp()}] Emulator: {description}")
            if not send_auto(sock, ack, "LHFS ACK"):
                return
            if response is not None and not send_auto(sock, response, "LHFS response"):
                return


def parse_args():
    parser = argparse.ArgumentParser(description="TeeJet LHFS filesystem emulator")
    parser.add_argument("--host", default=HOST, help=f"Bridge host (default: {HOST})")
    parser.add_argument("--port", type=int, default=PORT,
                        help=f"Bridge TCP port (default: {PORT})")
    parser.add_argument(
        "--protocol",
        choices=("file-loader", "pex"),
        default="file-loader",
        help="Protocol to emulate (default: file-loader)",
    )
    parser.add_argument(
        "--block-reply",
        choices=("a5", "00", "echo"),
        default="a5",
        help="Reply to each complete 32-byte block (default: a5)",
    )
    parser.add_argument(
        "--interactive", action="store_true",
        help="Enable manual hexadecimal TX input while the receiver is running",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    load_received_files()
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.connect((args.host, args.port))

    print(f"[{timestamp()}] Connected to {args.host}:{args.port}")

    print(f"[{timestamp()}] Protocol: {args.protocol}")
    if args.protocol == "file-loader":
        print(f"[{timestamp()}] Virtual file: {VIRTUAL_FILE_NAME} ({len(VIRTUAL_FILE_DATA)} B)")
        target = rx_file_loader
        rx_args = (s,)
    else:
        print(f"[{timestamp()}] PEX reply mode: {args.block_reply}")
        target = rx_pex
        rx_args = (s, args.block_reply)
    receiver = threading.Thread(target=target, args=rx_args, daemon=True)
    receiver.start()

    if not args.interactive:
        receiver.join()
        s.close()
        return

    while receiver.is_alive():
        try:
            line = input("HEX> ").strip()
        except EOFError:
            break

        if line == "":
            continue

        if line.lower() in ("quit", "exit"):
            break

        try:
            data = bytes.fromhex(line)
        except ValueError:
            print(f"[{timestamp()}] Invalid HEX")
            continue

        try:
            s.sendall(data)
        except OSError as exc:
            print(f"[{timestamp()}] TX error: {exc}", file=sys.stderr)
            break

        print(f"[{timestamp()}] TX {len(data)} bytes")
        hexdump(data)

    s.close()


if __name__ == "__main__":
    main()
