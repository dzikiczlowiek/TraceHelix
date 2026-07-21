import { StrictMode, useEffect, useRef, useState, useSyncExternalStore, type FormEvent, type ReactNode } from 'react';
import { createRoot } from 'react-dom/client';
import { QueryClient, QueryClientProvider, useInfiniteQuery, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { ApiError, api, type RunSummary, type TraceEvent } from './api';
import './style.css';

const navigate = (path: string) => {
  history.pushState({}, '', path);
  dispatchEvent(new PopStateEvent('popstate'));
};

function State({ loading, error, empty, children }: { loading: boolean; error: boolean; empty?: boolean; children: ReactNode }) {
  if (loading) return <p role="status">Loading…</p>;
  if (error) return <p role="alert">Unable to load data.</p>;
  if (empty) return <p>No runs found.</p>;
  return <>{children}</>;
}

function ComparePicker({ runs }: { runs: RunSummary[] }) {
  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const values = new FormData(event.currentTarget);
    navigate(`/compare?left=${encodeURIComponent(String(values.get('left')))}&right=${encodeURIComponent(String(values.get('right')))}`);
  };
  return (
    <section aria-labelledby="compare-heading">
      <h2 id="compare-heading">Compare runs</h2>
      <form onSubmit={submit}>
        <label>Left run <select name="left" required defaultValue=""><option value="" disabled>Choose a run</option>{runs.map(run => <option key={run.id} value={run.id}>{run.name}</option>)}</select></label>
        <label>Right run <select name="right" required defaultValue=""><option value="" disabled>Choose a run</option>{runs.map(run => <option key={run.id} value={run.id}>{run.name}</option>)}</select></label>
        <button type="submit">Compare selected runs</button>
      </form>
    </section>
  );
}

export function Runs() {
  const query = useQuery({ queryKey: ['runs'], queryFn: api.runs });
  return (
    <main>
      <h1>TraceHelix runs</h1>
      <State loading={query.isLoading} error={query.isError} empty={query.data?.length === 0}>
        <ul>{query.data?.map(run => <li key={run.id}><a href={`/runs/${run.id}`} onClick={event => { event.preventDefault(); navigate(`/runs/${run.id}`); }}>{run.name}</a> — {run.eventCount} events</li>)}</ul>
        {query.data && <ComparePicker runs={query.data} />}
      </State>
    </main>
  );
}

export function Dna({ events }: { events: TraceEvent[] }) {
  const [selected, setSelected] = useState<TraceEvent>();
  return (
    <section>
      <h2>Event DNA sequence</h2>
      <ul className="dna" aria-label="Trace event sequence">
        {events.map(event => (
          <li key={event.id}>
            <button className={`event ${event.kind}`} aria-label={`Sequence ${event.sequence}: ${event.kind}, ${event.summary ?? 'no summary'}`} onClick={() => setSelected(event)}>
              <span>{event.sequence}</span><small>{event.kind}</small>
            </button>
          </li>
        ))}
      </ul>
      {selected && <article aria-live="polite"><h3>Event {selected.sequence}: {selected.kind}</h3><p>{selected.summary ?? 'No summary'}</p><p>Actor: {selected.actor}</p><p>Content hash: <code>{selected.contentSha256}</code></p><details><summary>Evidence payload</summary><pre>{JSON.stringify(selected.payload, null, 2)}</pre></details></article>}
    </section>
  );
}

function RunPage({ id }: { id: string }) {
  const run = useQuery({ queryKey: ['run', id], queryFn: () => api.run(id) });
  const heading = run.data?.name ?? 'Run details';
  useEffect(() => {
    document.title = `${heading} · TraceHelix`;
  }, [heading]);
  const events = useInfiniteQuery({
    queryKey: ['events', id],
    initialPageParam: null as string | number | null,
    queryFn: ({ pageParam }) => api.events(id, pageParam),
    getNextPageParam: page => page.nextCursor ?? undefined,
  });
  const items = Array.from(new Map(events.data?.pages.flatMap(page => page.items).map(event => [event.id, event]) ?? []).values());
  const next = events.hasNextPage;
  return <main><a href="/" onClick={event => { event.preventDefault(); navigate('/'); }}>All runs</a><h1>{heading}</h1><State loading={run.isLoading || events.isLoading} error={run.isError || events.isError}>{run.data && <><p>Input hash: <code>{run.data.inputSha256}</code></p><p role="status">Showing {items.length} of {run.data.eventCount} events</p></>}{events.data && <><Dna events={items} />{next && <button type="button" disabled={events.isFetchingNextPage} onClick={() => void events.fetchNextPage()}>{events.isFetchingNextPage ? 'Loading more…' : 'Load more events'}</button>}</>}<AnalysisPanel runId={id} /></State></main>;
}

class AnalysisRevisionMismatchError extends Error {}

function AnalysisPanel({ runId }: { runId: string }) {
  const queryClient = useQueryClient();
  const analysis = useQuery({
    queryKey: ['analysis', runId],
    queryFn: ({ signal }) => api.analysis(runId, signal),
    retry: (attempt, error) => !(error instanceof ApiError && error.status === 404) && attempt < 2,
  });
  const analyze = useMutation({
    mutationFn: () => api.analyze(runId),
    onMutate: async () => {
      await Promise.all([
        queryClient.cancelQueries({ queryKey: ['analysis', runId] }),
        queryClient.cancelQueries({ queryKey: ['alerts', runId] }),
      ]);
    },
    onSuccess: result => {
      queryClient.setQueryData(['analysis', runId], result);
      void queryClient.invalidateQueries({ queryKey: ['analysis', runId], exact: true });
    },
  });
  const analysisId = analysis.data?.id;
  const alerts = useQuery({
    queryKey: ['alerts', runId, analysisId],
    queryFn: async ({ signal }) => {
      const result = await api.alerts(runId, signal);
      if (result.analysisId !== analysisId) throw new AnalysisRevisionMismatchError('Analysis revision changed while alerts were loading.');
      return result;
    },
    enabled: Boolean(analysisId),
    retry: false,
  });
  const reconciledAnalysisId = useRef<string | undefined>(undefined);
  const alertsRevisionChanged = alerts.error instanceof AnalysisRevisionMismatchError;
  useEffect(() => {
    if (!alertsRevisionChanged || reconciledAnalysisId.current === analysisId) return;
    reconciledAnalysisId.current = analysisId;
    void queryClient.invalidateQueries({ queryKey: ['analysis', runId], exact: true });
  }, [alertsRevisionChanged, analysisId, queryClient, runId]);
  const retryAlerts = async () => {
    if (!alertsRevisionChanged) {
      await alerts.refetch();
      return;
    }
    const refreshed = await analysis.refetch();
    if (refreshed.data?.id === analysisId) await alerts.refetch();
  };
  const alertsRetryPending = alerts.isFetching || analysis.isFetching;
  const revisionRefreshPending = alertsRevisionChanged && analysis.isFetching;
  const missing = analysis.error instanceof ApiError && analysis.error.status === 404;
  return <section aria-labelledby="analysis-heading">
    <h2 id="analysis-heading">Analysis</h2>
    {analysis.isLoading && <p role="status">Loading analysis…</p>}
    {missing && <><p>No analysis has been run for this trace.</p><button type="button" disabled={analyze.isPending} onClick={() => analyze.mutate()}>{analyze.isPending ? 'Running analysis…' : 'Run rules analysis'}</button></>}
    {analysis.isError && !missing && <><p role="alert">Unable to load analysis.</p><button type="button" disabled={analysis.isFetching} onClick={() => void analysis.refetch()}>{analysis.isFetching ? 'Retrying…' : 'Retry loading analysis'}</button></>}
    {analyze.isPending && <p role="status">Running rules analysis…</p>}
    {analyze.isError && <p role="alert">{analyze.error instanceof ApiError ? analyze.error.message : 'Unable to run rules analysis.'}</p>}
    {analysis.data && <div className="analysis-summary">
      <p>Status: {analysis.data.status}</p>
      <p>Classifier: {analysis.data.classifierId} {analysis.data.classifierVersion}</p>
      <button type="button" disabled={analyze.isPending} onClick={() => analyze.mutate()}>{analyze.isPending ? 'Running analysis…' : 'Run analysis again'}</button>
      <h3>Classifications</h3>
      {analysis.data.classifications.length === 0
        ? <p>No classifications produced.</p>
        : <ul>{analysis.data.classifications.map(classification => <li key={classification.eventId}>
          <strong>{classification.label}</strong>
          <span>Confidence: {Math.round(Number(classification.confidence) * 100)}%</span>
          <span>Event: <code>{classification.eventId}</code></span>
          <span>Evidence: {classification.evidenceEventIds.map(id => <code key={id}>{id}</code>)}</span>
        </li>)}</ul>}
      <section aria-labelledby="alerts-heading">
        <h3 id="alerts-heading">Alerts</h3>
        {alerts.isLoading && <p role="status">Loading alerts…</p>}
        {alertsRevisionChanged && revisionRefreshPending && <p role="status">Analysis changed while alerts were loading. Refreshing…</p>}
        {alerts.isError && !revisionRefreshPending && <>
          <p role="alert">{alertsRevisionChanged ? 'Analysis and alerts are out of sync.' : 'Unable to load alerts.'}</p>
          <button type="button" disabled={alertsRetryPending} onClick={() => void retryAlerts()}>{alertsRetryPending ? 'Retrying…' : 'Retry loading alerts'}</button>
        </>}
        {alerts.data?.items.length === 0 && <p>No alerts detected.</p>}
        {alerts.data && alerts.data.items.length > 0 && <ul className="alerts">{alerts.data.items.map((alert, index) => <li key={`${alert.code}-${alert.startSequence}-${alert.endSequence}-${index}`}>
          <h4>{alert.code}</h4>
          <p>{alert.severity} severity</p>
          <p>Sequences {alert.startSequence}–{alert.endSequence}</p>
          <p>{alert.explanation}</p>
          <p>Evidence: {alert.evidenceEventIds.map(id => <code key={id}>{id}</code>)}</p>
        </li>)}</ul>}
      </section>
    </div>}
  </section>;
}

export function Compare({ left, right }: { left: string; right: string }) {
  const query = useQuery({ queryKey: ['compare', left, right], queryFn: () => api.compare(left, right), enabled: Boolean(left && right) });
  if (!left || !right) return <main><h1>Compare runs</h1><p>Select both runs from the runs list to compare them.</p><a href="/">Choose runs</a></main>;
  return (
    <main><h1>Compare runs</h1><p>Independent summaries only; observed differences are not causal proof.</p>
      <State loading={query.isLoading} error={query.isError}>{query.data && <div className="comparison">{(['left', 'right'] as const).map(side => <section key={side}><h2>{side === 'left' ? 'Left' : 'Right'}</h2><p>{query.data[side].eventCount} events (denominator)</p><p>{query.data[side].alertCount} alerts</p><dl>{Object.entries(query.data[side].classificationCounts).map(([label, count]) => <div key={label}><dt>{label}</dt><dd>{count} / {query.data[side].eventCount}</dd></div>)}</dl></section>)}</div>}</State>
    </main>
  );
}

const subscribeLocation = (notify: () => void) => {
  addEventListener('popstate', notify);
  return () => removeEventListener('popstate', notify);
};
const locationSnapshot = () => `${location.pathname}${location.search}`;

export function App() {
  const route = useSyncExternalStore(subscribeLocation, locationSnapshot, locationSnapshot);
  useEffect(() => {
    const heading = document.querySelector<HTMLElement>('main h1');
    if (!heading) return;
    heading.tabIndex = -1;
    document.title = `${heading.textContent?.trim() || 'TraceHelix'} · TraceHelix`;
    heading.focus();
  }, [route]);
  const detail = location.pathname.match(/^\/runs\/([^/]+)$/);
  if (detail) return <RunPage id={decodeURIComponent(detail[1])} />;
  if (location.pathname === '/compare') { const params = new URLSearchParams(location.search); return <Compare left={params.get('left') ?? ''} right={params.get('right') ?? ''} />; }
  return <Runs />;
}

const client = new QueryClient();
const root = document.getElementById('root');
if (root) createRoot(root).render(<StrictMode><QueryClientProvider client={client}><App /></QueryClientProvider></StrictMode>);
