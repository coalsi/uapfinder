import {
  Cartesian2,
  Cartesian3,
  Color,
  DistanceDisplayCondition,
  type Label,
  LabelCollection,
  LabelStyle,
  type PointPrimitive,
  PointPrimitiveCollection,
  PrimitiveCollection,
  type Scene,
  VerticalOrigin,
} from "cesium";
import type { Node } from "../../api/types";
import type { Selection } from "../../selection";
import { nodeTitle } from "../../format";
import { NODE_COLORS } from "../../theme";
import type { RenderScheduler } from "../renderScheduler";

/** Markers stay visible over terrain within this camera distance (m), but are hidden behind the far side of the globe. */
export const MARKER_DEPTH_TEST_DISTANCE = 1.5e6;
export const LABEL_FONT = '600 13px system-ui, -apple-system, "Segoe UI", sans-serif';
export const LABEL_BACKGROUND = Color.fromCssColorString("#05080f").withAlpha(0.72);
export const LABEL_TEXT = Color.fromCssColorString("#f0efec");

const ONLINE = Color.fromCssColorString(NODE_COLORS.online);
const OFFLINE = Color.fromCssColorString(NODE_COLORS.offline);

interface Entry {
  key: string;
  point: PointPrimitive;
  label: Label;
}

/** Node markers: green online, grey offline, labelled, pickable. */
export class NodeLayer {
  private readonly root = new PrimitiveCollection();
  private readonly points = new PointPrimitiveCollection();
  private readonly labels = new LabelCollection();
  private readonly entries = new Map<string, Entry>();

  constructor(
    private readonly scene: Scene,
    private readonly scheduler: RenderScheduler,
  ) {
    this.root.add(this.points);
    this.root.add(this.labels);
    scene.primitives.add(this.root);
  }

  update(nodes: Record<string, Node>, selection: Selection | null): void {
    let changed = false;
    for (const [id, entry] of this.entries) {
      if (!nodes[id]) {
        this.points.remove(entry.point);
        this.labels.remove(entry.label);
        this.entries.delete(id);
        changed = true;
      }
    }
    for (const node of Object.values(nodes)) {
      if (node.lat == null || node.lon == null) continue;
      const selected = selection?.kind === "node" && selection.id === node.node_id;
      const key = [node.lat, node.lon, node.alt_m, node.online, node.label, selected].join("|");
      const existing = this.entries.get(node.node_id);
      if (existing?.key === key) continue;
      changed = true;
      const position = Cartesian3.fromDegrees(node.lon, node.lat, node.alt_m ?? 0);
      const pick = { kind: "node", id: node.node_id } satisfies Selection;
      const point = existing?.point ?? this.points.add({ id: pick });
      point.position = position;
      point.color = node.online ? ONLINE : OFFLINE;
      point.pixelSize = selected ? 15 : 11;
      point.outlineColor = selected ? Color.WHITE : Color.fromCssColorString("#05080f");
      point.outlineWidth = selected ? 3 : 2;
      point.disableDepthTestDistance = MARKER_DEPTH_TEST_DISTANCE;
      const label =
        existing?.label ??
        this.labels.add({
          id: pick,
          position,
          font: LABEL_FONT,
          fillColor: LABEL_TEXT,
          showBackground: true,
          backgroundColor: LABEL_BACKGROUND,
          backgroundPadding: new Cartesian2(6, 4),
          style: LabelStyle.FILL,
          verticalOrigin: VerticalOrigin.BOTTOM,
          pixelOffset: new Cartesian2(0, -12),
          distanceDisplayCondition: new DistanceDisplayCondition(0, 1.2e6),
          disableDepthTestDistance: MARKER_DEPTH_TEST_DISTANCE,
        });
      label.position = position;
      label.text = node.online ? nodeTitle(node) : `${nodeTitle(node)} · offline`;
      this.entries.set(node.node_id, { key, point, label });
    }
    if (changed) this.scheduler.request();
  }

  destroy(): void {
    this.scene.primitives.remove(this.root);
  }
}
