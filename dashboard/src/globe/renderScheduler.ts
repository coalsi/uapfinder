import type { Scene } from "cesium";

/**
 * The viewer runs in request-render mode. Layers call `request()` after
 * changing the scene, and `keepRendering(fn)` for things that need frames
 * until done (async primitives becoming ready, fading animations).
 */
export class RenderScheduler {
  private readonly active = new Set<() => boolean>();
  private readonly removeListener: () => void;

  constructor(private readonly scene: Scene) {
    this.removeListener = scene.postUpdate.addEventListener(() => {
      for (const stillActive of this.active) {
        if (!stillActive()) this.active.delete(stillActive);
      }
      if (this.active.size > 0) scene.requestRender();
    });
  }

  request(): void {
    this.scene.requestRender();
  }

  /** Request frames for as long as `stillActive` returns true. */
  keepRendering(stillActive: () => boolean): void {
    this.active.add(stillActive);
    this.scene.requestRender();
  }

  destroy(): void {
    this.removeListener();
    this.active.clear();
  }
}
