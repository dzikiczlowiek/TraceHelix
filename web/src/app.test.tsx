import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { App, Compare, Dna, Runs } from './main';
import type { TraceEvent } from './api';

const json = (body: unknown, status = 200) => Promise.resolve(new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }));
const renderQuery = (node: React.ReactNode, client = new QueryClient({ defaultOptions: { queries: { retry: false } } })) => {
  const result = render(<QueryClientProvider client={client}>{node}</QueryClientProvider>);
  return { ...result, client };
};
const run = { id: '11111111-1111-1111-1111-111111111111', name: 'run one', importedAt: '2026-01-01T00:00:00Z', adapter: 'generic-jsonl', adapterVersion: '1', eventCount: 2, diagnosticCount: 0 };
const detail = { ...run, inputSha256: 'abc' };
const traceEvent = (sequence: number): TraceEvent => ({ id: `00000000-0000-0000-0000-${String(sequence).padStart(12, '0')}`, sequence, timestamp: '2026-01-01T00:00:00Z', kind: 'Message', actor: 'agent', summary: `event ${sequence}`, payload: {}, contentSha256: 'hash' });

afterEach(() => {
  vi.unstubAllGlobals();
  history.replaceState({}, '', '/');
  dispatchEvent(new PopStateEvent('popstate'));
});

describe('runs', () => {
  it('renders success and the accessible compare picker', async () => {
    vi.stubGlobal('fetch', vi.fn(() => json([run])));
    renderQuery(<Runs />);
    expect(await screen.findByRole('link', { name: 'run one' })).toBeVisible();
    expect(screen.getAllByRole('combobox')).toHaveLength(2);
    await userEvent.selectOptions(screen.getAllByRole('combobox')[0], run.id);
    await userEvent.selectOptions(screen.getAllByRole('combobox')[1], run.id);
    await userEvent.click(screen.getByRole('button', { name: 'Compare selected runs' }));
    expect(location.search).toContain(`left=${run.id}`);
    expect(location.search).toContain(`right=${run.id}`);
  });
  it('renders empty', async () => { vi.stubGlobal('fetch', vi.fn(() => json([]))); renderQuery(<Runs />); expect(await screen.findByText('No runs found.')).toBeVisible(); });
  it('renders errors', async () => { vi.stubGlobal('fetch', vi.fn(() => json({}, 500))); renderQuery(<Runs />); expect(await screen.findByRole('alert')).toHaveTextContent('Unable to load data.'); });
});

it('selects DNA evidence by click and keyboard with valid list markup', async () => {
  const event: TraceEvent = { id: run.id, sequence: 0, timestamp: '2026-01-01T00:00:00Z', kind: 'ToolCall', actor: 'agent', summary: 'called tool', payload: { proof: 1 }, contentSha256: 'abc' };
  render(<Dna events={[event]} />);
  const button = screen.getByRole('button', { name: /Sequence 0/ });
  expect(button.closest('li')).not.toBeNull();
  expect(button).not.toHaveAttribute('role', 'listitem');
  button.focus();
  await userEvent.keyboard('{Enter}');
  expect(await screen.findByText('called tool')).toBeVisible();
  expect(screen.getByText('abc')).toBeVisible();
  expect(screen.getByText(/"proof": 1/)).toBeInTheDocument();
});

describe('production App navigation and pagination', () => {
  it('rerenders run detail and browser popstate destinations', async () => {
    history.replaceState({}, '', '/');
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith('/runs')) return json([run]);
      if (url.endsWith('/analysis/latest')) return json({}, 404);
      if (url.includes('/events?')) return json({ items: [traceEvent(0), traceEvent(1)], nextCursor: null, limit: 200 });
      return json(detail);
    }));
    renderQuery(<App />);
    await userEvent.click(await screen.findByRole('link', { name: 'run one' }));
    const detailHeading = await screen.findByRole('heading', { name: 'run one' });
    expect(detailHeading).toHaveFocus();
    expect(document.title).toBe('run one · TraceHelix');
    expect(screen.getByText('Showing 2 of 2 events')).toBeVisible();
    history.replaceState({}, '', '/');
    dispatchEvent(new PopStateEvent('popstate'));
    const runsHeading = await screen.findByRole('heading', { name: 'TraceHelix runs' });
    expect(runsHeading).toHaveFocus();
    expect(document.title).toBe('TraceHelix runs · TraceHelix');
  });

  it('focuses and titles a failed asynchronous run destination', async () => {
    history.replaceState({}, '', '/');
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith('/runs')) return json([run]);
      return json({ title: 'Unavailable', status: 503 }, 503);
    }));
    renderQuery(<App />);
    await userEvent.click(await screen.findByRole('link', { name: 'run one' }));
    const heading = await screen.findByRole('heading', { name: 'Run details' });
    expect(heading).toHaveFocus();
    expect(document.title).toBe('Run details · TraceHelix');
    expect(await screen.findByRole('alert')).toHaveTextContent('Unable to load data.');
  });

  it('renders the compare submission destination', async () => {
    const comparison = { left: { runId: run.id, eventCount: 2, classificationCounts: {}, alertCount: 0 }, right: { runId: run.id, eventCount: 2, classificationCounts: {}, alertCount: 0 }, summary: 'x' };
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => String(input).includes('/compare?') ? json(comparison) : json([run])));
    renderQuery(<App />);
    const selects = await screen.findAllByRole('combobox');
    await userEvent.selectOptions(selects[0], run.id);
    await userEvent.selectOptions(selects[1], run.id);
    await userEvent.click(screen.getByRole('button', { name: 'Compare selected runs' }));
    expect(await screen.findByText('Independent summaries only; observed differences are not causal proof.')).toBeVisible();
    expect(screen.getAllByText('2 events (denominator)')).toHaveLength(2);
  });

  it('shows an actionable empty analysis state without requesting alerts', async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith('/analysis/latest')) return json({}, 404);
      if (url.includes('/events?')) return json({ items: [traceEvent(0), traceEvent(1)], nextCursor: null, limit: 200 });
      return json(detail);
    });
    vi.stubGlobal('fetch', fetchMock);
    history.replaceState({}, '', `/runs/${run.id}`);
    renderQuery(<App />);
    expect(await screen.findByRole('heading', { name: 'Analysis' })).toBeVisible();
    expect(await screen.findByText('No analysis has been run for this trace.')).toBeVisible();
    expect(screen.getByRole('button', { name: 'Run rules analysis' })).toBeEnabled();
    expect(fetchMock.mock.calls.some(([input]) => String(input).endsWith('/alerts'))).toBe(false);
  });

  it('runs rules analysis and renders the returned evidence-linked classifications', async () => {
    const analysis = {
      id: '22222222-2222-2222-2222-222222222222', runId: run.id, status: 'Completed',
      createdAt: '2026-01-01T00:01:00Z', classifierId: 'rules', classifierVersion: '1',
      classifications: [{ eventId: traceEvent(0).id, label: 'Plan', confidence: 0.8, evidenceEventIds: [traceEvent(0).id] }],
    };
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith('/analysis/rules') && init?.method === 'POST') return json(analysis);
      if (url.endsWith('/analysis/latest')) return json({}, 404);
      if (url.endsWith('/alerts')) return json({ analysisId: analysis.id, runId: run.id, items: [] });
      if (url.includes('/events?')) return json({ items: [traceEvent(0)], nextCursor: null, limit: 200 });
      return json({ ...detail, eventCount: 1 });
    });
    vi.stubGlobal('fetch', fetchMock);
    history.replaceState({}, '', `/runs/${run.id}`);
    renderQuery(<App />);
    await screen.findByText('No analysis has been run for this trace.', {}, { timeout: 3000 });
    await userEvent.click(screen.getByRole('button', { name: 'Run rules analysis' }));
    expect(await screen.findByText('Plan')).toBeVisible();
    expect(screen.getByText('Classifier: rules 1')).toBeVisible();
    expect(screen.getByText('Confidence: 80%')).toBeVisible();
    expect(screen.getAllByText(traceEvent(0).id)).toHaveLength(2);
    expect(fetchMock).toHaveBeenCalledWith(expect.stringContaining('/analysis/rules'), { method: 'POST' });
  });

  it('keeps analysis retryable and shows the safe API problem detail when execution fails', async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith('/analysis/rules') && init?.method === 'POST') return json({ title: 'Analysis failed', detail: 'The rules analysis could not be completed.', status: 500 }, 500);
      if (url.endsWith('/analysis/latest')) return json({}, 404);
      if (url.includes('/events?')) return json({ items: [], nextCursor: null, limit: 200 });
      return json({ ...detail, eventCount: 0 });
    });
    vi.stubGlobal('fetch', fetchMock);
    history.replaceState({}, '', `/runs/${run.id}`);
    renderQuery(<App />);
    await screen.findByText('No analysis has been run for this trace.');
    await userEvent.click(screen.getByRole('button', { name: 'Run rules analysis' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('The rules analysis could not be completed.');
    expect(screen.getByRole('button', { name: 'Run rules analysis' })).toBeEnabled();
  });

  it('can rerun an existing analysis and replaces the displayed revision', async () => {
    const existing = {
      id: '22222222-2222-2222-2222-222222222222', runId: run.id, status: 'Completed',
      createdAt: '2026-01-01T00:01:00Z', classifierId: 'rules', classifierVersion: '1', classifications: [],
    };
    const replacement = { ...existing, id: '33333333-3333-3333-3333-333333333333', classifierVersion: '2' };
    let didPost = false;
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith('/analysis/rules') && init?.method === 'POST') {
        didPost = true;
        return json(replacement);
      }
      if (url.endsWith('/analysis/latest')) return json(didPost ? replacement : existing);
      if (url.endsWith('/alerts')) return json({ analysisId: didPost ? replacement.id : existing.id, runId: run.id, items: [] });
      if (url.includes('/events?')) return json({ items: [], nextCursor: null, limit: 200 });
      return json({ ...detail, eventCount: 0 });
    }));
    history.replaceState({}, '', `/runs/${run.id}`);
    renderQuery(<App />);
    expect(await screen.findByText('Classifier: rules 1')).toBeVisible();
    await userEvent.click(screen.getByRole('button', { name: 'Run analysis again' }));
    expect(await screen.findByText('Classifier: rules 2')).toBeVisible();
    expect(screen.queryByText('Classifier: rules 1')).not.toBeInTheDocument();
  });

  it('renders alerts with severity, sequence range, explanation, and evidence IDs', async () => {
    const analysis = {
      id: '22222222-2222-2222-2222-222222222222', runId: run.id, status: 'Completed',
      createdAt: '2026-01-01T00:01:00Z', classifierId: 'rules', classifierVersion: '1', classifications: [],
    };
    const alerts = {
      analysisId: analysis.id, runId: run.id,
      items: [{ code: 'THX001_NO_PROGRESS_LOOP', severity: 'Critical', startSequence: 2, endSequence: 4, evidenceEventIds: [traceEvent(2).id], explanation: 'Repeated tool failure.' }],
    };
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith('/analysis/latest')) return json(analysis);
      if (url.endsWith('/alerts')) return json(alerts);
      if (url.includes('/events?')) return json({ items: [], nextCursor: null, limit: 200 });
      return json({ ...detail, eventCount: 0 });
    }));
    history.replaceState({}, '', `/runs/${run.id}`);
    renderQuery(<App />);
    expect(await screen.findByRole('heading', { name: 'Alerts' })).toBeVisible();
    expect(await screen.findByText('THX001_NO_PROGRESS_LOOP')).toBeVisible();
    expect(screen.getByText('Critical severity')).toBeVisible();
    expect(screen.getByText('Sequences 2–4')).toBeVisible();
    expect(screen.getByText('Repeated tool failure.')).toBeVisible();
    expect(screen.getByText(traceEvent(2).id)).toBeVisible();
  });

  it('announces analysis progress in a live status region', async () => {
    const analysis = {
      id: '22222222-2222-2222-2222-222222222222', runId: run.id, status: 'Completed',
      createdAt: '2026-01-01T00:01:00Z', classifierId: 'rules', classifierVersion: '1', classifications: [],
    };
    let resolveAnalysis: ((response: Response) => void) | undefined;
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith('/analysis/rules') && init?.method === 'POST') {
        return new Promise<Response>(resolve => { resolveAnalysis = resolve; });
      }
      if (url.endsWith('/analysis/latest')) return json({}, 404);
      if (url.endsWith('/alerts')) return json({ analysisId: analysis.id, runId: run.id, items: [] });
      if (url.includes('/events?')) return json({ items: [], nextCursor: null, limit: 200 });
      return json({ ...detail, eventCount: 0 });
    }));
    history.replaceState({}, '', `/runs/${run.id}`);
    renderQuery(<App />);
    await screen.findByText('No analysis has been run for this trace.');
    await userEvent.click(screen.getByRole('button', { name: 'Run rules analysis' }));
    expect(screen.getByText('Running rules analysis…', { selector: 'p' })).toHaveAttribute('role', 'status');
    resolveAnalysis?.(new Response(JSON.stringify(analysis), { status: 200, headers: { 'Content-Type': 'application/json' } }));
    expect(await screen.findByText('Classifier: rules 1')).toBeVisible();
  });

  it('offers an explicit retry after the initial analysis request fails', async () => {
    const analysis = {
      id: '22222222-2222-2222-2222-222222222222', runId: run.id, status: 'Completed',
      createdAt: '2026-01-01T00:01:00Z', classifierId: 'rules', classifierVersion: '1', classifications: [],
    };
    let analysisGets = 0;
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith('/analysis/latest')) {
        analysisGets += 1;
        return analysisGets <= 3 ? json({ title: 'Unavailable', status: 503 }, 503) : json(analysis);
      }
      if (url.endsWith('/alerts')) return json({ analysisId: analysis.id, runId: run.id, items: [] });
      if (url.includes('/events?')) return json({ items: [], nextCursor: null, limit: 200 });
      return json({ ...detail, eventCount: 0 });
    }));
    history.replaceState({}, '', `/runs/${run.id}`);
    renderQuery(<App />);
    expect(await screen.findByRole('alert', {}, { timeout: 5000 })).toHaveTextContent('Unable to load analysis.');
    await userEvent.click(screen.getByRole('button', { name: 'Retry loading analysis' }));
    expect(await screen.findByText('Classifier: rules 1')).toBeVisible();
  });

  it('cancels a stale analysis refetch so it cannot overwrite a completed rerun', async () => {
    const existing = {
      id: '22222222-2222-2222-2222-222222222222', runId: run.id, status: 'Completed',
      createdAt: '2026-01-01T00:01:00Z', classifierId: 'rules', classifierVersion: '1', classifications: [],
    };
    const replacement = { ...existing, id: '33333333-3333-3333-3333-333333333333', classifierVersion: '2' };
    let analysisGets = 0;
    let alertsGets = 0;
    let didPost = false;
    let staleSignal: AbortSignal | undefined;
    let staleAlertsSignal: AbortSignal | undefined;
    let analysisWasAbortedAtPost = false;
    let alertsWereAbortedAtPost = false;
    let resolveStale: ((response: Response) => void) | undefined;
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith('/analysis/rules') && init?.method === 'POST') {
        analysisWasAbortedAtPost = staleSignal?.aborted === true;
        alertsWereAbortedAtPost = staleAlertsSignal?.aborted === true;
        didPost = true;
        return json(replacement);
      }
      if (url.endsWith('/analysis/latest')) {
        analysisGets += 1;
        if (analysisGets === 1) return json(existing);
        if (analysisGets >= 3) return json(replacement);
        return new Promise<Response>((resolve, reject) => {
          resolveStale = resolve;
          staleSignal = init?.signal ?? undefined;
          init?.signal?.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')), { once: true });
        });
      }
      if (url.endsWith('/alerts')) {
        alertsGets += 1;
        if (alertsGets === 2) {
          return new Promise<Response>((_resolve, reject) => {
            staleAlertsSignal = init?.signal ?? undefined;
            init?.signal?.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')), { once: true });
          });
        }
        const analysisId = didPost ? replacement.id : existing.id;
        return json({ analysisId, runId: run.id, items: [] });
      }
      if (url.includes('/events?')) return json({ items: [], nextCursor: null, limit: 200 });
      return json({ ...detail, eventCount: 0 });
    }));
    history.replaceState({}, '', `/runs/${run.id}`);
    const { client } = renderQuery(<App />);
    expect(await screen.findByText('Classifier: rules 1')).toBeVisible();
    void client.invalidateQueries({ queryKey: ['analysis', run.id] });
    void client.invalidateQueries({ queryKey: ['alerts', run.id] });
    await waitFor(() => {
      expect(analysisGets).toBe(2);
      expect(alertsGets).toBe(2);
    });
    await userEvent.click(screen.getByRole('button', { name: 'Run analysis again' }));
    expect(analysisWasAbortedAtPost).toBe(true);
    expect(alertsWereAbortedAtPost).toBe(true);
    expect(await screen.findByText('Classifier: rules 2')).toBeVisible();
    expect(staleSignal?.aborted).toBe(true);
    await waitFor(() => expect(analysisGets).toBeGreaterThanOrEqual(3));
    resolveStale?.(new Response(JSON.stringify(existing), { status: 200, headers: { 'Content-Type': 'application/json' } }));
    await new Promise(resolve => setTimeout(resolve, 0));
    expect(screen.queryByText('Classifier: rules 1')).not.toBeInTheDocument();
  });

  it('never renders alerts from a different analysis revision and refreshes to the matching latest analysis', async () => {
    const existing = {
      id: '22222222-2222-2222-2222-222222222222', runId: run.id, status: 'Completed',
      createdAt: '2026-01-01T00:01:00Z', classifierId: 'rules', classifierVersion: '1', classifications: [],
    };
    const latest = { ...existing, id: '33333333-3333-3333-3333-333333333333', classifierVersion: '2' };
    const mismatchedAlerts = {
      analysisId: latest.id, runId: run.id,
      items: [{ code: 'THX-RACE', severity: 'Critical', startSequence: 1, endSequence: 2, evidenceEventIds: [], explanation: 'Latest revision alert.' }],
    };
    let analysisGets = 0;
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith('/analysis/latest')) {
        analysisGets += 1;
        return json(analysisGets === 1 ? existing : latest);
      }
      if (url.endsWith('/alerts')) return json(mismatchedAlerts);
      if (url.includes('/events?')) return json({ items: [], nextCursor: null, limit: 200 });
      return json({ ...detail, eventCount: 0 });
    }));
    history.replaceState({}, '', `/runs/${run.id}`);
    renderQuery(<App />);
    expect(await screen.findByText('Classifier: rules 2')).toBeVisible();
    expect(await screen.findByText('THX-RACE')).toBeVisible();
    expect(analysisGets).toBeGreaterThanOrEqual(2);
  });

  it('recovers from a transient alerts request failure through an explicit retry', async () => {
    const existing = {
      id: '22222222-2222-2222-2222-222222222222', runId: run.id, status: 'Completed',
      createdAt: '2026-01-01T00:01:00Z', classifierId: 'rules', classifierVersion: '1', classifications: [],
    };
    let alertGets = 0;
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith('/analysis/latest')) return json(existing);
      if (url.endsWith('/alerts')) {
        alertGets += 1;
        return alertGets === 1
          ? json({ detail: 'Temporary alerts failure.' }, 503)
          : json({ analysisId: existing.id, runId: run.id, items: [{ code: 'THX-RECOVERED', severity: 'Warning', startSequence: 1, endSequence: 2, evidenceEventIds: [], explanation: 'Recovered alert.' }] });
      }
      if (url.includes('/events?')) return json({ items: [], nextCursor: null, limit: 200 });
      return json({ ...detail, eventCount: 0 });
    }));
    history.replaceState({}, '', `/runs/${run.id}`);
    renderQuery(<App />);
    expect(await screen.findByText('Unable to load alerts.')).toBeVisible();
    await userEvent.click(screen.getByRole('button', { name: 'Retry loading alerts' }));
    expect(await screen.findByText('THX-RECOVERED')).toBeVisible();
    expect(alertGets).toBe(2);
  });

  it('stops automatic revision reconciliation and offers an explicit retry when latest does not advance', async () => {
    const existing = {
      id: '22222222-2222-2222-2222-222222222222', runId: run.id, status: 'Completed',
      createdAt: '2026-01-01T00:01:00Z', classifierId: 'rules', classifierVersion: '1', classifications: [],
    };
    let analysisGets = 0;
    let alertGets = 0;
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith('/analysis/latest')) {
        analysisGets += 1;
        return json(existing);
      }
      if (url.endsWith('/alerts')) {
        alertGets += 1;
        return json({ analysisId: '33333333-3333-3333-3333-333333333333', runId: run.id, items: [] });
      }
      if (url.includes('/events?')) return json({ items: [], nextCursor: null, limit: 200 });
      return json({ ...detail, eventCount: 0 });
    }));
    history.replaceState({}, '', `/runs/${run.id}`);
    renderQuery(<App />);
    expect(await screen.findByText('Analysis and alerts are out of sync.')).toBeVisible();
    expect(screen.queryByText(/Refreshing…/)).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Retry loading alerts' }));
    await waitFor(() => expect(alertGets).toBe(2));
    expect(analysisGets).toBeGreaterThanOrEqual(2);
    expect(screen.getByText('Analysis and alerts are out of sync.')).toBeVisible();
  });

  it('shows partial event counts and explicitly loads and appends the next cursor without duplicates', async () => {
    const first = Array.from({ length: 200 }, (_, index) => traceEvent(index));
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith('/analysis/latest')) return json({}, 404);
      if (url.includes('/events?') && url.includes('cursor=199')) return json({ items: [traceEvent(199), traceEvent(200)], nextCursor: null, limit: 200 });
      if (url.includes('/events?')) return json({ items: first, nextCursor: 199, limit: 200 });
      return json({ ...detail, eventCount: 201 });
    });
    vi.stubGlobal('fetch', fetchMock);
    history.replaceState({}, '', `/runs/${run.id}`);
    renderQuery(<App />);
    expect(await screen.findByText('Showing 200 of 201 events')).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: 'Load more events' }));
    expect(await screen.findByText('Showing 201 of 201 events')).toBeVisible();
    expect(screen.queryByRole('button', { name: 'Load more events' })).not.toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith(expect.stringContaining('cursor=199'), undefined);
    expect(screen.getAllByRole('listitem')).toHaveLength(201);
  });
});

it('shows compare raw counts and denominators and prompts for missing IDs', async () => {
  const value = { left: { runId: run.id, eventCount: 2, classificationCounts: { Plan: 1 }, alertCount: 0 }, right: { runId: run.id, eventCount: 4, classificationCounts: { Plan: 3 }, alertCount: 1 }, summary: 'x' };
  vi.stubGlobal('fetch', vi.fn(() => json(value)));
  const { unmount } = renderQuery(<Compare left={run.id} right={run.id} />);
  expect(await screen.findByText('1 / 2')).toBeVisible();
  expect(screen.getByText('3 / 4')).toBeVisible();
  unmount();
  renderQuery(<Compare left="" right="" />);
  expect(screen.getByText(/Select both runs/)).toBeVisible();
  await waitFor(() => expect(fetch).toHaveBeenCalledTimes(1));
});
