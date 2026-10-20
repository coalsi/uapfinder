"""Automatic pointing calibration from the stars.

    from skynet_edge.starcal import Observer, Prior, StarCalResult, calibrate, is_dark_enough
    result = calibrate(image, t_ns, Observer(lat, lon, alt_m), Prior(1920, 1080, 90.0, pitch_deg=45, roll_deg=0,
                                                                         sigma_deg=2))

See docs/NODE_APPLIANCE.md ("Pointing calibration from the stars") and the
module docstrings: astro (catalog -> az/el), detect (stars in a stacked
frame), solve (guided search + robust fit), synth (synthetic test skies).
"""

from .solve import Observer, Prior, SolveParams, StarCalResult, calibrate, is_dark_enough, sun_altitude_deg

__all__ = ["Observer", "Prior", "SolveParams", "StarCalResult", "calibrate", "is_dark_enough", "sun_altitude_deg"]
