import type { Classification, CoverageStatus } from "./api/types";

// One source of truth for colours shared by the globe (CesiumJS) and the UI
// legends. Track classes: neutral greys for the routine classes and three
// hues validated as colour-blind-distinguishable on the dark globe; every use
// is paired with a text label. Coverage uses the reserved status palette.

export const CLASS_COLORS: Record<Classification, string> = {
  unknown: "#c3c2b7",
  ballistic: "#3987e5",
  hypersonic: "#d95926",
  high_g: "#199e70",
  stationary: "#8d94a3",
};

export const CLASS_LABELS: Record<Classification, string> = {
  unknown: "Unknown",
  ballistic: "Ballistic",
  hypersonic: "Hypersonic",
  high_g: "High-g",
  stationary: "Stationary",
};

export const COVERAGE_COLORS: Record<CoverageStatus, string> = {
  blind: "#d03b3b",
  mono: "#fab219",
  stereo: "#0ca30c",
};

export const COVERAGE_LABELS: Record<CoverageStatus, string> = {
  blind: "Blind (no node)",
  mono: "Mono (one view)",
  stereo: "Stereo (triangulates)",
};

export const NODE_COLORS = { online: "#0ca30c", offline: "#898781" } as const;
export const FRUSTUM_COLOR = "#9ec5f4";
export const SUGGESTION_COLOR = "#9085e9";
export const RAY_COLOR = "#f0efec";
