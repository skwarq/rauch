#!/usr/bin/env python3
"""TCP logger and emulator for the RAUCH WLAN application protocols.

Examples:
  sudo ip addr add 152.21.0.31/24 dev <interface>
  python3 protocols/rauch-wlan/rauch_wlan.py

  python3 protocols/rauch-wlan/rauch_wlan.py \
      --host 0.0.0.0 --port 58200 --weight-kg 123.45
  python3 protocols/rauch-wlan/rauch_wlan.py --reply-hex "4f4b0d0a"
"""

from __future__ import annotations

import argparse
import datetime as dt
import socket
import struct
import sys
import threading
from pathlib import Path


def hex_dump(data: bytes, width: int = 16) -> str:
    lines: list[str] = []
    for offset in range(0, len(data), width):
        chunk = data[offset:offset + width]
        hex_part = " ".join(f"{byte:02X}" for byte in chunk)
        ascii_part = "".join(chr(byte) if 32 <= byte <= 126 else "." for byte in chunk)
        lines.append(f"{offset:04X}  {hex_part:<{width * 3 - 1}}  |{ascii_part}|")
    return "\n".join(lines)


def timestamp() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="milliseconds")


def fixed_text(data: bytes, start: int, size: int) -> str:
    """Read a zero-terminated string from a fixed-width frame field."""
    raw = data[start:start + size].split(b"\0", 1)[0]
    return raw.decode("latin-1", errors="replace").strip()


def validate_rauch_frame(data: bytes) -> str | None:
    """Return an error description, or ``None`` for a supported XF1 frame."""
    if len(data) != 206:
        return f"expected 206 bytes, received {len(data)}"
    if struct.unpack_from("<I", data, 0)[0] != len(data):
        return "the length field does not match the received frame"
    if struct.unpack_from("<I", data, 8)[0] != 3:
        return "unsupported XF1 version"
    if struct.unpack_from("<H", data, 12)[0] != 1:
        return "unsupported XF1 record count"
    stored_checksum = struct.unpack_from("<I", data, 4)[0]
    calculated_checksum = sum(data[16:]) & 0xFFFF
    if stored_checksum != calculated_checksum:
        return (
            f"checksum mismatch: stored {stored_checksum}, "
            f"calculated {calculated_checksum}"
        )
    return None


def decode_rauch_settings(data: bytes) -> dict[str, object] | None:
    """Decode a valid, supported 206-byte RAUCH XF1 settings frame."""
    if validate_rauch_frame(data) is not None:
        return None

    disc_keys = {47: "S1"}
    mounting_heights = {
        0: "none",
        1: "0 / 6",
        2: "40 / 40",
        3: "50 / 50",
        4: "60 / 60",
        5: "70 / 70",
        6: "70 / 76",
    }
    disc_key = data[201]
    disc = disc_keys.get(disc_key, f"key {disc_key}")
    return {
        "checksum": struct.unpack_from("<I", data, 4)[0],
        "checksum_calculated": sum(data[16:]) & 0xFFFF,
        "file_version": struct.unpack_from("<I", data, 8)[0],
        "record_count": struct.unpack_from("<H", data, 12)[0],
        "machine_type": data[14],
        "overwrite": data[15],
        "record_number": data[16],
        "valid": data[17],
        "disc": disc,
        "disc_key": disc_key,
        "normal_drop_point": data[18] / 10,
        "edge_drop_point": data[19] / 10,
        "border_drop_point": data[20] / 10,
        "mounting_height_code": data[21],
        "mounting_height": mounting_heights.get(data[21], "unknown"),
        "normal_rpm": struct.unpack_from("<H", data, 47)[0],
        "edge_rpm": struct.unpack_from("<H", data, 49)[0],
        "border_rpm": struct.unpack_from("<H", data, 51)[0],
        "composition": fixed_text(data, 26, 21),
        "flow_factor": struct.unpack_from("<I", data, 176)[0] / 100,
        "wing_setting": fixed_text(data, 180, 6),
        "distance_factor": struct.unpack_from("<H", data, 186)[0],
        "edge_setting_text": fixed_text(data, 188, 6),
        "border_setting_text": fixed_text(data, 194, 6),
        "telimat_amount": data[200],
        "min_split": struct.unpack_from("<H", data, 202)[0],
        "max_split": struct.unpack_from("<H", data, 204)[0],
        "manufacturer": fixed_text(data, 53, 61),
        "product": fixed_text(data, 114, 61),
        "working_width_mm": struct.unpack_from("<I", data, 22)[0],
        "fertilization_type": data[175],
    }


def print_rauch_settings(settings: dict[str, object]) -> None:
    print("\n========== RAUCH FERTILIZER PROFILE ==========")
    print(f"Fertilizer:         {settings['product']}")
    print(f"Manufacturer:       {settings['manufacturer']}")
    print(f"Composition:        {settings['composition']}")
    checksum_ok = settings["checksum"] == settings["checksum_calculated"]
    print(f"Content checksum:   {settings['checksum']} ({'OK' if checksum_ok else 'ERROR'})")
    print(f"File version:       {settings['file_version']}")
    print(f"Record count:       {settings['record_count']}")
    print(f"Machine type:       {settings['machine_type']}")
    print(f"Number / validity:  {settings['record_number']} / {settings['valid']}")
    width_mm = settings["working_width_mm"]
    print(f"Working width:      {width_mm / 1000:g} m ({width_mm} mm)")
    print(f"Spreading disc:     {settings['disc']} (key {settings['disc_key']})")
    print(f"Mounting height:    {settings['mounting_height']} (code {settings['mounting_height_code']})")
    print("Normal spreading:")
    print(f"  Feed point:       {settings['normal_drop_point']:g}")
    print(f"  Speed:            {settings['normal_rpm']} RPM")
    print(f"  Distance factor:  {settings['distance_factor']}")
    print("Border spreading:")
    print(f"  Feed point:       {settings['border_drop_point']:g}")
    print(f"  Speed:            {settings['border_rpm']} RPM")
    print("Edge spreading:")
    print(f"  Feed point:       {settings['edge_drop_point']:g}")
    print(f"  Speed:            {settings['edge_rpm']} RPM")
    print(f"Flow factor:        {settings['flow_factor']:.2f}")
    print(f"Vane setting:       {settings['wing_setting'] or '-'}")
    print(f"Edge/border text:   {settings['edge_setting_text'] or '-'} / {settings['border_setting_text'] or '-'}")
    print(f"TELIMAT / split:    {settings['telimat_amount']} / {settings['min_split']}..{settings['max_split']}")
    print(f"Fertilization type: {settings['fertilization_type']}")
    print("=========================================\n")


def extract_rauch_frames(buffer: bytearray) -> list[bytes]:
    """Extract complete length-prefixed frames from a TCP stream buffer."""
    frames: list[bytes] = []
    while len(buffer) >= 4:
        frame_size = struct.unpack_from("<I", buffer, 0)[0]
        if frame_size < 4 or frame_size > 64 * 1024:
            # Unknown prefix: discard the undecodable stream fragment.
            buffer.clear()
            break
        if len(buffer) < frame_size:
            break
        frames.append(bytes(buffer[:frame_size]))
        del buffer[:frame_size]
    return frames


def decode_weight_frame(data: bytes) -> float:
    """Decode a 4-byte port 58200 sample as kilograms."""
    if len(data) != 4:
        raise ValueError("A weight frame must contain exactly 4 bytes")
    return struct.unpack("<f", data)[0] / 1000.0


def encode_weight_frame(kg: float) -> bytes:
    """Encode kilograms as a little-endian float32 multiplied by 1000."""
    return struct.pack("<f", kg * 1000.0)


def extract_weight_frames(buffer: bytearray) -> list[bytes]:
    """Split a port 58200 stream into consecutive 4-byte weight samples."""
    frames: list[bytes] = []
    while len(buffer) >= 4:
        frames.append(bytes(buffer[:4]))
        del buffer[:4]
    return frames


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="RAUCH application TCP logger/emulator")
    parser.add_argument("--host", default="152.21.0.31",
                        help="Listen address (default: 152.21.0.31)")
    parser.add_argument(
        "--port", dest="ports", type=int, action="append",
        help="TCP port (repeatable; defaults to 58200 and 8172)",
    )
    default_output = Path(__file__).resolve().parent / "captures"
    parser.add_argument("--output", default=str(default_output),
                        help="Session log directory")
    parser.add_argument("--recv-size", type=int, default=4096,
                        help="Size of a single recv() call")
    parser.add_argument("--timeout", type=float, default=30.0,
                        help="Client timeout in seconds")
    parser.add_argument("--reply-hex", default=None,
                        help="Optional hexadecimal reply sent after each received chunk")
    parser.add_argument(
        "--weight-kg", type=float, default=None,
        help="On port 58200, send this weight in kg as float32 LE ×1000",
    )
    parser.add_argument(
        "--weight-interval", type=float, default=1.0,
        help="Weight sample interval in seconds (default: 1.0)",
    )
    return parser.parse_args()


def handle_client(
    conn: socket.socket,
    peer: tuple[str, int],
    host: str,
    port: int,
    output_dir: Path,
    timeout: float,
    recv_size: int,
    reply: bytes | None,
    weight_kg: float | None,
    weight_interval: float,
    session_no: int,
) -> None:
    session_id = dt.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    bin_path = output_dir / f"session_{session_id}_{peer[0]}_{peer[1]}.bin"
    log_path = output_dir / f"session_{session_id}_{peer[0]}_{peer[1]}.txt"

    print(f"\n[{timestamp()}] PORT {port} SESSION #{session_no}: connection from {peer[0]}:{peer[1]}")
    weight_mode = port == 58200 and weight_kg is not None
    conn.settimeout(weight_interval if weight_mode else timeout)
    total = 0
    packet_no = 0
    protocol_buffer = bytearray()

    with conn, bin_path.open("wb") as bin_file, log_path.open(
        "w", encoding="utf-8"
    ) as log_file:
        log_file.write(
            f"Start: {timestamp()}\n"
            f"Peer: {peer[0]}:{peer[1]}\n"
            f"Local: {host}:{port}\n\n"
        )

        def send_weight() -> None:
            frame = encode_weight_frame(weight_kg)
            conn.sendall(frame)
            msg = (
                f"[{timestamp()}] PORT 58200 TX WEIGHT: {weight_kg:g} kg, "
                f"raw={frame.hex(' ').upper()}"
            )
            print(msg)
            log_file.write(msg + "\n")
            log_file.flush()

        if weight_mode:
            try:
                send_weight()
            except OSError as exc:
                msg = f"[{timestamp()}] Port 58200: initial weight TX failed: {exc}"
                print(msg)
                log_file.write(msg + "\n")
                return

        while True:
            try:
                data = conn.recv(recv_size)
            except socket.timeout:
                if weight_mode:
                    try:
                        send_weight()
                    except (BrokenPipeError, ConnectionResetError):
                        msg = f"[{timestamp()}] Port 58200: client closed the weight stream"
                        print(msg)
                        log_file.write(msg + "\n")
                        break
                    continue
                msg = f"[{timestamp()}] Port {port}: timeout after {timeout} s"
                print(msg)
                log_file.write(msg + "\n")
                break
            except ConnectionResetError:
                msg = f"[{timestamp()}] Port {port}: client reset the connection"
                print(msg)
                log_file.write(msg + "\n")
                break
            except OSError as exc:
                msg = f"[{timestamp()}] Port {port}: receive error: {exc}"
                print(msg)
                log_file.write(msg + "\n")
                break

            if not data:
                msg = f"[{timestamp()}] Port {port}: client closed the connection"
                print(msg)
                log_file.write(msg + "\n")
                break

            packet_no += 1
            total += len(data)
            bin_file.write(data)
            bin_file.flush()
            dump = hex_dump(data)
            header = (
                f"[{timestamp()}] PORT {port} RX #{packet_no}: "
                f"{len(data)} B, total {total} B"
            )
            print(header)
            print(dump)
            print()
            log_file.write(header + "\n")
            log_file.write(dump + "\n\n")
            log_file.flush()

            if port == 8172:
                protocol_buffer.extend(data)
                for frame in extract_rauch_frames(protocol_buffer):
                    validation_error = validate_rauch_frame(frame)
                    settings = decode_rauch_settings(frame)
                    if settings is not None:
                        print_rauch_settings(settings)
                    else:
                        print(
                            f"[{timestamp()}] Port 8172: rejected {len(frame)} B "
                            f"frame: {validation_error or 'unknown format'}"
                        )

            if port == 58200:
                protocol_buffer.extend(data)
                for frame in extract_weight_frames(protocol_buffer):
                    kg = decode_weight_frame(frame)
                    msg = (
                        f"[{timestamp()}] PORT 58200 WEIGHT RX: {kg:g} kg, "
                        f"raw={frame.hex(' ').upper()}"
                    )
                    print(msg)
                    log_file.write(msg + "\n")
                    log_file.flush()

            if reply is not None:
                try:
                    conn.sendall(reply)
                except OSError as exc:
                    msg = f"[{timestamp()}] Port {port}: reply failed: {exc}"
                    print(msg)
                    log_file.write(msg + "\n")
                    break
                tx_header = f"[{timestamp()}] PORT {port} TX: {len(reply)} B"
                tx_dump = hex_dump(reply)
                print(tx_header)
                print(tx_dump)
                print()
                log_file.write(tx_header + "\n")
                log_file.write(tx_dump + "\n\n")
                log_file.flush()

    print(
        f"[{timestamp()}] Port {port}: end of session #{session_no}; "
        f"received {total} B\nBIN: {bin_path}\nLOG: {log_path}"
    )


def listen(
    host: str,
    port: int,
    output_root: Path,
    timeout: float,
    recv_size: int,
    reply: bytes | None,
    weight_kg: float | None,
    weight_interval: float,
) -> None:
    output_dir = output_root / f"port_{port}"
    output_dir.mkdir(parents=True, exist_ok=True)

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((host, port))
        server.listen(5)
        print(f"[{timestamp()}] Listening on TCP {host}:{port}")
        print(f"Port {port} data: {output_dir.resolve()}")

        session_no = 0
        while True:
            conn, peer = server.accept()
            session_no += 1
            threading.Thread(
                target=handle_client,
                args=(conn, peer, host, port, output_dir, timeout, recv_size,
                      reply, weight_kg, weight_interval, session_no),
                daemon=True,
            ).start()


def main() -> int:
    args = parse_args()
    ports = args.ports or [58200, 8172]
    if len(ports) != len(set(ports)):
        print("Each port may only be specified once", file=sys.stderr)
        return 2
    if args.weight_interval <= 0:
        print("--weight-interval must be greater than zero", file=sys.stderr)
        return 2
    if args.recv_size <= 0:
        print("--recv-size must be greater than zero", file=sys.stderr)
        return 2
    if args.timeout <= 0:
        print("--timeout must be greater than zero", file=sys.stderr)
        return 2
    if any(port < 1 or port > 65535 for port in ports):
        print("Ports must be in the range 1..65535", file=sys.stderr)
        return 2

    reply: bytes | None = None
    if args.reply_hex:
        try:
            reply = bytes.fromhex(args.reply_hex)
        except ValueError as exc:
            print(f"Invalid --reply-hex: {exc}", file=sys.stderr)
            return 2

    output_root = Path(args.output)
    output_root.mkdir(parents=True, exist_ok=True)
    print("Press Ctrl+C to stop.\n")

    threads = [
        threading.Thread(
            target=listen,
            args=(args.host, port, output_root, args.timeout, args.recv_size,
                  reply, args.weight_kg, args.weight_interval),
            daemon=True,
        )
        for port in ports
    ]
    for thread in threads:
        thread.start()

    try:
        while all(thread.is_alive() for thread in threads):
            for thread in threads:
                thread.join(timeout=0.5)
    except KeyboardInterrupt:
        print("\nStopping.")
        return 0

    print("A listener stopped because of an error.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
