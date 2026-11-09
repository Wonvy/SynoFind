import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { api, ApiError, setCsrfToken } from './api';

describe('authenticated API client', () => {
  const dispatch = vi.fn();
  beforeEach(() => { setCsrfToken('csrf-value'); vi.stubGlobal('window', { dispatchEvent: dispatch }); dispatch.mockReset(); });
  afterEach(() => { vi.unstubAllGlobals(); });

  it('sends session mutations with a CSRF header and cookie credentials', async () => {
    const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify({ started: true }), { status: 202 }));
    vi.stubGlobal('fetch', fetcher);
    await api('/index/scan', { method: 'POST', body: '{}' });
    expect(fetcher.mock.calls[0][0]).toBe('/api/v1/index/scan');
    const options = fetcher.mock.calls[0][1] as RequestInit;
    expect(options.credentials).toBe('same-origin');
    expect(new Headers(options.headers).get('X-CSRF-Token')).toBe('csrf-value');
    expect(new Headers(options.headers).get('Content-Type')).toBe('application/json');
  });

  it('does not include the CSRF token on reads', async () => {
    const fetcher = vi.fn().mockResolvedValue(new Response('{}'));
    vi.stubGlobal('fetch', fetcher);
    await api('/settings');
    expect(new Headers(fetcher.mock.calls[0][1].headers).has('X-CSRF-Token')).toBe(false);
  });

  it('preserves API error details and expires an invalid session', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ error: { code: 'unauthorized', message: '请重新登录' } }), { status: 401 })));
    await expect(api('/tokens')).rejects.toMatchObject({ message: '请重新登录', status: 401, code: 'unauthorized' });
    expect(dispatch).toHaveBeenCalledTimes(1);
  });

  it('keeps invalid login on the login form and handles an empty logout response', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('{}', { status: 401 })));
    await expect(api('/auth/login', { method: 'POST', body: '{}' })).rejects.toBeInstanceOf(ApiError);
    expect(dispatch).not.toHaveBeenCalled();
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(null, { status: 204 })));
    await expect(api('/auth/logout', { method: 'POST' })).resolves.toBeUndefined();
  });
});
