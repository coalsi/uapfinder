import {
  BoundingSphere,
  Cartesian3,
  Math as CesiumMath,
  HeadingPitchRange,
  ScreenSpaceEventHandler,
  ScreenSpaceEventType,
  type Viewer,
} from "cesium";
import type { Coverage } from "../api/types";
import type { AppConfig } from "../config";
import type { GeoRect } from "../geo/coverageQuery";
import type { LayerVisibility, Selection } from "../selection";
import type { State } from "../store/reducer";
import { createViewer } from "./createViewer";
import { CoverageLayer } from "./layers/CoverageLayer";
import { FlashLayer } from "./layers/FlashLayer";
import { FrustumLayer } from "./layers/FrustumLayer";
import { NodeLayer } from "./layers/NodeLayer";
import { TrackLayer, pointPosition } from "./layers/TrackLayer";
import { RenderScheduler } from "./renderScheduler";

export interface GlobeInput {
  state: State;
  coverage: Coverage | null;
  layers: LayerVisibility;
  frustumRangeKm: number;
  selection: Selection | null;
}

export interface GlobeCallbacks {
  onSelect(selection: Selection | null): void;
  onViewChange(view: GeoRect | null): void;
}

const isSelection = (value: unknown): value is Selection =>
  typeof value === "object" && value !== null && "kind" in value;

/** Owns the CesiumJS viewer and its layers; React feeds it state through `update`. */
export class GlobeController {
  readonly viewer: Viewer;
  private readonly scheduler: RenderScheduler;
  private readonly nodes: NodeLayer;
  private readonly frustums: FrustumLayer;
  private readonly tracks: TrackLayer;
  private readonly flashes: FlashLayer;
  private readonly coverage: CoverageLayer;
  private readonly handler: ScreenSpaceEventHandler;
  private readonly removeMoveEnd: () => void;
  private state: State | null = null;
  private fitted = false;

  constructor(container: HTMLElement, config: AppConfig, callbacks: GlobeCallbacks) {
    this.viewer = createViewer(container, config);
    const { scene } = this.viewer;
    this.scheduler = new RenderScheduler(scene);
    this.coverage = new CoverageLayer(scene, this.scheduler);
    this.frustums = new FrustumLayer(scene, this.scheduler);
    this.tracks = new TrackLayer(scene, this.scheduler);
    this.flashes = new FlashLayer(scene, this.scheduler);
    this.nodes = new NodeLayer(scene, this.scheduler);

    this.handler = new ScreenSpaceEventHandler(scene.canvas);
    this.handler.setInputAction((event: ScreenSpaceEventHandler.PositionedEvent) => {
      const picked: unknown = scene.pick(event.position);
      const id = picked && typeof picked === "object" && "id" in picked ? picked.id : undefined;
      callbacks.onSelect(isSelection(id) ? id : null);
    }, ScreenSpaceEventType.LEFT_CLICK);

    this.removeMoveEnd = scene.camera.moveEnd.addEventListener(() => callbacks.onViewChange(this.viewRect()));
    this.viewer.homeButton.viewModel.command.beforeExecute.addEventListener((e: { cancel: boolean }) => {
      e.cancel = true;
      this.fitNetwork(1.5);
    });
  }

  update(input: GlobeInput): void {
    const { state, layers, selection } = input;
    this.state = state;
    this.coverage.update(input.coverage, layers.coverage);
    this.frustums.update(state.nodes, input.frustumRangeKm * 1000, layers.frustums);
    this.tracks.update(state.tracks, selection, layers.tracks);
    this.flashes.update(state.flashes, layers.tracks);
    this.nodes.update(state.nodes, selection);
    if (!this.fitted && state.loaded && this.fitNetwork(0)) this.fitted = true;
  }

  /** Visible area on the ellipsoid in degrees, or null when looking past the globe. */
  viewRect(): GeoRect | null {
    const r = this.viewer.camera.computeViewRectangle();
    if (!r) return null;
    const d = CesiumMath.toDegrees;
    return { west: d(r.west), south: d(r.south), east: d(r.east), north: d(r.north) };
  }

  /** Frame all positioned nodes; false when none are known yet. */
  fitNetwork(duration: number): boolean {
    const positions = Object.values(this.state?.nodes ?? {})
      .filter((n) => n.lat != null && n.lon != null)
      .map((n) => Cartesian3.fromDegrees(n.lon as number, n.lat as number, n.alt_m ?? 0));
    if (positions.length === 0) return false;
    const sphere = BoundingSphere.fromPoints(positions);
    this.flyToSphere(sphere, Math.max(sphere.radius * 5, 120_000), -50, duration);
    return true;
  }

  flyTo(target: Selection): void {
    const state = this.state;
    if (target.kind === "suggestion") {
      this.flyToSphere(new BoundingSphere(Cartesian3.fromDegrees(target.lon, target.lat, 0), 1), 90_000, -55);
      return;
    }
    if (target.kind === "node") {
      const node = state?.nodes[target.id];
      if (node?.lat == null || node.lon == null) return;
      this.flyToSphere(new BoundingSphere(Cartesian3.fromDegrees(node.lon, node.lat, node.alt_m ?? 0), 1), 70_000, -40);
      return;
    }
    const track = state?.tracks[target.id];
    if (!track) return;
    const points = track.points.length > 0 ? track.points : track.track.last_point ? [track.track.last_point] : [];
    if (points.length === 0) return;
    const sphere = BoundingSphere.fromPoints(points.map(pointPosition));
    this.flyToSphere(sphere, Math.max(sphere.radius * 3.2, 40_000), -30);
  }

  private flyToSphere(sphere: BoundingSphere, range: number, pitchDeg: number, duration = 1.5): void {
    this.viewer.camera.flyToBoundingSphere(sphere, {
      offset: new HeadingPitchRange(0, CesiumMath.toRadians(pitchDeg), range),
      duration,
    });
    this.scheduler.request();
  }

  destroy(): void {
    this.removeMoveEnd();
    this.handler.destroy();
    this.coverage.destroy();
    this.frustums.destroy();
    this.tracks.destroy();
    this.flashes.destroy();
    this.nodes.destroy();
    this.scheduler.destroy();
    this.viewer.destroy();
  }
}
