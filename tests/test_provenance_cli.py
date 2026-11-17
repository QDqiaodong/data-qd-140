"""CLI wiring tests for the provenance subcommand."""
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def scenario_bytes():
    document = {
        "buses": ["U", "V"],
        "devices": [
            {"device_id": "A", "bus": "U",
             "device": {"capacity": 100, "initial": 4, "max_charge": 100,
                        "max_discharge": 100,
                        "charge_efficiency": {"numerator": 1,
                                              "denominator": 1},
                        "discharge_efficiency":
                            {"numerator": 1, "denominator": 2},
                        "requests": [-2, 0]},
             "initial_origins": {"R": "4"}},
            {"device_id": "Bdev", "bus": "V",
             "device": {"capacity": 100, "initial": 0, "max_charge": 100,
                        "max_discharge": 100,
                        "charge_efficiency":
                            {"numerator": 1, "denominator": 2},
                        "discharge_efficiency":
                            {"numerator": 1, "denominator": 1},
                        "requests": [4, -2]},
             "initial_origins": {"R": "0"}}],
        "frames": [
            {"injections": [{"source_id": "B", "bus": "U", "energy": "2"}],
             "links": [
                 {"link_id": "l1", "from_bus": "U", "to_bus": "V",
                  "sent": "14", "efficiency": "1"},
                 {"link_id": "l2", "from_bus": "V", "to_bus": "U",
                  "sent": "10", "efficiency": "1"}],
             "sinks": []},
            {"injections": [], "links": [],
             "sinks": [{"sink_id": "t1", "bus": "V", "energy": "2"}]}],
    }
    return json.dumps(document).encode()


class ProvenanceCliTests(unittest.TestCase):
    def call(self, source, output):
        return subprocess.run(
            [sys.executable, "-m", "storage_dispatch.cli", "provenance",
             "--source", str(source), "--output", str(output)],
            capture_output=True, text=True)

    def test_cli_success_and_sha(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "in.json"
            out = Path(td) / "out.json"
            src.write_bytes(scenario_bytes())
            result = self.call(src, out)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(out.read_text())
            self.assertEqual(report["schema_version"], 2)
            self.assertEqual(report["source_sha256"],
                             hashlib.sha256(src.read_bytes()).hexdigest())
            self.assertEqual(len(report["slots"]), 2)
            # Idempotent rerun produces identical bytes.
            first = out.read_bytes()
            self.assertEqual(self.call(src, out).returncode, 0)
            self.assertEqual(out.read_bytes(), first)

    def test_failure_exit_2_preserves_output(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "bad.json"
            out = Path(td) / "out.json"
            out.write_text("prior")
            for body in (b"{}", b'{"buses":[],"buses":[]}', b"NaN",
                         b'{"buses":["b0"],"devices":[],'
                         b'"frames":[]}'):
                src.write_bytes(body)
                result = self.call(src, out)
                self.assertEqual(result.returncode, 2, body)
                self.assertEqual(out.read_text(), "prior")

    def test_same_path_and_symlink_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "in.json"
            src.write_bytes(scenario_bytes())
            self.assertEqual(self.call(src, src).returncode, 2)
            link = Path(td) / "link.json"
            link.symlink_to(src)
            out = Path(td) / "out.json"
            result = self.call(src, link)
            self.assertEqual(result.returncode, 2)
            self.assertFalse(out.exists())

    def test_legacy_replay_still_works(self):
        with tempfile.TemporaryDirectory() as td:
            src = ROOT / "fixtures" / "input.json"
            out = Path(td) / "v1.json"
            result = subprocess.run(
                [sys.executable, "-m", "storage_dispatch.cli",
                 "--source", str(src), "--output", str(out)],
                capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(out.read_text())["schema_version"], 1)

    def test_input_size_cap_is_enforced(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "big.json"
            out = Path(td) / "out.json"
            # A valid-looking document padded past the 8 MB cap.
            padding = b" " * (8_000_001)
            src.write_bytes(scenario_bytes()[:-1] + padding + b"}")
            result = self.call(src, out)
            self.assertEqual(result.returncode, 2)
            self.assertIn("8 MB", result.stderr)
            self.assertFalse(out.exists())


if __name__ == "__main__":
    unittest.main()
