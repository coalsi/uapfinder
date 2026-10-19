import {
  Cartesian2,
  Cartesian3,
  Color,
  DistanceDisplayCondition,
  HorizontalOrigin,
  type Label,
  LabelCollection,
  LabelStyle,
  Material,
  type PointPrimitive,
  PointPrimitiveCollection,
  type Polyline,
  PolylineCollection,
  PrimitiveCollection,
  type Scene,
  VerticalOrigin,
} from "cesium";
import type { Track, TrackPoint } from "../../api/types";
import type { Selection } from "../../selection";
import type { TrackState } from "../../store/reducer";
import { CLASS_COLORS, CLASS_LABELS } from "../../theme";
import { formatSpeed } from "../../format";
import type { RenderScheduler } from "../renderScheduler";
import { LABEL_BACKGROUND, LABEL_FONT, LABEL_TEXT, MARKER_DEPTH_TEST_DISTANCE } from "./NodeLayer";

const SMALL_LABEL_FONT = '600 12px system-ui, -apple-system, "Segoe UI", sans-serif';
/**
 * Unselected track labels show only within this camera distance (m), so the
 * network-wide view stays legible (colour + legend identify the class); the
 * selected track's label always shows.
 */
const LABEL_RANGE_M = 6e4;
const ALWAYS = new DistanceDisplayCondition(0, Number.MAX_VALUE);
const NEAR = new DistanceDisplayCondition(0, LABEL_RANGE_M);

interface Entry {
  points: TrackPoint[];
  track: Track;
  selected: boolean;
  path: Polyline;
  drop: Polyline;
  head: PointPrimitive;
  label: Label;
}

export function pointPosition(p: TrackPoint): Cartesian3 {
  return Cartesian3.fromDegrees(p.lon, p.lat, p.alt_m);
}

/**
 * Track polylines coloured by classification. The latest point is a ringed
 * marker with a label and a faint drop line to the ground for height cues.
 */
export class TrackLayer {
  private readonly root = new PrimitiveCollection();
  private readonly paths = new PolylineCollection();
  private readonly heads = new PointPrimitiveCollection();
  private readonly labels = new LabelCollection();
  private readonly entries = new Map<string, Entry>();

  constructor(
    private readonly scene: Scene,
    private readonly scheduler: RenderScheduler,
  ) {
    this.root.add(this.paths);
    this.root.add(this.heads);
    this.root.add(this.labels);
    scene.primitives.add(this.root);
  }

  update(tracks: Record<string, TrackState>, selection: Selection | null, visible: boolean): void {
    let changed = false;
    if (this.root.show !== visible) {
      this.root.show = visible;
      changed = true;
    }
    for (const [id, entry] of this.entries) {
      if (tracks[id]) continue;
      this.paths.remove(entry.path);
      this.paths.remove(entry.drop);
      this.heads.remove(entry.head);
      this.labels.remove(entry.label);
      this.entries.delete(id);
      changed = true;
    }
    for (const [id, { track, points }] of Object.entries(tracks)) {
      const selected = selection?.kind === "track" && selection.id === id;
      const entry = this.entries.get(id);
      if (entry && entry.points === points && entry.track === track && entry.selected === selected) continue;
      changed = true;
      this.entries.set(id, this.draw(id, track, points, selected, entry));
    }
    if (changed) this.scheduler.request();
  }

  private draw(id: string, track: Track, points: TrackPoint[], selected: boolean, entry?: Entry): Entry {
    const color = Color.fromCssColorString(CLASS_COLORS[track.classification]);
    const pick = { kind: "track", id } satisfies Selection;
    const latest = points[points.length - 1] ?? track.last_point;
    const positions = points.map(pointPosition);
    const headPosition = latest ? pointPosition(latest) : Cartesian3.ZERO;

    const path = entry?.path ?? this.paths.add({ id: pick, material: Material.fromType("Color", { color }) });
    path.positions = positions;
    path.show = positions.length >= 2;
    path.width = selected ? 5 : 3;
    path.material.uniforms.color = color.withAlpha(0.95);

    const drop = entry?.drop ?? this.paths.add({ id: pick, material: Material.fromType("Color", { color }) });
    drop.show = latest !== null && latest !== undefined;
    if (latest) drop.positions = [headPosition, Cartesian3.fromDegrees(latest.lon, latest.lat, 0)];
    drop.width = 1;
    drop.material.uniforms.color = color.withAlpha(0.45);

    const head = entry?.head ?? this.heads.add({ id: pick });
    head.show = latest !== null && latest !== undefined;
    head.position = headPosition;
    head.color = color;
    head.pixelSize = selected ? 16 : 12;
    head.outlineColor = Color.WHITE;
    head.outlineWidth = selected ? 3 : 2;
    head.disableDepthTestDistance = MARKER_DEPTH_TEST_DISTANCE;

    const label =
      entry?.label ??
      this.labels.add({
        id: pick,
        position: headPosition,
        font: LABEL_FONT,
        fillColor: LABEL_TEXT,
        showBackground: true,
        backgroundColor: LABEL_BACKGROUND,
        backgroundPadding: new Cartesian2(6, 4),
        style: LabelStyle.FILL,
        // To the right of the head, so labels collide less with node labels (above their markers).
        horizontalOrigin: HorizontalOrigin.LEFT,
        verticalOrigin: VerticalOrigin.CENTER,
        pixelOffset: new Cartesian2(12, 0),
        disableDepthTestDistance: MARKER_DEPTH_TEST_DISTANCE,
      });
    label.show = head.show;
    label.position = headPosition;
    label.font = selected ? LABEL_FONT : SMALL_LABEL_FONT;
    label.distanceDisplayCondition = selected ? ALWAYS : NEAR;
    label.text = selected
      ? `${id} · ${CLASS_LABELS[track.classification]} · ${formatSpeed(track.speed_mps)}`
      : `${CLASS_LABELS[track.classification]} ${id}`;

    return { points, track, selected, path, drop, head, label };
  }

  destroy(): void {
    this.scene.primitives.remove(this.root);
  }
}
