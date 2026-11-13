import { useSyncExternalStore } from 'react';
import type { ReadonlyStore } from '../stores/base';

/** 把外部 store 接进 React（subscribe/getState 都是稳定的函数引用）。 */
export function useStore<T extends object>(store: ReadonlyStore<T>): T {
  return useSyncExternalStore(store.subscribe, store.getState, store.getState);
}
