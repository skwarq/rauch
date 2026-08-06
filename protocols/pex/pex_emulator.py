#!/usr/bin/env python3
"""Experimental device emulator for TeeJet PEX Loader.

It emulates bootstrap mode, acknowledges update stages, and saves all received
parts under the local ``captures`` directory.
"""

import argparse
import datetime as dt
import socket
import time
from pathlib import Path


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 11520
TRIGGER = 0x00
DEVICE_IDS = (0xA5, 0xC5, 0xD5)
LEVEL1_SIZE = 32
LEVEL2_SIZE = 1024
SUPPORTED_RECORD_TYPES = frozenset(range(0x00, 0x06))
OUTPUT_DIR = Path(__file__).resolve().parent / "captures"


def timestamp() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="milliseconds")


def hexdump(data: bytes) -> None:
    for offset in range(0, len(data), 16):
        chunk = data[offset:offset + 16]
        hex_part = " ".join(f"{byte:02X}" for byte in chunk)
        ascii_part = "".join(chr(byte) if 32 <= byte < 127 else "." for byte in chunk)
        print(f"{offset:04X}  {hex_part:<48} |{ascii_part}|")
    print()


def send(sock: socket.socket, data: bytes, description: str, log: bool = True) -> None:
    sock.sendall(data)
    if log:
        print(f"[{timestamp()}] TX {len(data)} B ({description}): {data.hex(' ').upper()}")


class PexEmulator:
    """Minimal state machine reconstructed from TeeJetPexLoader.exe."""

    def __init__(self, sock: socket.socket, device_id: int, idle_time: float):
        self.sock = sock
        self.device_id = device_id
        self.idle_time = idle_time
        self.phase = "discovery"
        self.phase_data = bytearray()
        self.all_data = bytearray()
        self.application_buffer = bytearray()
        self.application_data = bytearray()
        self.application_records = 0
        self.last_rx = 0.0
        self.session = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    def save_part(self, name: str, data: bytes) -> Path:
        path = OUTPUT_DIR / f"{self.session}_{name}.bin"
        path.write_bytes(data)
        print(f"[{timestamp()}] Saved {name}: {len(data)} B -> {path}")
        return path

    def receive(self, data: bytes) -> None:
        self.last_rx = time.monotonic()

        if self.phase == "error":
            print(f"[{timestamp()}] Ignoring {len(data)} B after a protocol error")
            return
        if self.phase == "application":
            self.receive_application(data)
            return
        if self.phase == "done":
            self.save_part("after_done", data)
            send(self.sock, b"A", "final acknowledgement")
            return

        print(f"\n[{timestamp()}] RX {len(data)} B, phase={self.phase}")
        hexdump(data)

        if self.phase == "discovery":
            trigger_at = data.find(bytes([TRIGGER]))
            if trigger_at < 0:
                print(f"[{timestamp()}] Waiting for 00 polling byte")
                return
            send(self.sock, bytes([self.device_id]), "device identifier")
            self.phase = "level1"
            remainder = data[trigger_at + 1:]
            if remainder:
                self.phase_data.extend(remainder)
                self.all_data.extend(remainder)
            print(f"[{timestamp()}] Device detected; waiting for Level 1")
            self.consume_bootstrap_data()
            return

        self.phase_data.extend(data)
        self.all_data.extend(data)
        self.consume_bootstrap_data()

    def consume_bootstrap_data(self) -> None:
        """Advance bootstrap phases only after their confirmed byte counts."""
        while True:
            expected = LEVEL1_SIZE if self.phase == "level1" else LEVEL2_SIZE
            if self.phase not in ("level1", "level2") or len(self.phase_data) < expected:
                return

            data = bytes(self.phase_data[:expected])
            del self.phase_data[:expected]
            if self.phase == "level1":
                self.save_part("level1", data)
                send(self.sock, b"1", "ACK Level1")
                self.phase = "level2"
                continue

            self.save_part("level2", data)
            send(self.sock, b"2", "ACK Level2")
            time.sleep(0.05)
            send(self.sock, b"F", "Flash erased")
            time.sleep(0.20)
            send(self.sock, b"A", "Flash erase result OK")
            self.phase = "application"
            if self.phase_data:
                remainder = bytes(self.phase_data)
                self.phase_data.clear()
                self.receive_application(remainder, record_in_stream=False)
            return

    def receive_application(self, data: bytes, *, record_in_stream: bool = True) -> None:
        """Receive binary Intel HEX records and acknowledge each immediately."""
        self.application_buffer.extend(data)
        if record_in_stream:
            self.all_data.extend(data)

        while self.application_buffer:
            record_size = self.application_buffer[0] + 5
            if len(self.application_buffer) < record_size:
                return
            record = bytes(self.application_buffer[:record_size])
            del self.application_buffer[:record_size]
            self.application_data.extend(record)
            self.application_records += 1
            record_type = record[3]

            if sum(record) & 0xFF:
                print(
                    f"[{timestamp()}] Record #{self.application_records}: "
                    f"invalid checksum, {record.hex(' ').upper()}"
                )
                send(self.sock, b"C", "invalid Intel HEX checksum")
                self.phase = "error"
                return

            if record_type not in SUPPORTED_RECORD_TYPES:
                send(self.sock, b"E", "invalid Intel HEX record type")
                self.phase = "error"
                return
            if (record_type == 0x01 and record[0] != 0) or (
                record_type in (0x02, 0x04) and record[0] != 2
            ) or (record_type in (0x03, 0x05) and record[0] != 4):
                send(self.sock, b"E", "invalid Intel HEX record length")
                self.phase = "error"
                return

            send(self.sock, b"A", f"ACK record #{self.application_records}", log=False)
            if self.application_records <= 3 or self.application_records % 500 == 0:
                address = int.from_bytes(record[1:3], "big")
                print(
                    f"[{timestamp()}] PEX record #{self.application_records}: "
                    f"type={record_type:02X}, address={address:04X}, "
                    f"data={record[0]} B, total={len(self.application_data)} B"
                )

            if record_type == 0x01:
                trailing = bytes(self.application_buffer)
                self.application_buffer.clear()
                self.save_part("application", bytes(self.application_data))
                self.save_part("complete_stream", bytes(self.all_data))
                self.phase = "done"
                print(
                    f"[{timestamp()}] Update completed: "
                    f"{self.application_records} records, "
                    f"{len(self.application_data)} B"
                )
                if trailing:
                    self.save_part("after_done", trailing)
                    send(self.sock, b"A", "final acknowledgement")
                return

    def on_idle(self) -> None:
        """Retained for compatibility; fixed-size phases do not use idle gaps."""


def parse_args():
    parser = argparse.ArgumentParser(description="TeeJet PEX update emulator")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--device-id", choices=("a5", "c5", "d5"), default="a5",
        help="Bootstrap device identifier (default: A5)",
    )
    parser.add_argument(
        "--idle", type=float, default=0.25,
        help="Deprecated compatibility option; fixed phase sizes are used",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    device_id = int(args.device_id, 16)
    sock = socket.create_connection((args.host, args.port))
    # Single-byte ACKs must be sent immediately. Without TCP_NODELAY, TCP added
    # about 40 ms to each of more than 33,000 records.
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    sock.settimeout(0.1)
    emulator = PexEmulator(sock, device_id, args.idle)
    print(f"[{timestamp()}] Connected to {args.host}:{args.port}")
    print(f"[{timestamp()}] PEX device ID={device_id:02X}; waiting for 00 polling")

    try:
        while True:
            try:
                data = sock.recv(65536)
            except socket.timeout:
                emulator.on_idle()
                continue
            if not data:
                print(f"[{timestamp()}] Connection closed")
                break
            emulator.receive(data)
            emulator.on_idle()
    except KeyboardInterrupt:
        print(f"\n[{timestamp()}] Emulator stopped")
    finally:
        sock.close()


if __name__ == "__main__":
    main()
