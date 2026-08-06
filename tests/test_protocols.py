import contextlib
import importlib.util
import io
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def load_module(name, relative_path):
    path = ROOT / relative_path
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rauch = load_module("rauch_wlan_tests", "protocols/rauch-wlan/rauch_wlan.py")
lhfs = load_module("lhfs_tests", "protocols/lhfs/lhfs_emulator.py")
pex = load_module("pex_emulator_tests", "protocols/pex/pex_emulator.py")
pex_extract = load_module("pex_extract_tests", "protocols/pex/pex_extract.py")


def make_xf1(*, version=3, record_count=1):
    frame = bytearray(206)
    struct.pack_into("<I", frame, 0, len(frame))
    struct.pack_into("<I", frame, 8, version)
    struct.pack_into("<H", frame, 12, record_count)
    frame[16] = 1
    frame[17] = 1
    frame[114:119] = b"UREA\0"
    struct.pack_into("<I", frame, 4, sum(frame[16:]) & 0xFFFF)
    return bytes(frame)


def intel_record(record_type, payload=b"", address=0):
    body = bytes([len(payload)]) + address.to_bytes(2, "big") + bytes([record_type]) + payload
    return body + bytes([-sum(body) & 0xFF])


class FakeSocket:
    def __init__(self):
        self.sent = []

    def sendall(self, data):
        self.sent.append(bytes(data))


class RauchTests(unittest.TestCase):
    def test_valid_frame_decodes(self):
        decoded = rauch.decode_rauch_settings(make_xf1())
        self.assertIsNotNone(decoded)
        self.assertEqual(decoded["product"], "UREA")

    def test_bad_checksum_and_unsupported_header_are_rejected(self):
        bad_checksum = bytearray(make_xf1())
        bad_checksum[-1] ^= 1
        self.assertIsNone(rauch.decode_rauch_settings(bytes(bad_checksum)))
        self.assertIn("checksum mismatch", rauch.validate_rauch_frame(bytes(bad_checksum)))
        self.assertIsNone(rauch.decode_rauch_settings(make_xf1(version=4)))
        self.assertIsNone(rauch.decode_rauch_settings(make_xf1(record_count=2)))

    def test_tcp_fragmentation_and_multiple_frames(self):
        frame = make_xf1()
        buffer = bytearray(frame[:73])
        self.assertEqual(rauch.extract_rauch_frames(buffer), [])
        buffer.extend(frame[73:] + frame)
        self.assertEqual(rauch.extract_rauch_frames(buffer), [frame, frame])
        self.assertEqual(buffer, bytearray())

    def test_weight_stream_fragmentation(self):
        first = rauch.encode_weight_frame(123.45)
        second = rauch.encode_weight_frame(-5)
        buffer = bytearray(first[:2])
        self.assertEqual(rauch.extract_weight_frames(buffer), [])
        buffer.extend(first[2:] + second)
        frames = rauch.extract_weight_frames(buffer)
        self.assertEqual(len(frames), 2)
        self.assertAlmostEqual(rauch.decode_weight_frame(frames[0]), 123.45, places=3)
        self.assertAlmostEqual(rauch.decode_weight_frame(frames[1]), -5, places=3)


class LhfsTests(unittest.TestCase):
    def setUp(self):
        self.original_files = dict(lhfs.VIRTUAL_FILES)
        lhfs.UPLOAD_STATE = None

    def tearDown(self):
        lhfs.VIRTUAL_FILES.clear()
        lhfs.VIRTUAL_FILES.update(self.original_files)
        lhfs.UPLOAD_STATE = None

    def test_wakeup_frame(self):
        self.assertEqual(
            lhfs.build_lhfs_frame(0x0000, b"\x01"),
            bytes.fromhex("00 00 01 01 02 00"),
        )

    def test_nv_usage_tracks_current_directory(self):
        lhfs.VIRTUAL_FILES["EXTRA.BIN"] = {
            "name": "EXTRA.BIN", "data": b"x" * 1000, "handle": 99
        }
        _, response, _ = lhfs.lhfs_response(0x1331, b"")
        payload = response[3:-2]
        reported = struct.unpack_from("<I", payload, 1)[0]
        expected = sum(len(item["data"]) for item in lhfs.VIRTUAL_FILES.values())
        self.assertEqual(reported, expected)

    def test_oversized_file_read_returns_error(self):
        lhfs.VIRTUAL_FILES["LARGE.BIN"] = {
            "name": "LARGE.BIN", "data": b"x" * 65536, "handle": 7
        }
        _, response, _ = lhfs.lhfs_response(0x1302, struct.pack("<HH", 7, 0))
        payload = response[3:-2]
        self.assertEqual(payload[0], 1)
        self.assertEqual(struct.unpack_from("<H", payload, 3)[0], 0)

    def test_out_of_order_upload_packet_is_not_appended(self):
        lhfs.UPLOAD_STATE = {
            "name": "ORDER.BIN", "size": 3, "packet_count": 1,
            "next_packet": 0, "data": bytearray(),
        }
        payload = struct.pack("<HB", 1, 3) + b"bad"
        lhfs.lhfs_response(0x1309, payload)
        self.assertEqual(lhfs.UPLOAD_STATE["data"], bytearray())
        self.assertEqual(lhfs.UPLOAD_STATE["next_packet"], 0)


class PexEmulatorTests(unittest.TestCase):
    def test_partial_level1_is_not_accepted_after_idle(self):
        sock = FakeSocket()
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            pex, "OUTPUT_DIR", Path(directory)
        ):
            emulator = pex.PexEmulator(sock, 0xA5, 0.01)
            with contextlib.redirect_stdout(io.StringIO()):
                emulator.receive(b"\x00" + b"x" * 16)
                emulator.on_idle()
            self.assertEqual(emulator.phase, "level1")
            self.assertEqual(sock.sent, [b"\xA5"])

    def test_combined_bootstrap_and_application_stream(self):
        sock = FakeSocket()
        eof = intel_record(0x01)
        stream = b"\x00" + b"1" * pex.LEVEL1_SIZE + b"2" * pex.LEVEL2_SIZE + eof
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            pex, "OUTPUT_DIR", Path(directory)
        ), mock.patch.object(pex.time, "sleep"):
            emulator = pex.PexEmulator(sock, 0xA5, 0.25)
            with contextlib.redirect_stdout(io.StringIO()):
                emulator.receive(stream)
            self.assertEqual(emulator.phase, "done")
            self.assertEqual(sock.sent, [b"\xA5", b"1", b"2", b"F", b"A", b"A"])
            self.assertEqual(emulator.application_data, eof)
            self.assertEqual(len(emulator.all_data), pex.LEVEL1_SIZE + pex.LEVEL2_SIZE + len(eof))

    def test_unknown_intel_record_type_returns_e(self):
        sock = FakeSocket()
        emulator = pex.PexEmulator(sock, 0xA5, 0.25)
        emulator.phase = "application"
        with contextlib.redirect_stdout(io.StringIO()):
            emulator.receive(intel_record(0x06))
            emulator.receive(b"ignored after error")
        self.assertEqual(emulator.phase, "error")
        self.assertEqual(sock.sent, [b"E"])
        self.assertEqual(emulator.phase_data, bytearray())


class PexExtractorTests(unittest.TestCase):
    def test_requires_eof_and_rejects_data_after_eof(self):
        data = intel_record(0x00, b"x")
        with self.assertRaisesRegex(ValueError, "no EOF"):
            pex_extract.parse_intel_hex(b":" + data.hex().encode())

        eof = intel_record(0x01)
        lines = b":" + eof.hex().encode() + b"\n:" + data.hex().encode()
        with self.assertRaisesRegex(ValueError, "after EOF"):
            pex_extract.parse_intel_hex(lines)

    def test_extraction_removes_only_stale_segments(self):
        eof_line = b":" + intel_record(0x01).hex().upper().encode() + b"\n"
        container = (
            b"<LEVEL1><BIN_FILE>L1</BIN_FILE></LEVEL1>"
            b"<LEVEL2><BIN_FILE>L2</BIN_FILE></LEVEL2>"
            b"<HEX_DATA>" + eof_line + b"</HEX_DATA>"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "test.pex"
            output = root / "output"
            output.mkdir()
            source.write_bytes(container)
            stale = output / "segment_999_0x00000000_0x00000000.bin"
            unrelated = output / "keep.txt"
            stale.write_bytes(b"stale")
            unrelated.write_bytes(b"keep")
            argv = ["pex_extract.py", str(source), "--output", str(output)]
            with mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(io.StringIO()):
                pex_extract.main()
            self.assertFalse(stale.exists())
            self.assertTrue(unrelated.exists())
            self.assertTrue((output / "manifest.json").exists())


if __name__ == "__main__":
    unittest.main()
