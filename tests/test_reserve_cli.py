"""CLI tests for the reservations subcommand (v1 replay stays untouched)."""
import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
ENV = dict(os.environ, PYTHONPATH=SRC)


class ReservationCliTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.mkdtemp()
        self.db = os.path.join(self.td, "book.db")
        self.init = os.path.join(self.td, "init.json")
        with open(self.init, "w") as fh:
            json.dump({
                "capacity": 100, "initial": 10,
                "charge_efficiency": {"numerator": 1, "denominator": 1},
                "discharge_efficiency": {"numerator": 1, "denominator": 1},
                "terminal_min": 0,
                "slots": [
                    {"charge_limit": 50, "discharge_limit": 0, "price": 1},
                    {"charge_limit": 0, "discharge_limit": 50, "price": 5}]},
                fh)

    def cli(self, *args, input_text=None):
        return subprocess.run(
            [sys.executable, "-m", "storage_dispatch.cli", *args],
            capture_output=True, text=True, env=ENV, input=input_text)

    def test_full_reservation_workflow_and_status(self):
        r = self.cli("reservations", "--db", self.db, "init", self.init)
        self.assertEqual(r.returncode, 0, r.stderr)
        add = json.dumps({"request_id": "r1", "contract_id": "A",
                          "start": 1, "end": 2, "energy": 4})
        r = self.cli("reservations", "--db", self.db, "add", "-",
                     input_text=add)
        self.assertEqual(r.returncode, 0, r.stderr)
        receipt = json.loads(r.stdout)
        self.assertEqual(receipt["operation"], "add")
        r = self.cli("reservations", "--db", self.db, "status")
        self.assertEqual(r.returncode, 0, r.stderr)
        status = json.loads(r.stdout)
        self.assertEqual(status["revision"], 1)
        self.assertEqual(status["contracts"][0]["contract_id"], "A")

    def test_validation_failure_exit_code_two(self):
        self.cli("reservations", "--db", self.db, "init", self.init)
        bad = json.dumps({"request_id": "r", "contract_id": "A",
                          "start": 5, "end": 6, "energy": 1})  # start past end
        r = self.cli("reservations", "--db", self.db, "add", "-",
                     input_text=bad)
        self.assertEqual(r.returncode, 2)
        self.assertTrue(r.stderr.strip())

    def test_legacy_v1_replay_still_works(self):
        out = os.path.join(self.td, "report.json")
        r = self.cli("--source",
                     os.path.join(ROOT, "fixtures", "input.json"),
                     "--output", out)
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(out) as fh:
            report = json.load(fh)
        self.assertEqual(report["schema_version"], 1)


if __name__ == "__main__":
    unittest.main()
