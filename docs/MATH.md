# Geometry, triangulation and error model

Reference code: [`common/skynet_common/`](../common/skynet_common/). Every
formula here has a test in `common/tests/`, and the WGS84 conversions are
cross-checked against PROJ (`pyproj`).

## Frames and conventions

| Quantity | Convention |
|----------|------------|
| Geodetic position | WGS84 latitude/longitude (deg), height above the **ellipsoid** (m). GPS receivers report MSL height in NMEA GGA; add the geoid separation field to get ellipsoidal height. CesiumJS also uses ellipsoidal heights. |
| ECEF | WGS84 Earth-Centred Earth-Fixed, metres. |
| ENU | Local East-North-Up at the observer; *Up* is the ellipsoid normal. |
| Azimuth | Degrees clockwise from **true** north, [0, 360). |
| Elevation | Degrees above the local geodetic horizon. |
| Camera frame | OpenCV: +x right, +y down, +z forward (optical axis). |
| Attitude | `yaw` = azimuth of the optical axis, `pitch` = its elevation, `roll` = rotation about it, positive = right side of the image down. With zero roll the image x axis is horizontal. |

### Geodetic ↔ ECEF

With `a = 6378137 m`, `f = 1/298.257223563`, `e² = f(2 − f)` and the prime
vertical radius `N(φ) = a / √(1 − e² sin²φ)`:

```
X = (N + h) cos φ cos λ
Y = (N + h) cos φ sin λ
Z = (N (1 − e²) + h) sin φ
```

The inverse uses Bowring's parametric-latitude estimate with two refinement
passes; round-trip error is below 1e-9° and 0.1 mm for heights from −1 km
to 400 km.

### Line of sight

The ENU unit vector for azimuth `A` and elevation `E` is
`(cos E sin A, cos E cos A, sin E)`, rotated into ECEF with the ENU basis at
the observer:

```
east  = (−sin λ,        cos λ,        0    )
north = (−sin φ cos λ,  −sin φ sin λ,  cos φ)
up    = ( cos φ cos λ,   cos φ sin λ,  sin φ)
```

On the node, a detection at pixel `(u, v)` is first undistorted
(`cv2.undistortPoints`), turned into the camera ray
`((u − cx)/fx, (v − cy)/fy, 1)`, and rotated into ENU with the camera axes:

```
forward = (cos p sin y, cos p cos y, sin p)
right₀  = (cos y, −sin y, 0)
down₀   = forward × right₀
right   =  cos r · right₀ + sin r · down₀
down    = −sin r · right₀ + cos r · down₀
ray_ENU = x · right + y · down + z · forward
```

## Two-ray triangulation (blueprint §4)

Rays `L₁ = p₁ + t₁v₁` and `L₂ = p₂ + t₂v₂` with unit `vᵢ`. With
`c = v₁ × v₂` and `d = p₂ − p₁`, the parameters of the closest points are

```
t₁ = det[d, v₂, c] / |c|²        t₂ = det[d, v₁, c] / |c|²
```

The estimate is the midpoint of the closest-approach segment and the
blueprint's `error_margin_m` is that segment's length. Our implementation
adds three checks the blueprint's version lacks:

1. `|c|² < 1e-8` → parallel, no solution (same as the blueprint).
2. `t₁ ≤ 0` or `t₂ ≤ 0` → the closest approach is *behind* a camera. The
   inputs are rays, not infinite lines, so this pair is not the same object.
3. The result is converted to latitude/longitude/ellipsoidal altitude, and
   the ranges `t₁`, `t₂` and the parallax angle `∠(v₁, v₂)` are returned
   because the gate and the error model need them.

### Gating (is it the same object?)

A pair of detections is accepted when the parallax is at least 1°, both
ranges and the altitude are physically plausible, and

```
miss ≤ k · √((r₁σ)² + (r₂σ)²) + floor
```

where `σ` is the per-axis angular noise (default 0.05°), `rᵢ` the ranges,
`k = 3` and `floor = 30 m` for timing and GPS error. The ratio
`miss / √((r₁σ)² + (r₂σ)²)` ranks competing candidates.

## N-ray least squares

With three or more nodes the server minimises
`Σ wᵢ |(I − vᵢvᵢᵀ)(x − pᵢ)|²`. Setting the gradient to zero gives the 3×3
system

```
(Σ wᵢ (I − vᵢvᵢᵀ)) x = Σ wᵢ (I − vᵢvᵢᵀ) pᵢ
```

with weights `wᵢ = 1/(rᵢσ)²` iterated from the previous solution, so distant
observers, whose lateral error is larger, count for less.

## Error model

Each ray constrains the position in the two directions perpendicular to it
with standard deviation `rᵢσ`. The linearised covariance of the
intersection is

```
Σₓ = ( Σᵢ (I − vᵢvᵢᵀ) / (rᵢσ)² )⁻¹
```

and the API reports `sigma_m = √trace(Σₓ)`, the 3D RMS error. A Monte-Carlo
test confirms the observed error matches this prediction.

### Error budget

Two nodes due east-west, target at 10 km altitude midway between them
(computed with `position_covariance`):

| Baseline | Range | Parallax | Elevation | σ at 0.01° | σ at 0.05° | σ at 1° |
|---------:|------:|---------:|----------:|-----------:|-----------:|--------:|
| 8 km     | 10.4 km | 45°  | 68° | 4 m  | 19 m  | 386 m |
| 16 km    | 12.5 km | 79°  | 50° | 4 m  | 18 m  | 351 m |
| 32 km    | 18.7 km | 118° | 31° | 6 m  | 29 m  | 570 m |
| 64 km    | 33.4 km | 146° | 17° | 15 m | 77 m  | 1.5 km |
| 128 km (80 mi) | 64.7 km | 162° | 8° | 53 m | 266 m | 5.3 km |

Takeaways:

* **Pointing calibration matters most.** A 1080p sensor with a 90° lens has
  about 0.047°/pixel, so centroiding gives 0.01–0.05°. A consumer IMU
  gives about 1°, which is 20–100× worse. Fixed cameras should be
  calibrated against the star field (plate solving) and use the IMU only to
  detect that a mount has moved.
* **The 5–40 mile spacing in the blueprint is the sweet spot.** Beyond
  about 40 miles the target sits low on both horizons, where refraction,
  haze and obstructions dominate even before the angular error grows.
* **Timing.** A Mach 6 object moves about 100 m in the 50 ms match window,
  so the server interpolates each neighbour's line of sight to the exact
  packet time when it has samples of the same track on both sides. Nodes
  need GPS/PPS-disciplined clocks (chrony); NTP over a home connection is
  only good to a few milliseconds.
* **Not modelled yet:** atmospheric refraction (about 0.5° at the horizon,
  under 0.02° above 45° elevation), lens distortion residuals, and the
  uncertainty of the node's own GPS position (about 2–5 m without RTK,
  negligible next to angular error at these ranges).
