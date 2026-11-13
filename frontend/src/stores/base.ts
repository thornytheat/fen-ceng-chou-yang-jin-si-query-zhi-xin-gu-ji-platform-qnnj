export type Listener = () => void;

/** 只读视图：useSyncExternalStore 只需要这两个方法（包装后的领域 store 没有 set）。 */
export interface ReadonlyStore<T> {
  subscribe: (listener: Listener) => () => void;
  getState: () => T;
}

export interface Store<T> extends ReadonlyStore<T> {
  set: (patch: Partial<T> | ((state: T) => Partial<T>)) => void;
}

export function createStore<T extends object>(initial: T): Store<T> {
  let state = initial;
  const listeners = new Set<Listener>();
  return {
    subscribe(listener) {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },
    getState() {
      return state;
    },
    set(patch) {
      const next = typeof patch === 'function' ? (patch as (value: T) => Partial<T>)(state) : patch;
      state = { ...state, ...next };
      listeners.forEach((listener) => listener());
    },
  };
}
