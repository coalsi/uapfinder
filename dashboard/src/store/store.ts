import { createContext, useContext, useSyncExternalStore } from "react";
import { type Action, type State, initialState, reducer } from "./reducer";

export interface Store {
  getState(): State;
  dispatch(action: Action): void;
  subscribe(listener: () => void): () => void;
}

export function createStore(state: State = initialState): Store {
  let current = state;
  const listeners = new Set<() => void>();
  return {
    getState: () => current,
    dispatch(action) {
      const next = reducer(current, action);
      if (next === current) return;
      current = next;
      for (const listener of listeners) listener();
    },
    subscribe(listener) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
  };
}

export const StoreContext = createContext<Store | null>(null);

export function useStoreApi(): Store {
  const store = useContext(StoreContext);
  if (!store) throw new Error("StoreContext is missing");
  return store;
}

/** Subscribe to a slice of the store; `selector` must return stable references for unchanged data. */
export function useStore<T>(selector: (state: State) => T): T {
  const store = useStoreApi();
  return useSyncExternalStore(store.subscribe, () => selector(store.getState()));
}
