import {
  Cartesian2,
  Cartesian3,
  Color,
  ColorGeometryInstanceAttribute,
  DistanceDisplayCondition,
  GeometryInstance,
  LabelCollection,
  LabelStyle,
  PerInstanceColorAppearance,
  PointPrimitiveCollection,
  PolygonGeometry,
  PolygonHierarchy,
  Primitive,
  PrimitiveCollection,
  type Scene,
  VerticalOrigin,
} from "cesium";
import type { Coverage, CoverageStatus } from "../../api/types";
import type { Selection } from "../../selection";
import { COVERAGE_COLORS, SUGGESTION_COLOR } from "../../theme";
import type { RenderScheduler } from "../renderScheduler";
import { LABEL_BACKGROUND, LABEL_FONT, LABEL_TEXT, MARKER_DEPTH_TEST_DISTANCE } from "./NodeLayer";

const ALPHA: Record<CoverageStatus, number> = { blind: 0.38, mono: 0.42, stereo: 0.46 };
const CELL_COLORS = Object.fromEntries(
  Object.entries(COVERAGE_COLORS).map(([status, hex]) => [
    status,
    ColorGeometryInstanceAttribute.fromColor(Color.fromCssColorString(hex).withAlpha(ALPHA[status as CoverageStatus])),
  ]),
) as Record<CoverageStatus, ColorGeometryInstanceAttribute>;
/** Cells are shrunk towards their centre so the grid reads as cells, not one blob. */
const CELL_INSET = 0.92;
const SUGGESTION = Color.fromCssColorString(SUGGESTION_COLOR);

/**
 * Coverage grid drawn as a translucent sheet at the evaluated altitude slice
 * (`ref_alt_m`): red blind, amber mono, green stereo. Plus "deploy a node
 * here" markers for the server's suggestions.
 */
export class CoverageLayer {
  private readonly root = new PrimitiveCollection();
  private readonly cells = new PrimitiveCollection();
  private readonly markers = new PointPrimitiveCollection();
  private readonly labels = new LabelCollection();
  private coverage: Coverage | null = null;
  private latest: Primitive | null = null;

  constructor(
    private readonly scene: Scene,
    private readonly scheduler: RenderScheduler,
  ) {
    this.root.add(this.cells);
    this.root.add(this.markers);
    this.root.add(this.labels);
    scene.primitives.add(this.root);
  }

  /** `fallbackHeightM` is used when the response does not echo `params.ref_alt_m`. */
  update(coverage: Coverage | null, visible: boolean, fallbackHeightM: number): void {
    if (this.root.show !== visible) {
      this.root.show = visible;
      this.scheduler.request();
    }
    if (coverage === this.coverage) return;
    this.coverage = coverage;
    this.drawSuggestions(coverage);
    if (!coverage || coverage.features.length === 0) {
      this.latest = null;
      this.cells.removeAll();
      this.scheduler.request();
      return;
    }

    const height = typeof coverage.params?.ref_alt_m === "number" ? coverage.params.ref_alt_m : fallbackHeightM;
    const instances: GeometryInstance[] = [];
    for (const feature of coverage.features) {
      const ring = feature.geometry.coordinates[0];
      if (!ring || ring.length < 4) continue;
      instances.push(
        new GeometryInstance({
          geometry: new PolygonGeometry({
            polygonHierarchy: new PolygonHierarchy(insetRing(ring)),
            height,
            vertexFormat: PerInstanceColorAppearance.FLAT_VERTEX_FORMAT,
          }),
          attributes: { color: CELL_COLORS[feature.properties.status] },
        }),
      );
    }
    const next = new Primitive({
      geometryInstances: instances,
      appearance: new PerInstanceColorAppearance({ flat: true, translucent: true, closed: false }),
      allowPicking: false,
      asynchronous: true,
    });
    this.cells.add(next);
    this.latest = next;
    // Keep the previous grid until the new one (built in a worker) is ready, to avoid flicker.
    this.scheduler.keepRendering(() => {
      if (next.isDestroyed()) return false;
      if (next !== this.latest) {
        this.cells.remove(next);
        return false;
      }
      if (!next.ready) return true;
      for (let i = this.cells.length - 1; i >= 0; i--) {
        const p = this.cells.get(i) as Primitive;
        if (p !== next) this.cells.remove(p);
      }
      return false;
    });
  }

  private drawSuggestions(coverage: Coverage | null): void {
    this.markers.removeAll();
    this.labels.removeAll();
    for (const s of coverage?.suggestions ?? []) {
      const position = Cartesian3.fromDegrees(s.lon, s.lat, 0);
      const id = { kind: "suggestion", lat: s.lat, lon: s.lon, gain: s.gain_cells } satisfies Selection;
      this.markers.add({
        id,
        position,
        pixelSize: 14,
        color: SUGGESTION,
        outlineColor: Color.WHITE,
        outlineWidth: 2,
        disableDepthTestDistance: MARKER_DEPTH_TEST_DISTANCE,
      });
      this.labels.add({
        id,
        position,
        text: `Deploy here · +${s.gain_cells}`,
        font: LABEL_FONT,
        fillColor: LABEL_TEXT,
        showBackground: true,
        backgroundColor: LABEL_BACKGROUND,
        backgroundPadding: new Cartesian2(6, 4),
        style: LabelStyle.FILL,
        verticalOrigin: VerticalOrigin.BOTTOM,
        pixelOffset: new Cartesian2(0, -12),
        distanceDisplayCondition: new DistanceDisplayCondition(0, 6e5),
        disableDepthTestDistance: MARKER_DEPTH_TEST_DISTANCE,
      });
    }
    this.scheduler.request();
  }

  destroy(): void {
    this.scene.primitives.remove(this.root);
  }
}

function insetRing(ring: [number, number][]): Cartesian3[] {
  const open = ring.slice(0, -1);
  const cx = open.reduce((s, p) => s + p[0], 0) / open.length;
  const cy = open.reduce((s, p) => s + p[1], 0) / open.length;
  return open.map(([lon, lat]) => Cartesian3.fromDegrees(cx + (lon - cx) * CELL_INSET, cy + (lat - cy) * CELL_INSET));
}
