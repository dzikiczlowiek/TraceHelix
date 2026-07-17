import { StrictMode, useState, useSyncExternalStore, type FormEvent, type ReactNode } from 'react';
import { createRoot } from 'react-dom/client';
import { QueryClient, QueryClientProvider, useInfiniteQuery, useQuery } from '@tanstack/react-query';
import { api, type RunSummary, type TraceEvent } from './api';
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
  const events = useInfiniteQuery({
    queryKey: ['events', id],
    initialPageParam: null as string | number | null,
    queryFn: ({ pageParam }) => api.events(id, pageParam),
    getNextPageParam: page => page.nextCursor ?? undefined,
  });
  const items = Array.from(new Map(events.data?.pages.flatMap(page => page.items).map(event => [event.id, event]) ?? []).values());
  const next = events.hasNextPage;
  return <main><a href="/" onClick={event => { event.preventDefault(); navigate('/'); }}>All runs</a><State loading={run.isLoading || events.isLoading} error={run.isError || events.isError}><h1>{run.data?.name}</h1>{run.data && <><p>Input hash: <code>{run.data.inputSha256}</code></p><p role="status">Showing {items.length} of {run.data.eventCount} events</p></>}{events.data && <><Dna events={items} />{next && <button type="button" disabled={events.isFetchingNextPage} onClick={() => void events.fetchNextPage()}>{events.isFetchingNextPage ? 'Loading more…' : 'Load more events'}</button>}</>}</State></main>;
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
  useSyncExternalStore(subscribeLocation, locationSnapshot, locationSnapshot);
  const detail = location.pathname.match(/^\/runs\/([^/]+)$/);
  if (detail) return <RunPage id={decodeURIComponent(detail[1])} />;
  if (location.pathname === '/compare') { const params = new URLSearchParams(location.search); return <Compare left={params.get('left') ?? ''} right={params.get('right') ?? ''} />; }
  return <Runs />;
}

const client = new QueryClient();
const root = document.getElementById('root');
if (root) createRoot(root).render(<StrictMode><QueryClientProvider client={client}><App /></QueryClientProvider></StrictMode>);
