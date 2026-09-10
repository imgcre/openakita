import { beforeEach, afterEach, expect, it, vi } from 'vitest';
import { buildWebMarketplaceUrl, captureWebInstallReturn, pendingWebInstall, saveWebInstallJob, dismissWebInstall } from '../web';

const endpoint = 'https://marketplace.openakita.cn';
const token = 'a'.repeat(64);
beforeEach(() => { sessionStorage.clear(); history.replaceState(null, '', '/proxy/web?local=value#plugins'); });
afterEach(() => vi.useRealTimers());
function returnFromMarket(state: string, overrides: Record<string, string> = {}) {
  history.replaceState(null, '', '/proxy/web#' + new URLSearchParams({ 'openakita-install': token, state, endpoint, ...overrides }));
  captureWebInstallReturn();
}
it('keeps credentials and the original route local, accepts the bound return and persists it across reads', () => {
  localStorage.setItem('openakita_access_token', 'secret');
  const url = new URL(buildWebMarketplaceUrl('1.27.40', location.origin));
  expect(url.searchParams.get('client')).toBe('web');
  expect(url.searchParams.get('return_url')).toBe(location.origin + '/proxy/web');
  expect(url.href).not.toMatch(/secret|local=value|plugins/);
  returnFromMarket(url.searchParams.get('state')!);
  expect(location.hash).toBe('#plugins');
  expect(location.search).toBe('?local=value');
  expect(pendingWebInstall(location.origin)?.token).toBe(token);
  const pending = pendingWebInstall(location.origin)!;
  saveWebInstallJob(pending, 'job');
  expect(pendingWebInstall(location.origin)?.token).toBeUndefined();
  expect(pendingWebInstall(location.origin)?.jobId).toBe('job');
  dismissWebInstall();
  expect(pendingWebInstall(location.origin)).toBeNull();
});
it.each(['state', 'endpoint', 'openakita-install'])('rejects a modified %s without accepting a ticket', (field) => {
  const url = new URL(buildWebMarketplaceUrl('1.27.40', location.origin));
  returnFromMarket(url.searchParams.get('state')!, { [field]: 'untrusted' });
  expect(() => pendingWebInstall(location.origin)).toThrow('marketplace_instruction_invalid');
  expect(pendingWebInstall(location.origin)).toBeNull();
});
it('rejects a different backend, expired state and replayed returns', () => {
  const url = new URL(buildWebMarketplaceUrl('1.27.40', location.origin));
  const state = url.searchParams.get('state')!;
  returnFromMarket(state);
  expect(() => pendingWebInstall('https://other.example')).toThrow('marketplace_target_changed');
  saveWebInstallJob(pendingWebInstall(location.origin)!, 'job');
  returnFromMarket(state);
  expect(() => pendingWebInstall(location.origin)).toThrow('marketplace_instruction_invalid');
  vi.useFakeTimers(); vi.setSystemTime(Date.now() + 31 * 60_000);
  returnFromMarket(state);
  expect(() => pendingWebInstall(location.origin)).toThrow('marketplace_context_expired');
});
it('rejects an installation link opened in a different tab without originating context', () => {
  returnFromMarket('b'.repeat(64));
  expect(() => pendingWebInstall(location.origin)).toThrow('marketplace_context_expired');
  expect(location.hash).toBe('');
});
