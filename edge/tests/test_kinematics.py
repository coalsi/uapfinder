import math

import numpy as np
import pytest

from helpers import gnomonic_path, path_samples, sample, small_config
from skynet_edge.kinematics import anomaly_score, fit_kinematics, mean_angular_speed

CFG = small_config().anomaly
FPS = 60.0


def noisy(samples, sigma_deg, seed=0):
    rng = np.random.default_rng(seed)
    out = []
    for s in samples:
        de, da = rng.normal(0, sigma_deg, 2)
        out.append(sample((s.t_ns - samples[0].t_ns) / 1e9, s.az + da / math.cos(math.radians(s.el)), s.el + de))
    return out


def turning_path(speed=4.0, turn_deg=90.0, turn_s=0.25, t_turn=0.25):
    """Straight at ``speed`` deg/s, a constant-rate turn, then straight on the new heading."""
    omega = math.radians(turn_deg) / turn_s
    r = speed / omega

    def pos(t):
        px, py = speed * min(t, t_turn), 0.0
        if t > t_turn:
            tau = min(t - t_turn, turn_s)
            px += r * math.sin(omega * tau)
            py += r * (1 - math.cos(omega * tau))
            if t > t_turn + turn_s:
                rest = t - t_turn - turn_s
                px += speed * rest * math.cos(omega * turn_s)
                py += speed * rest * math.sin(omega * turn_s)
        return px, py

    return gnomonic_path(150.0, 50.0, lambda t: pos(t)[0], lambda t: pos(t)[1])


def test_straight_great_circle_motion_scores_zero():
    s = noisy(path_samples(gnomonic_path(150.0, 50.0, lambda t: 5.0 * t, lambda t: 1.0 * t), 0.5, FPS), 0.005)
    k = fit_kinematics(s, CFG.min_samples)
    assert k.speed_deg_s == pytest.approx(math.hypot(5.0, 1.0), rel=0.03)
    assert k.turn_rate_deg_s == 0.0
    assert 0.002 < k.residual_deg < 0.01 and k.accel_sigma_deg_s2 < 1.0  # fit quality reflects the noise
    assert anomaly_score(k, CFG) < 0.05


def test_hard_turn_scores_high():
    s = noisy(path_samples(turning_path(), 0.5, FPS), 0.005)
    k = fit_kinematics(s, CFG.min_samples, CFG.min_speed_for_turn_deg_s)
    assert k.turn_rate_deg_s > 60.0 and k.normal_accel_deg_s2 > 5.0
    assert anomaly_score(k, CFG) > 0.6


def test_hard_stop_is_an_acceleration():
    def x(t):  # 10 deg/s, then a stop within 0.1 s
        return 10.0 * t if t < 0.25 else 2.5 + 10.0 * min(t - 0.25, 0.1) * (1 - min(t - 0.25, 0.1) / 0.2)

    s = noisy(path_samples(gnomonic_path(90.0, 30.0, x, lambda t: 0.0), 0.5, FPS), 0.005)
    k = fit_kinematics(s, CFG.min_samples)
    assert k.accel_deg_s2 > 10.0 and anomaly_score(k, CFG) > 0.5


def test_outlier_does_not_fake_a_manoeuvre():
    s = noisy(path_samples(gnomonic_path(150.0, 50.0, lambda t: 5.0 * t, lambda t: 0.0), 0.5, FPS), 0.005)
    bad = s[15]
    s[15] = sample((bad.t_ns - s[0].t_ns) / 1e9, bad.az, bad.el + 0.5)  # one 0.5 deg glitch
    k = fit_kinematics(s, CFG.min_samples)
    assert anomaly_score(k, CFG) < 0.1


def test_noise_alone_is_not_anomalous():
    s = noisy(path_samples(gnomonic_path(10.0, 70.0, lambda t: 0.3 * t, lambda t: 0.0), 0.5, FPS), 0.02, seed=3)
    assert anomaly_score(fit_kinematics(s, CFG.min_samples, CFG.min_speed_for_turn_deg_s), CFG) < 0.05


def test_too_few_samples():
    s = path_samples(gnomonic_path(10.0, 70.0, lambda t: t, lambda t: 0.0), 0.05, FPS)
    assert fit_kinematics(s[:3], 6) is None
    same_time = [sample(0.0, 10.0, 70.0)] * 8
    assert fit_kinematics(same_time, 6) is None


def test_mean_angular_speed_at_the_sidereal_rate():
    sidereal = 360.0 / 86164.0905  # deg/s, ~0.0042
    s = noisy(path_samples(gnomonic_path(180.0, 0.0, lambda t: sidereal * t, lambda t: 0.0), 3.0, 10.0), 0.002)
    assert mean_angular_speed(s) == pytest.approx(sidereal, abs=0.002)
    assert mean_angular_speed(s[:1]) is None
