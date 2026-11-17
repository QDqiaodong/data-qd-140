from fractions import Fraction

from .physics import step_request


def simulate(capacity, initial, max_charge, max_discharge, eta_c, eta_d,
             requests):
    """Deterministic v1 saturated replay.

    Every slot is advanced by the shared
    :func:`storage_dispatch.physics.step_request` so the v1 replay and the
    provenance trace session use exactly the same direction decision, grid-side
    limits, efficiency conversions and saturation policy.  The output shape is
    the original v1 behaviour and must not change.
    """
    soc = Fraction(initial)
    total_in = total_out = losses = unmet = Fraction()
    steps = []
    for i, r in enumerate(requests):
        before = soc
        outcome = step_request(soc, capacity, eta_c, eta_d, max_charge,
                               max_discharge, r)
        grid_in = outcome['grid_in']
        grid_out = outcome['grid_out']
        loss = outcome['loss']
        missing = outcome['unmet']
        soc = outcome['after']
        total_in += grid_in
        total_out += grid_out
        losses += loss
        unmet += missing
        steps.append({'slot': i, 'request': r, 'before': str(before),
                      'after': str(soc), 'grid_in': str(grid_in),
                      'grid_out': str(grid_out), 'loss': str(loss),
                      'unmet': str(missing)})
    return {'steps': steps, 'final': str(soc), 'grid_in': str(total_in),
            'grid_out': str(total_out), 'loss': str(losses),
            'unmet': str(unmet),
            'conservation_residual': str(
                Fraction(initial) + total_in - total_out - losses - soc)}
