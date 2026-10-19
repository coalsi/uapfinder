/** What the user has selected on the globe or in a panel. */
export type Selection =
  | { kind: "node"; id: string }
  | { kind: "track"; id: string }
  | { kind: "suggestion"; lat: number; lon: number; gain: number };

export interface LayerVisibility {
  frustums: boolean;
  tracks: boolean;
  coverage: boolean;
}
