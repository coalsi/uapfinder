import {
  Color,
  ColorGeometryInstanceAttribute,
  CoplanarPolygonGeometry,
  GeometryInstance,
  Material,
  PerInstanceColorAppearance,
  PolygonHierarchy,
  Primitive,
  PolylineCollection,
  PrimitiveCollection,
  type Scene,
} from "cesium";
import type { Node } from "../../api/types";
import { FRUSTUM_COLOR, NODE_COLORS } from "../../theme";
import { type Frustum, nodeFrustum } from "../frustum";
import type { RenderScheduler } from "../renderScheduler";

const ONLINE = Color.fromCssColorString(FRUSTUM_COLOR);
const OFFLINE = Color.fromCssColorString(NODE_COLORS.offline);

function geometryKey(nodes: Node[], rangeM: number): string {
  return (
    `${rangeM};` +
    nodes
      .map((n) =>
        [n.node_id, n.lat, n.lon, n.alt_m, n.yaw_deg, n.pitch_deg, n.roll_deg, n.fov_h_deg, n.fov_v_deg, n.online].join(","),
      )
      .sort()
      .join(";")
  );
}

/**
 * Each node's viewing airspace: four translucent side faces from the camera
 * to the far rectangle at the configured range, plus edges. Rebuilt only when
 * a node's position, attitude, FOV or online state changes (not per heartbeat).
 */
export class FrustumLayer {
  private readonly root = new PrimitiveCollection();
  private key = "";

  constructor(
    private readonly scene: Scene,
    private readonly scheduler: RenderScheduler,
  ) {
    scene.primitives.add(this.root);
  }

  update(nodes: Record<string, Node>, rangeM: number, visible: boolean): void {
    if (this.root.show !== visible) {
      this.root.show = visible;
      this.scheduler.request();
    }
    if (!visible) return;
    const list = Object.values(nodes);
    const key = geometryKey(list, rangeM);
    if (key === this.key) return;
    this.key = key;
    this.root.removeAll();

    const faces: GeometryInstance[] = [];
    const edges = new PolylineCollection();
    for (const node of list) {
      const frustum = nodeFrustum(node, rangeM);
      if (!frustum) continue;
      const color = node.online ? ONLINE : OFFLINE;
      faces.push(...faceInstances(frustum, color.withAlpha(node.online ? 0.11 : 0.05)));
      addEdges(edges, frustum, color.withAlpha(node.online ? 0.65 : 0.3));
    }
    if (faces.length > 0) {
      this.root.add(
        new Primitive({
          geometryInstances: faces,
          appearance: new PerInstanceColorAppearance({ flat: true, translucent: true, closed: false }),
          asynchronous: false,
          allowPicking: false,
        }),
      );
    }
    this.root.add(edges);
    this.scheduler.request();
  }

  destroy(): void {
    this.scene.primitives.remove(this.root);
  }
}

function faceInstances({ apex, far }: Frustum, color: Color): GeometryInstance[] {
  const attributes = { color: ColorGeometryInstanceAttribute.fromColor(color) };
  return far.map(
    (corner, i) =>
      new GeometryInstance({
        geometry: new CoplanarPolygonGeometry({
          polygonHierarchy: new PolygonHierarchy([apex, corner, far[(i + 1) % far.length]!]),
          vertexFormat: PerInstanceColorAppearance.FLAT_VERTEX_FORMAT,
        }),
        attributes,
      }),
  );
}

function addEdges(edges: PolylineCollection, { apex, far }: Frustum, color: Color): void {
  const material = Material.fromType("Color", { color });
  for (const corner of far) edges.add({ positions: [apex, corner], width: 1.5, material });
  edges.add({ positions: [...far, far[0]], width: 1.5, material });
}
