import { Cartesian3, Color, Material, type Polyline, PolylineCollection, type Scene } from "cesium";
import type { RayFlash, TrackState } from "../../store/reducer";
import { CLASS_COLORS, RAY_COLOR } from "../../theme";
import type { RenderScheduler } from "../renderScheduler";

export const FLASH_MS = 1200;
const PEAK_ALPHA = 0.8;
const WHITE = Color.fromCssColorString(RAY_COLOR);
/** Rays are tinted towards their track's classification colour, so each fan reads as part of its track. */
const TINTS = Object.fromEntries(
  Object.entries(CLASS_COLORS).map(([c, hex]) => [c, Color.lerp(WHITE, Color.fromCssColorString(hex), 0.45, new Color())]),
) as Record<keyof typeof CLASS_COLORS, Color>;

interface Active {
  trackId: string;
  start: number;
  color: Color;
  lines: Polyline[];
}

/**
 * Briefly draws the contributing nodes' lines of sight to each new
 * triangulated point, fading out. Only the newest flash of each track is
 * shown, so fast-updating tracks do not turn into a fan of rays.
 */
export class FlashLayer {
  private readonly lines = new PolylineCollection();
  private readonly drawn = new Set<string>();
  private active: Active[] = [];
  private readonly tick = () => this.animate();

  /** `now` must use the same clock as `RayFlash.receivedAt` (Date.now). */
  constructor(
    private readonly scene: Scene,
    private readonly scheduler: RenderScheduler,
    private readonly now: () => number = Date.now,
  ) {
    scene.primitives.add(this.lines);
  }

  update(flashes: RayFlash[], tracks: Record<string, TrackState>, visible: boolean): void {
    if (this.lines.show !== visible) {
      this.lines.show = visible;
      this.scheduler.request();
    }
    const now = this.now();
    let added = false;
    for (const flash of flashes) {
      if (this.drawn.has(flash.key)) continue;
      this.drawn.add(flash.key);
      if (!visible || now - flash.receivedAt > FLASH_MS) continue;
      const t = flash.triangulation;
      this.retire(t.track_id);
      const classification = tracks[t.track_id]?.track.classification;
      const color = classification ? TINTS[classification] : WHITE;
      const target = Cartesian3.fromDegrees(t.lon, t.lat, t.alt_m);
      const lines = t.rays.map((ray) =>
        this.lines.add({
          positions: [Cartesian3.fromDegrees(ray.node_lon, ray.node_lat, ray.node_alt_m), target],
          width: 3.5,
          material: Material.fromType("PolylineGlow", {
            color: color.withAlpha(PEAK_ALPHA),
            glowPower: 0.2,
            taperPower: 1,
          }),
        }),
      );
      this.active.push({ trackId: t.track_id, start: flash.receivedAt, color, lines });
      added = true;
    }
    // Forget keys that can no longer appear in the store's bounded flash list.
    if (this.drawn.size > 4 * flashes.length + 64) {
      const keep = new Set(flashes.map((f) => f.key));
      for (const key of this.drawn) if (!keep.has(key)) this.drawn.delete(key);
    }
    if (added) this.scheduler.keepRendering(this.tick);
  }

  /** Remove the flash currently shown for `trackId`, if any. */
  private retire(trackId: string): void {
    this.active = this.active.filter((a) => {
      if (a.trackId !== trackId) return true;
      for (const line of a.lines) this.lines.remove(line);
      return false;
    });
  }

  /** Fade active flashes; returns true while any remain. */
  private animate(): boolean {
    const now = this.now();
    this.active = this.active.filter(({ start, color, lines }) => {
      const age = (now - start) / FLASH_MS;
      if (age >= 1) {
        for (const line of lines) this.lines.remove(line);
        return false;
      }
      for (const line of lines) line.material.uniforms.color = color.withAlpha(PEAK_ALPHA * (1 - age));
      return true;
    });
    return this.active.length > 0;
  }

  destroy(): void {
    this.scene.primitives.remove(this.lines);
  }
}
