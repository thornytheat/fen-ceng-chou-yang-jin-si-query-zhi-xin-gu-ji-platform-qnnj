import { http } from './client';
import type { DataSnapshot, Dataset, ListEnvelope, Stratum } from '../types/models';

export interface DatasetPatch {
  precision_target_pct?: number;
  confidence_level?: number;
  note?: string;
}

export interface RefreshResult {
  task_id: string | null;
  queued: boolean;
  message: string;
  snapshot_id: number | null;
  snapshot_code: string | null;
  version: number | null;
  reused: boolean | null;
}

export const datasetApi = {
  list: () => http.get<ListEnvelope<Dataset>>('/datasets'),
  detail: (id: number) => http.get<Dataset>(`/datasets/${id}`),
  strata: (id: number) => http.get<ListEnvelope<Stratum>>(`/datasets/${id}/strata`),
  snapshots: (id: number) => http.get<ListEnvelope<DataSnapshot>>(`/datasets/${id}/snapshots`),
  patch: (id: number, body: DatasetPatch) => http.patch<Dataset>(`/datasets/${id}`, body),
  refreshSnapshot: (id: number) => http.post<RefreshResult>(`/datasets/${id}/refresh-snapshot`),
  // 旧路径保留，语义已升级为发布不可变快照
  refreshTruth: (id: number) => http.post<RefreshResult>(`/datasets/${id}/refresh-truth`),
};
