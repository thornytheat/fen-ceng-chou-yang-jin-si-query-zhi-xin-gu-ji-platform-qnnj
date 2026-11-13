/** 数据集 store：列表 + 当前档案 + 精度标准维护 + 不可变快照刷新。 */
import { datasetApi, type DatasetPatch, type RefreshResult } from '../api/datasets';
import type { DataSnapshot, Dataset } from '../types/models';
import { createStore } from './base';

export interface DatasetState {
  items: Dataset[];
  detail: Dataset | null;
  snapshots: DataSnapshot[];
  selectedId: number | null;
  loading: boolean;
  saving: boolean;
  refreshing: boolean;
  error: string | null;
}

const store = createStore<DatasetState>({
  items: [],
  detail: null,
  snapshots: [],
  selectedId: null,
  loading: false,
  saving: false,
  refreshing: false,
  error: null,
});

async function load(): Promise<void> {
  store.set({ loading: true, error: null });
  try {
    const result = await datasetApi.list();
    const selectedId = store.getState().selectedId ?? result.items[0]?.id ?? null;
    store.set({ items: result.items, loading: false });
    if (selectedId) await select(selectedId);
  } catch (error) {
    store.set({ loading: false, error: '数据集加载失败' });
    throw error;
  }
}

async function select(id: number): Promise<void> {
  store.set({ selectedId: id });
  try {
    const detail = await datasetApi.detail(id);
    store.set({ detail });
    void loadSnapshots(id).catch(() => undefined);
  } catch (error) {
    store.set({ error: '数据集详情加载失败' });
    throw error;
  }
}

async function loadSnapshots(id: number): Promise<DataSnapshot[]> {
  const result = await datasetApi.snapshots(id);
  if (store.getState().selectedId === id) {
    store.set({ snapshots: result.items });
  }
  return result.items;
}

async function patch(id: number, body: DatasetPatch): Promise<Dataset> {
  store.set({ saving: true });
  try {
    const detail = await datasetApi.patch(id, body);
    store.set((state) => ({
      saving: false,
      detail,
      items: state.items.map((item) => (item.id === id ? detail : item)),
    }));
    return detail;
  } catch (error) {
    store.set({ saving: false });
    throw error;
  }
}

async function refreshSnapshot(id: number): Promise<RefreshResult> {
  store.set({ refreshing: true });
  try {
    const result = await datasetApi.refreshSnapshot(id);
    return result;
  } finally {
    store.set({ refreshing: false });
  }
}

/** 刷新后由页面调用：重新拉取当前档案与快照历史，确保展示最新数据版本。 */
async function reloadAfterRefresh(id: number): Promise<void> {
  await select(id);
  const detail = store.getState().detail;
  if (detail) {
    store.set({ items: store.getState().items.map((item) => (item.id === id ? detail : item)) });
  }
}

export const datasetStore = {
  subscribe: store.subscribe,
  getState: store.getState,
  load,
  select,
  loadSnapshots,
  patch,
  refreshSnapshot,
  reloadAfterRefresh,
};
