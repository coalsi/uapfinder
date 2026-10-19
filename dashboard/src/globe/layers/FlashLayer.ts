import { Cartesian3, Color, Material, type Polyline, PolylineCollection, type Scene } from "cesium";
import type { RayFlash } from "../../store/reducer";
import { RAY_COLOR } from "../../theme";
import type { RenderScheduler } from "../renderScheduler";

export const FLASH_MS = 1600;
const BASE = Color.fromCssColorString(RAY_COLOR);

interface Active {
  start: number;
  lines: Polyline[];
}

/** Briefly draws the contributing nodes' lines of sight to each new triangulated point, fading out. */
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

  update(flashes: RayFlash[], visible: boolean): void {
    this.lines.show = visible;
    const now = this.now();
    let added = false;
    for (const flash of flashes) {
      if (this.drawn.has(flash.key)) continue;
      this.drawn.add(flash.key);
      if (!visible || now - flash.receivedAt > FLASH_MS) continue;
      const t = flash.triangulation;
      const target = Cartesian3.fromDegrees(t.lon, t.lat, t.alt_m);
      const lines = t.rays.map((ray) =>
        this.lines.add({
          positions: [Cartesian3.fromDegrees(ray.node_lon, ray.node_lat, ray.node_alt_m), target],
          width: 6,
          material: Material.fromType("PolylineGlow", { color: BASE.withAlpha(0.9), glowPower: 0.2, taperPower: 1 }),
        }),
      );
      this.active.push({ start: flash.receivedAt, lines });
      added = true;
    }
    // Forget keys that can no longer appear in the store's bounded flash list.
    if (this.drawn.size > 4 * flashes.length + 64) {
      const keep = new Set(flashes.map((f) => f.key));
      for (const key of this.drawn) if (!keep.has(key)) this.drawn.delete(key);
    }
    if (added) this.scheduler.keepRendering(this.tick);
  }

  /** Fade active flashes; returns true while any remain. */
  private animate(): boolean {
    const now = this.now();
    this.active = this.active.filter(({ start, lines }) => {
      const age = (now - start) / FLASH_MS;
      if (age >= 1) {
        for (const line of lines) this.lines.remove(line);
        return false;
      }
      for (const line of lines) line.material.uniforms.color = BASE.withAlpha(0.9 * (1 - age));
      return true;
    });
    return this.active.length > 0;
  }

  destroy(): void {
    this.scene.primitives.remove(this.lines);
  }
}
