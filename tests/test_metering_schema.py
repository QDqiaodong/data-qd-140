"""Strict ingest/invoice schema validation tests."""
import unittest

from storage_dispatch.metering.schema import (parse_strict_json,
                                              validate_ingest,
                                              validate_invoices,
                                              canonical_rational, MeteringError)

DEVICE = {
    "capacity": 10, "initial": 1, "max_charge": 5, "max_discharge": 4,
    "charge_efficiency": {"numerator": 4, "denominator": 5},
    "discharge_efficiency": {"numerator": 2, "denominator": 3},
    "requests": [3, -2, 0, 4],
}


def payload(**overrides):
    data = {"device": DEVICE, "readings": []}
    data.update(overrides)
    return data


def reading(rid="r1", channel="import", start=0, end=1, lower="0", upper="1"):
    return {"reading_id": rid, "channel": channel, "start": start,
            "end": end, "lower": lower, "upper": upper}


class SchemaTests(unittest.TestCase):
    def reject(self, raw):
        with self.assertRaises(MeteringError):
            validate_ingest(parse_strict_json(raw))

    def test_valid_empty_readings(self):
        device, readings, slots = validate_ingest(payload())
        self.assertEqual(slots, 4)
        self.assertEqual(readings, [])
        self.assertEqual(str(device["eta_c"]), "4/5")

    def test_invalid_utf8_and_duplicate_keys(self):
        with self.assertRaises(MeteringError):
            parse_strict_json(b'{"device":{},"readings":\xff}')
        self.reject(b'{"device":{"capacity":1},"device":{},"readings":[]}')

    def test_slot_limits(self):
        d = payload(device=dict(DEVICE, requests=[1] * 25))
        self.reject(__import__("json").dumps(d).encode())
        d = payload(device=dict(DEVICE, requests=[]))
        self.reject(__import__("json").dumps(d).encode())

    def test_bool_and_float_rejected(self):
        d = payload(device=dict(DEVICE, capacity=True))
        self.reject(__import__("json").dumps(d).encode())
        d = payload(readings=[reading(lower="0", upper=1.5)])
        self.reject(__import__("json").dumps(d).encode())
        d = payload(readings=[dict(reading(), lower=True)])
        self.reject(__import__("json").dumps(d).encode())

    def test_reading_count_and_unknown_fields(self):
        d = payload(readings=[reading(f"r{i}") for i in range(65)])
        self.reject(__import__("json").dumps(d).encode())
        bad = reading()
        bad["extra"] = 1
        self.reject(__import__("json").dumps(payload(readings=[bad])).encode())

    def test_reading_ids_channels_windows(self):
        self.reject(__import__("json").dumps(
            payload(readings=[reading("bad id")])).encode())
        self.reject(__import__("json").dumps(
            payload(readings=[reading(channel="wat")])).encode())
        self.reject(__import__("json").dumps(
            payload(readings=[reading(start=2, end=2)])).encode())
        self.reject(__import__("json").dumps(
            payload(readings=[reading(rid="a")] + [reading(rid="a")])).encode())
        self.reject(__import__("json").dumps(
            payload(readings=[reading(lower="2", upper="1")])).encode())

    def test_canonical_rationals(self):
        self.assertEqual(canonical_rational("0", "x"), 0)
        self.assertEqual(canonical_rational("25/8", "x"),
                         __import__("fractions").Fraction(25, 8))
        for bad in ("1.5", "1e2", "3/1", " 1 ", "-1", "0/1", "1000001/1",
                    "5/0", "", "1_000"):
            with self.assertRaises(MeteringError):
                canonical_rational(bad, "x")
        with self.assertRaises(MeteringError):
            canonical_rational("1000000000001", "x", 10 ** 12)

    def test_invoice_schema(self):
        inv = [{"invoice_id": "I0", "start": 0, "end": 2,
                "prices": [1, 2], "window": 1, "demand_rate": 3,
                "amount": "7/2"}]
        parsed = validate_invoices(inv, 4)
        self.assertEqual(parsed[0]["amount"],
                         __import__("fractions").Fraction(7, 2))
        for bad in (
            [dict(inv[0], invoice_id="bad id")],
            [dict(inv[0], prices=[1])],                 # length mismatch
            [dict(inv[0], window=3)],                   # window > span
            [dict(inv[0], start=2, end=2)],
            [dict(inv[0], demand_rate=-1)],
            [dict(inv[0], amount="1.5")],
            [inv[0], dict(inv[0], invoice_id="I0")],    # duplicate id
            [inv[0]] * 5,                               # at most four
        ):
            with self.assertRaises(MeteringError):
                validate_invoices(bad, 4)


if __name__ == "__main__":
    unittest.main()
