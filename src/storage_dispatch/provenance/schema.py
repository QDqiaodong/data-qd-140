"""Strict input contract for multi-battery energy-source provenance.

The accepted document is exactly ``{"buses": ..., "devices": ...,
"frames": ...}``.  Validation is total: unknown or duplicated fields, booleans
posing as integers, floats, non-canonical rationals, unknown identifiers,
duplicate identifiers and illegal UTF-8 all reject the whole document.  All
energy arithmetic downstream consumes the exact :class:`Fraction` values
produced here; nothing is ever parsed as a float.
"""
import json
import re
from fractions import Fraction

from ..common import number
from ..physics import efficiency

ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")

MAX_BUSES = 32
MAX_DEVICES = 16
MAX_SLOTS = 256
MAX_SOURCES = 64
MAX_INJECTIONS = 64
MAX_LINKS = 128
MAX_SINKS = 64
ENERGY_MAX = 10 ** 12
DEN_MAX = 10 ** 6
DEVICE_REQUEST_MAX = 10 ** 9


class ProvenanceError(ValueError):
    """Base class for every provenance validation and tracing failure."""


class Unbalanced(ProvenanceError):
    """A slot's bus energy balance cannot be satisfied exactly."""


class AmbiguousOrigin(ProvenanceError):
    """A positive-flow closed loop has no reachable external/battery source."""


def parse_strict_json(raw):
    """Parse exact UTF-8 JSON *raw* bytes with the strictest settings."""
    if not isinstance(raw, (bytes, bytearray)):
        raise ProvenanceError("input must be JSON bytes")
    raw = bytes(raw)

    def reject_duplicates(pairs):
        seen = set()
        for key, _ in pairs:
            if key in seen:
                raise ProvenanceError("duplicate JSON key: " + str(key))
            seen.add(key)
        return dict(pairs)

    def reject_constant(value):
        raise ProvenanceError("non-finite JSON constant: " + value)

    try:
        return json.loads(raw, object_pairs_hook=reject_duplicates,
                          parse_constant=reject_constant)
    except ProvenanceError:
        raise
    except (ValueError, UnicodeDecodeError) as exc:
        raise ProvenanceError("invalid JSON: " + str(exc))


def _exact(obj, keys, what):
    if not isinstance(obj, dict) or set(obj) != set(keys):
        raise ProvenanceError(
            "%s must be an object with exactly fields: %s"
            % (what, ", ".join(keys)))


def _id(value, what):
    if not isinstance(value, str) or ID_RE.fullmatch(value) is None:
        raise ProvenanceError(
            "%s must be 1..64 ASCII letters, digits, '_' or '-'" % what)
    return value


def canonical_rational(value, what, maximum=ENERGY_MAX, allow_zero=True):
    """Return the exact Fraction behind a canonical rational string.

    The string must equal ``str(Fraction(value))`` (reduced, no exponent or
    whitespace variants), the denominator may not exceed ``10**6`` and the
    value must be non-negative.
    """
    if not isinstance(value, str):
        raise ProvenanceError("%s must be a canonical rational string" % what)
    try:
        parsed = Fraction(value)
    except (ValueError, ZeroDivisionError):
        raise ProvenanceError(
            "%s is not a canonical rational string" % what)
    if str(parsed) != value or parsed.denominator > DEN_MAX:
        raise ProvenanceError(
            "%s is not a canonical rational string" % what)
    if parsed < 0 or (not allow_zero and parsed == 0):
        raise ProvenanceError("%s must be non-negative" % what)
    if maximum is not None and parsed > maximum:
        raise ProvenanceError("%s exceeds %s" % (what, maximum))
    return parsed


def _efficiency_string(value, what):
    """Validate a canonical rational string with ``0 < efficiency <= 1``."""
    frac = canonical_rational(value, what, maximum=Fraction(1),
                              allow_zero=False)
    return frac


def _v1_device(obj, what):
    """Validate the existing complete v1 source object with v1 field ranges."""
    _exact(obj, ["capacity", "initial", "max_charge", "max_discharge",
                 "charge_efficiency", "discharge_efficiency", "requests"],
           what)
    capacity = number(obj["capacity"], 1, DEVICE_REQUEST_MAX)
    initial = number(obj["initial"], 0, capacity)
    max_charge = number(obj["max_charge"], 0, DEVICE_REQUEST_MAX)
    max_discharge = number(obj["max_discharge"], 0, DEVICE_REQUEST_MAX)
    for key in ("charge_efficiency", "discharge_efficiency"):
        eff = obj[key]
        if not isinstance(eff, dict) or set(eff) != {"numerator",
                                                     "denominator"}:
            raise ProvenanceError(
                "%s.%s must be {numerator, denominator}" % (what, key))
    eta_c = efficiency(
        number(obj["charge_efficiency"]["numerator"], 1, DEN_MAX),
        number(obj["charge_efficiency"]["denominator"], 1, DEN_MAX))
    eta_d = efficiency(
        number(obj["discharge_efficiency"]["numerator"], 1, DEN_MAX),
        number(obj["discharge_efficiency"]["denominator"], 1, DEN_MAX))
    requests_raw = obj["requests"]
    if not isinstance(requests_raw, list) or not (
            1 <= len(requests_raw) <= MAX_SLOTS):
        raise ProvenanceError(
            "%s.requests must have 1..%d entries" % (what, MAX_SLOTS))
    requests = [number(r, -DEVICE_REQUEST_MAX, DEVICE_REQUEST_MAX)
                for r in requests_raw]
    return {
        "capacity": capacity,
        "initial": initial,
        "max_charge": max_charge,
        "max_discharge": max_discharge,
        "eta_c": eta_c,
        "eta_d": eta_d,
        "requests": requests,
    }


def _validate_frame(frame, buses, what):
    _exact(frame, ["injections", "links", "sinks"], what)
    injections_raw = frame["injections"]
    links_raw = frame["links"]
    sinks_raw = frame["sinks"]
    if not isinstance(injections_raw, list) or len(injections_raw) > \
            MAX_INJECTIONS:
        raise ProvenanceError(
            "%s.injections must be an array of at most %d items"
            % (what, MAX_INJECTIONS))
    if not isinstance(links_raw, list) or len(links_raw) > MAX_LINKS:
        raise ProvenanceError(
            "%s.links must be an array of at most %d links"
            % (what, MAX_LINKS))
    if not isinstance(sinks_raw, list) or len(sinks_raw) > MAX_SINKS:
        raise ProvenanceError(
            "%s.sinks must be an array of at most %d sinks"
            % (what, MAX_SINKS))

    injections, injection_keys = [], set()
    sources = set()
    for i, item in enumerate(injections_raw):
        item_what = "%s.injections[%d]" % (what, i)
        _exact(item, ["source_id", "bus", "energy"], item_what)
        source_id = _id(item["source_id"], item_what + ".source_id")
        bus = _id(item["bus"], item_what + ".bus")
        if bus not in buses:
            raise ProvenanceError(item_what + " references unknown bus")
        key = (source_id, bus)
        if key in injection_keys:
            raise ProvenanceError(
                item_what + " duplicates (source_id, bus) pair")
        injection_keys.add(key)
        energy = canonical_rational(item["energy"], item_what + ".energy")
        sources.add(source_id)
        injections.append({"source_id": source_id, "bus": bus,
                           "energy": energy})

    links, link_ids = [], set()
    for i, item in enumerate(links_raw):
        item_what = "%s.links[%d]" % (what, i)
        _exact(item, ["link_id", "from_bus", "to_bus", "sent",
                      "efficiency"], item_what)
        link_id = _id(item["link_id"], item_what + ".link_id")
        if link_id in link_ids:
            raise ProvenanceError(item_what + " duplicates link_id")
        link_ids.add(link_id)
        from_bus = _id(item["from_bus"], item_what + ".from_bus")
        to_bus = _id(item["to_bus"], item_what + ".to_bus")
        if from_bus not in buses or to_bus not in buses:
            raise ProvenanceError(item_what + " references unknown bus")
        if from_bus == to_bus:
            raise ProvenanceError(item_what + " is a self-loop link")
        sent = canonical_rational(item["sent"], item_what + ".sent")
        eta = _efficiency_string(item["efficiency"],
                                 item_what + ".efficiency")
        links.append({"link_id": link_id, "from_bus": from_bus,
                      "to_bus": to_bus, "sent": sent, "efficiency": eta})

    sinks, sink_ids = [], set()
    for i, item in enumerate(sinks_raw):
        item_what = "%s.sinks[%d]" % (what, i)
        _exact(item, ["sink_id", "bus", "energy"], item_what)
        sink_id = _id(item["sink_id"], item_what + ".sink_id")
        if sink_id in sink_ids:
            raise ProvenanceError(item_what + " duplicates sink_id")
        sink_ids.add(sink_id)
        bus = _id(item["bus"], item_what + ".bus")
        if bus not in buses:
            raise ProvenanceError(item_what + " references unknown bus")
        energy = canonical_rational(item["energy"], item_what + ".energy")
        sinks.append({"sink_id": sink_id, "bus": bus, "energy": energy})

    return {"injections": injections, "links": links, "sinks": sinks,
            "sources": sources}


def validate_document(data):
    """Validate the full provenance document and return normalized Fractions.

    The result has stable sorted identifier lists and every energy/efficiency
    value converted to an exact :class:`Fraction`.
    """
    try:
        return _validate_document(data)
    except ProvenanceError:
        raise
    except (ValueError, TypeError) as exc:
        raise ProvenanceError(str(exc))


def _validate_document(data):
    _exact(data, ["buses", "devices", "frames"], "provenance payload")

    buses_raw = data["buses"]
    if not isinstance(buses_raw, list) or not (1 <= len(buses_raw) <= MAX_BUSES):
        raise ProvenanceError(
            "buses must be an array of 1..%d distinct identifiers" % MAX_BUSES)
    buses, bus_seen = [], set()
    for i, value in enumerate(buses_raw):
        bus = _id(value, "buses[%d]" % i)
        if bus in bus_seen:
            raise ProvenanceError("duplicate bus id: " + bus)
        bus_seen.add(bus)
        buses.append(bus)

    devices_raw = data["devices"]
    if not isinstance(devices_raw, list) or not (
            1 <= len(devices_raw) <= MAX_DEVICES):
        raise ProvenanceError(
            "devices must be an array of 1..%d items" % MAX_DEVICES)
    devices, device_seen = [], set()
    request_len = None
    all_sources = set()
    for i, item in enumerate(devices_raw):
        what = "devices[%d]" % i
        _exact(item, ["device_id", "bus", "device", "initial_origins"], what)
        device_id = _id(item["device_id"], what + ".device_id")
        if device_id in device_seen:
            raise ProvenanceError("duplicate device_id: " + device_id)
        device_seen.add(device_id)
        bus = _id(item["bus"], what + ".bus")
        if bus not in bus_seen:
            raise ProvenanceError(what + " references unknown bus")
        device = _v1_device(item["device"], what + ".device")
        if request_len is None:
            request_len = len(device["requests"])
        elif len(device["requests"]) != request_len:
            raise ProvenanceError(
                "all devices must share the same requests length")
        origins_raw = item["initial_origins"]
        if not isinstance(origins_raw, dict) or not origins_raw:
            raise ProvenanceError(
                what + ".initial_origins must be a non-empty object")
        origins = {}
        origin_total = Fraction(0)
        for source_id, value in origins_raw.items():
            sid = _id(source_id, what + ".initial_origins key")
            if sid in origins:
                raise ProvenanceError(
                    what + ".initial_origins duplicates source " + sid)
            amount = canonical_rational(
                value, what + ".initial_origins[%s]" % sid)
            origins[sid] = amount
            origin_total += amount
            all_sources.add(sid)
        if origin_total != Fraction(device["initial"]):
            raise ProvenanceError(
                what + ".initial_origins total must equal device.initial")
        devices.append({"device_id": device_id, "bus": bus,
                        "device": device, "initial_origins": origins})

    frames_raw = data["frames"]
    if not isinstance(frames_raw, list) or len(frames_raw) != request_len:
        raise ProvenanceError(
            "frames length must equal the common requests length")
    frames = []
    for t, frame in enumerate(frames_raw):
        normalized = _validate_frame(frame, bus_seen, "frames[%d]" % t)
        all_sources.update(normalized["sources"])
        frames.append(normalized)
    if len(all_sources) > MAX_SOURCES:
        raise ProvenanceError(
            "at most %d distinct source ids are allowed" % MAX_SOURCES)
    return {"buses": buses, "devices": devices, "frames": frames,
            "sources": all_sources}


def validate_frame(frame, buses, known_sources=None):
    """Validate one standalone frame for :meth:`TraceSession.feed`.

    ``buses`` is the set of legal bus ids; ``known_sources`` (when given) is
    the set of source ids already seen, and the combined count may not exceed
    64.  Returns the normalized frame plus its source-id set.
    """
    try:
        normalized = _validate_frame(frame, set(buses), "frame")
        if known_sources is not None and len(
                known_sources | normalized["sources"]) > MAX_SOURCES:
            raise ProvenanceError(
                "at most %d distinct source ids are allowed" % MAX_SOURCES)
        return normalized
    except ProvenanceError:
        raise
    except (ValueError, TypeError) as exc:
        raise ProvenanceError(str(exc))
