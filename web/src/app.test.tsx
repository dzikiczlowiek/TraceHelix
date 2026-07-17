import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { App, Compare, Dna, Runs } from './main';
import type { TraceEvent } from './api';

const json = (body: unknown, status = 200) => Promise.resolve(new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }));
const renderQuery = (node: React.ReactNode) => render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>{node}</QueryClientProvider>);
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
  fireEvent.keyDown(button, { key: 'Enter' }); fireEvent.click(button);
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
      if (url.includes('/events?')) return json({ items: [traceEvent(0), traceEvent(1)], nextCursor: null, limit: 200 });
      return json(detail);
    }));
    renderQuery(<App />);
    await userEvent.click(await screen.findByRole('link', { name: 'run one' }));
    expect(await screen.findByRole('heading', { name: 'run one' })).toBeVisible();
    expect(screen.getByText('Showing 2 of 2 events')).toBeVisible();
    history.replaceState({}, '', '/');
    dispatchEvent(new PopStateEvent('popstate'));
    expect(await screen.findByRole('heading', { name: 'TraceHelix runs' })).toBeVisible();
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

  it('shows partial event counts and explicitly loads and appends the next cursor without duplicates', async () => {
    const first = Array.from({ length: 200 }, (_, index) => traceEvent(index));
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/events?') && url.includes('cursor=199')) return json({ items: [traceEvent(199), traceEvent(200)], nextCursor: null, limit: 200 });
      if (url.includes('/events?')) return json({ items: first, nextCursor: 199, limit: 200 });
      return json({ ...detail, eventCount: 201 });
    });
    vi.stubGlobal('fetch', fetchMock);
    history.replaceState({}, '', `/runs/${run.id}`);
    renderQuery(<App />);
    expect(await screen.findByText('Showing 200 of 201 events')).toBeVisible();
    await userEvent.click(screen.getByRole('button', { name: 'Load more events' }));
    expect(await screen.findByText('Showing 201 of 201 events')).toBeVisible();
    expect(screen.queryByRole('button', { name: 'Load more events' })).not.toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith(expect.stringContaining('cursor=199'), undefined);
    expect(screen.getAllByRole('listitem')).toHaveLength(201);
  });
});

it('shows compare raw counts and denominators and prompts for missing IDs', async () => {
  const value = { left: { runId: run.id, eventCount: 2, classificationCounts: { Planning: 1 }, alertCount: 0 }, right: { runId: run.id, eventCount: 4, classificationCounts: { Planning: 3 }, alertCount: 1 }, summary: 'x' };
  vi.stubGlobal('fetch', vi.fn(() => json(value)));
  const { unmount } = renderQuery(<Compare left={run.id} right={run.id} />);
  expect(await screen.findByText('1 / 2')).toBeVisible();
  expect(screen.getByText('3 / 4')).toBeVisible();
  unmount();
  renderQuery(<Compare left="" right="" />);
  expect(screen.getByText(/Select both runs/)).toBeVisible();
  await waitFor(() => expect(fetch).toHaveBeenCalledTimes(1));
});
