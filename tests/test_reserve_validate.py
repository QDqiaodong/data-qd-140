"""Strict request validation tests."""
import json
import unittest

from storage_dispatch.reserve.validate import (
    parse_json, validate_init, validate_add, validate_cancel,
    validate_advance, RequestError)


def slot(cl=1, dl=0, price=1):
    return {"charge_limit": cl, "discharge_limit": dl, "price": price}


class ParseTests(unittest.TestCase):
    def test_rejects_duplicate_keys(self):
        with self.assertRaises(RequestError):
            parse_json(b'{"a":1,"a":2}')

    def test_rejects_non_finite_constants(self):
        for bad in (b"NaN", b"Infinity", b"-Infinity"):
            with self.assertRaises(RequestError):
                parse_json(bad)

    def test_rejects_non_object_init(self):
        with self.assertRaises(RequestError):
            validate_init([1, 2, 3])


class InitValidationTests(unittest.TestCase):
    def base(self, **over):
        body = {
            "capacity": 100, "initial": 10,
            "charge_efficiency": {"numerator": 1, "denominator": 2},
            "discharge_efficiency": {"numerator": 3, "denominator": 4},
            "terminal_min": 5,
            "slots": [slot(1, 0, 2), slot(0, 1, 3)]}
        body.update(over)
        return body

    def test_accepts_valid(self):
        spec = validate_init(self.base())
        self.assertEqual(len(spec["slots"]), 2)

    def test_rejects_unknown_field(self):
        body = self.base()
        body["extra"] = 1
        with self.assertRaises(RequestError):
            validate_init(body)

    def test_rejects_both_limits_positive(self):
        body = self.base(slots=[{"charge_limit": 1, "discharge_limit": 1,
                                 "price": 0}])
        with self.assertRaises(RequestError):
            validate_init(body)

    def test_rejects_negative_or_oversize_price(self):
        with self.assertRaises(RequestError):
            validate_init(self.base(slots=[slot(1, 0, -1)]))
        with self.assertRaises(RequestError):
            validate_init(self.base(slots=[slot(1, 0, 10 ** 9 + 1)]))

    def test_terminal_min_bounds(self):
        with self.assertRaises(RequestError):
            validate_init(self.base(terminal_min=101))

    def test_slot_count_bounds(self):
        with self.assertRaises(RequestError):
            validate_init(self.base(slots=[]))
        validate_init(self.base(slots=[slot() for _ in range(192)]))
        with self.assertRaises(RequestError):
            validate_init(self.base(slots=[slot() for _ in range(193)]))

    def test_efficiency_must_be_at_most_one(self):
        body = self.base(
            charge_efficiency={"numerator": 3, "denominator": 2})
        with self.assertRaises(RequestError):
            validate_init(body)

    def test_boolean_is_not_an_integer(self):
        body = self.base(capacity=True)
        with self.assertRaises(RequestError):
            validate_init(body)


class AddValidationTests(unittest.TestCase):
    def test_window_and_energy_bounds(self):
        known = set()
        req = validate_add(
            {"request_id": "r1", "contract_id": "c1", "start": 0, "end": 3,
             "energy": 5}, cursor=0, slot_count=4, existing_contracts=known)
        self.assertEqual(req["energy"], 5)

    def test_start_must_be_at_or_after_cursor(self):
        with self.assertRaises(RequestError):
            validate_add(
                {"request_id": "r1", "contract_id": "c1", "start": 1,
                 "end": 3, "energy": 5}, cursor=2, slot_count=4,
                existing_contracts=set())

    def test_end_must_exceed_start_and_fit_slots(self):
        with self.assertRaises(RequestError):
            validate_add(
                {"request_id": "r1", "contract_id": "c1", "start": 2,
                 "end": 2, "energy": 5}, cursor=0, slot_count=4,
                existing_contracts=set())
        with self.assertRaises(RequestError):
            validate_add(
                {"request_id": "r1", "contract_id": "c1", "start": 2,
                 "end": 5, "energy": 5}, cursor=0, slot_count=4,
                existing_contracts=set())

    def test_energy_must_be_positive(self):
        with self.assertRaises(RequestError):
            validate_add(
                {"request_id": "r1", "contract_id": "c1", "start": 0,
                 "end": 3, "energy": 0}, cursor=0, slot_count=4,
                existing_contracts=set())

    def test_contract_id_never_reused_and_cap_80(self):
        with self.assertRaises(RequestError):
            validate_add(
                {"request_id": "r", "contract_id": "old", "start": 0,
                 "end": 1, "energy": 1}, cursor=0, slot_count=2,
                existing_contracts={"old"})
        many = {"c%d" % i for i in range(80)}
        with self.assertRaises(RequestError):
            validate_add(
                {"request_id": "r", "contract_id": "new", "start": 0,
                 "end": 1, "energy": 1}, cursor=0, slot_count=2,
                existing_contracts=many)

    def test_identifier_charset_and_length(self):
        good = {"request_id": "r_1-2", "contract_id": "C-9_x",
                "start": 0, "end": 1, "energy": 1}
        validate_add(good, 0, 2, set())
        for bad_id in ("", "has space", "dot.id", "x" * 65, "中文"):
            with self.assertRaises(RequestError):
                validate_add({**good, "contract_id": bad_id}, 0, 2, set())


class CancelAdvanceValidationTests(unittest.TestCase):
    def test_cancel_requires_exact_fields(self):
        validate_cancel({"request_id": "r", "contract_id": "c"})
        with self.assertRaises(RequestError):
            validate_cancel({"request_id": "r"})

    def test_advance_requires_strictly_forward_to_slot(self):
        validate_advance({"request_id": "r", "to_slot": 3}, cursor=2,
                         slot_count=5)
        with self.assertRaises(RequestError):
            validate_advance({"request_id": "r", "to_slot": 2}, cursor=2,
                             slot_count=5)
        with self.assertRaises(RequestError):
            validate_advance({"request_id": "r", "to_slot": 6}, cursor=2,
                             slot_count=5)


if __name__ == "__main__":
    unittest.main()
