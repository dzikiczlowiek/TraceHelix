import { expect, it, vi } from 'vitest';
import { request } from './api';

it('parses ProblemDetails errors', async () => {
  vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(new Response(JSON.stringify({ title: 'Invalid request', status: 400, detail: 'bad cursor' }), { status: 400, headers: { 'Content-Type': 'application/problem+json' } }))));
  await expect(request('/broken')).rejects.toMatchObject({ status: 400, problem: { title: 'Invalid request', detail: 'bad cursor' } });
});

it('normalizes non-JSON errors', async () => {
  vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(new Response('upstream exploded', { status: 502 }))));
  await expect(request('/broken')).rejects.toMatchObject({ status: 502, problem: { title: 'Request failed', status: 502 } });
});
