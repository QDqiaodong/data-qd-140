"""Strict provenance input contract tests."""
import json
import unittest

from storage_dispatch.provenance import (parse_strict_json,
                                         validate_document,
                                         ProvenanceError)


def device(initial=0, requests=None, eta_c=(1, 1), eta_d=(1, 1)):
    return {
        "capacity": 100, "initial": initial, "max_charge": 50,
        "max_discharge": 50,
        "charge_efficiency": {"numerator": eta_c[0],
                              "denominator": eta_c[1]},
        "discharge_efficiency": {"numerator": eta_d[0],
                                 "denominator": eta_d[1]},
        "requests": requests if requests is not None else [0]}


def document(**overrides):
    data = {
        "buses": ["b0"],
        "devices": [{
            "device_id": "D", "bus": "b0", "device": device(),
            "initial_origins": {"R": "0"}}],
        "frames": [{"injections": [], "links": [], "sinks": []}],
    }
    data.update(overrides)
    return data


def validate(raw_obj):
    return validate_document(parse_strict_json(json.dumps(raw_obj).encode()))


class SchemaTests(unittest.TestCase):
    def reject(self, raw_obj):
        with self.assertRaises(ProvenanceError):
            validate(raw_obj)

    def test_minimal_valid(self):
        normalized = validate(document())
        self.assertEqual(normalized["buses"], ["b0"])
        self.assertEqual(normalized["sources"], {"R"})

    def test_invalid_utf8_duplicate_keys_constants(self):
        with self.assertRaises(ProvenanceError):
            parse_strict_json(b'{"buses":[],"buses":[]}')
        with self.assertRaises(ProvenanceError):
            parse_strict_json(b'{"buses": [NaN]}')
        with self.assertRaises(ProvenanceError):
            parse_strict_json(b'\xff\xfe')

    def test_top_level_shape(self):
        bad = document()
        del bad["buses"]
        self.reject(bad)
        bad = document()
        bad["extra"] = 1
        self.reject(bad)
        self.reject({"buses": [], "devices": [], "frames": []})
        self.reject({"buses": ["b%d" % i for i in range(33)],
                     "devices": [], "frames": []})

    def test_identifiers(self):
        self.reject(document(buses=["bad id"]))
        self.reject(document(buses=[""]))
        long_id = "x" * 65
        self.reject(document(buses=[long_id]))
        self.reject(document(buses=["b0", "b0"]))
        good = document(buses=["b0", "Z9"])
        good["devices"][0]["bus"] = "Z9"
        validate(good)

    def test_unknown_references_and_duplicates(self):
        bad = document()
        bad["devices"][0]["bus"] = "nope"
        self.reject(bad)
        frame = {"injections": [
            {"source_id": "R", "bus": "nope", "energy": "1"}],
            "links": [], "sinks": []}
        self.reject(document(frames=[frame]))
        dup = {"injections": [
            {"source_id": "R", "bus": "b0", "energy": "1"},
            {"source_id": "R", "bus": "b0", "energy": "2"}],
            "links": [], "sinks": []}
        self.reject(document(frames=[dup]))
        self.reject(document(devices=[
            {"device_id": "D", "bus": "b0", "device": device(),
             "initial_origins": {"R": "0"}},
            {"device_id": "D", "bus": "b0", "device": device(),
             "initial_origins": {"R": "0"}}]))

    def test_canonical_rationals(self):
        for value in ("1.5", "3/1", " 1", "1e2", "-1", "2/0", True, 3):
            frame = {"injections": [
                {"source_id": "R", "bus": "b0", "energy": value}],
                "links": [], "sinks": []}
            self.reject(document(frames=[frame]))
        oversized = "1000000000001"
        frame = {"injections": [
            {"source_id": "R", "bus": "b0", "energy": oversized}],
            "links": [], "sinks": []}
        self.reject(document(frames=[frame]))
        frame = {"injections": [], "sinks": [], "links": [
            {"link_id": "l", "from_bus": "b0", "to_bus": "b0",
             "sent": "1", "efficiency": "1"}]}
        self.reject(document(frames=[frame]))
        frame = {"injections": [], "sinks": [], "links": [
            {"link_id": "l", "from_bus": "b0", "to_bus": "b0",
             "sent": "1", "efficiency": "0"}]}
        self.reject(document(frames=[frame]))

    def test_v1_device_ranges_and_booleans(self):
        bad_device = device()
        bad_device["capacity"] = True
        self.reject(document(devices=[
            {"device_id": "D", "bus": "b0", "device": bad_device,
             "initial_origins": {"R": "0"}}]))
        bad_device = device()
        bad_device["requests"] = [1, 2]
        # frames length must match requests length.
        self.reject(document(devices=[
            {"device_id": "D", "bus": "b0", "device": bad_device,
             "initial_origins": {"R": "0"}}]))
        bad_device = device(initial=5)
        self.reject(document(devices=[
            {"device_id": "D", "bus": "b0", "device": bad_device,
             "initial_origins": {"R": "4"}}]))
        # initial_origins total must equal device.initial.
        self.reject(document(devices=[
            {"device_id": "D", "bus": "b0",
             "device": device(initial=3),
             "initial_origins": {"R": "1", "G": "1"}}]))

    def test_request_length_mismatch_between_devices(self):
        self.reject(document(devices=[
            {"device_id": "D1", "bus": "b0", "device": device(requests=[0]),
             "initial_origins": {"R": "0"}},
            {"device_id": "D2", "bus": "b0",
             "device": device(requests=[0, 0]),
             "initial_origins": {"R": "0"}}]))

    def test_slot_array_limits(self):
        frame = {"injections": [
            {"source_id": "S%d" % i, "bus": "b0", "energy": "1"}
            for i in range(65)], "links": [], "sinks": []}
        self.reject(document(frames=[frame]))
        frame = {"injections": [], "sinks": [],
                 "links": [{"link_id": "l%d" % i, "from_bus": "b0",
                            "to_bus": "b0", "sent": "0",
                            "efficiency": "1"} for i in range(129)]}
        self.reject(document(frames=[frame]))

    def test_sixty_four_source_cap(self):
        # R already exists in the device initial_origins, so the frames may
        # add at most 63 further distinct sources.
        sources = ["S%02d" % i for i in range(64)]
        frames = [{"injections": [
            {"source_id": s, "bus": "b0", "energy": "0"} for s in sources],
            "links": [], "sinks": []}]
        self.reject(document(frames=frames))
        within = sources[:63]
        frames = [{"injections": [
            {"source_id": s, "bus": "b0", "energy": "0"} for s in within],
            "links": [], "sinks": []}]
        validate(document(frames=frames))


if __name__ == "__main__":
    unittest.main()
