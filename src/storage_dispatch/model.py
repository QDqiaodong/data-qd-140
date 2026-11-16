from fractions import Fraction
from .common import require, number, rows
from .dispatch import simulate
from .physics import efficiency


def ratio(value):
    require(value, 'numerator denominator')
    n = number(value['numerator'], 1, 1000000)
    d = number(value['denominator'], 1, 1000000)
    return efficiency(n, d)


def analyze(data):
    require(data, 'capacity initial max_charge max_discharge charge_efficiency discharge_efficiency requests')
    capacity = number(data['capacity'], 1, 10**9)
    initial = number(data['initial'], 0, capacity)
    charge = number(data['max_charge'], 0, 10**9)
    discharge = number(data['max_discharge'], 0, 10**9)
    requests = [number(r, -10**9, 10**9) for r in rows(data['requests'], 20000)]
    return simulate(capacity, initial, charge, discharge,
                    ratio(data['charge_efficiency']),
                    ratio(data['discharge_efficiency']), requests)
