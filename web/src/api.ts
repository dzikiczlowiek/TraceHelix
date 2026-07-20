import type { components } from './api/generated';

export type RunSummary = components['schemas']['RunSummaryDto'];
export type RunDetail = components['schemas']['RunDetailDto'];
export type TraceEvent = components['schemas']['EventDto'];
export type EventPage = components['schemas']['EventPageDto'];
export type Classification = components['schemas']['ClassificationDto'];
export type Analysis = components['schemas']['AnalysisDto'];
export type Alert = components['schemas']['AlertDto'];
export type Alerts = components['schemas']['AlertsDto'];
export type CompareSide = components['schemas']['CompareSideDto'];
export type Comparison = components['schemas']['ComparisonDto'];
export type ApiProblem = components['schemas']['ProblemDetails'];

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    public readonly problem: ApiProblem,
  ) {
    super(problem.detail ?? problem.title ?? 'Request failed');
  }
}

const base = (import.meta.env.VITE_API_BASE as string | undefined) ?? '';

export async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${base}${path}`, init);
  if (!response.ok) {
    let problem: ApiProblem = { title: 'Request failed', status: response.status };
    try {
      const value: unknown = await response.json();
      if (value && typeof value === 'object') problem = value as ApiProblem;
    } catch {
      // Preserve a useful status/title when an intermediary returns non-JSON.
    }
    throw new ApiError(response.status, problem);
  }
  return response.json() as Promise<T>;
}

export const api = {
  runs: () => request<RunSummary[]>('/api/v1/runs'),
  run: (id: string) => request<RunDetail>(`/api/v1/runs/${encodeURIComponent(id)}`),
  events: (id: string, cursor?: string | number | null, limit = 200) => {
    const after = cursor == null ? '' : `&cursor=${encodeURIComponent(String(cursor))}`;
    return request<EventPage>(`/api/v1/runs/${encodeURIComponent(id)}/events?limit=${limit}${after}`);
  },
  analysis: (id: string, signal?: AbortSignal) => request<Analysis>(`/api/v1/runs/${encodeURIComponent(id)}/analysis/latest`, { signal }),
  alerts: (id: string, signal?: AbortSignal) => request<Alerts>(`/api/v1/runs/${encodeURIComponent(id)}/alerts`, { signal }),
  analyze: (id: string) => request<Analysis>(`/api/v1/runs/${encodeURIComponent(id)}/analysis/rules`, { method: 'POST' }),
  compare: (left: string, right: string) => request<Comparison>(`/api/v1/compare?left=${encodeURIComponent(left)}&right=${encodeURIComponent(right)}`),
};
