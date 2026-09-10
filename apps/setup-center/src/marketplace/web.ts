import { buildMarketplaceContextUrl } from './navigation';

const KEY = 'openakita.marketplace.web.v1';
const ERROR_KEY = KEY + '.error';
const TTL = 30 * 60_000;
export type WebInstallContext = {
  state: string; base: string; endpoint: string; returnUrl: string; expires: number;
  token?: string; jobId?: string; consumed?: boolean;
};
function read(): WebInstallContext | null {
  try { return JSON.parse(sessionStorage.getItem(KEY) || 'null'); } catch { return null; }
}
function write(value: WebInstallContext) { sessionStorage.setItem(KEY, JSON.stringify(value)); }
const baseUrl = (base: string) => new URL(base || location.origin, location.origin).href.replace(/\/+$/, '');

/** Same-tab navigation preserves the instance login in its own origin. No
 * instance credentials or API address are sent to the storefront. */
export function buildWebMarketplaceUrl(version: string, base: string, next = '/', origin?: string) {
  const page = new URL(location.href);
  if (!['https:', 'http:'].includes(page.protocol) || page.username || page.password) {
    throw new Error('marketplace_instruction_invalid');
  }
  // getRandomValues also works on LAN HTTP, where randomUUID is unavailable.
  const state = Array.from(crypto.getRandomValues(new Uint8Array(32)), b => b.toString(16).padStart(2, '0')).join('');
  const url = new URL(buildMarketplaceContextUrl(version, next, origin));
  write({ state, base: baseUrl(base), endpoint: url.origin, returnUrl: page.href, expires: Date.now() + TTL });
  sessionStorage.removeItem(ERROR_KEY);
  page.search = ''; page.hash = '';
  url.searchParams.set('client', 'web');
  url.searchParams.set('state', state);
  url.searchParams.set('return_url', page.href);
  return url.href;
}

/** Capture before routing/login can replace the hash. Persist before removing
 * the fragment so reloads and instance-login screens cannot lose the ticket. */
export function captureWebInstallReturn() {
  if (!location.hash.startsWith('#openakita-install=')) return;
  const params = new URLSearchParams(location.hash.slice(1));
  const context = read();
  let restore = location.pathname + location.search;
  try {
    if (!context || context.expires <= Date.now()) throw new Error('marketplace_context_expired');
    const page = new URL(context.returnUrl);
    if (page.origin !== location.origin || page.pathname !== location.pathname || context.consumed ||
      params.getAll('state').length !== 1 || params.get('state') !== context.state ||
      params.getAll('openakita-install').length !== 1 || !/^[a-f0-9]{64}$/.test(params.get('openakita-install') || '') ||
      params.getAll('endpoint').length !== 1 || params.get('endpoint') !== context.endpoint) {
      throw new Error('marketplace_instruction_invalid');
    }
    const token = params.get('openakita-install')!;
    if (context.token && context.token !== token) throw new Error('marketplace_install_busy');
    write({ ...context, token });
    sessionStorage.removeItem(ERROR_KEY);
    restore = page.pathname + page.search + page.hash;
  } catch (error) {
    try { sessionStorage.setItem(ERROR_KEY, error instanceof Error ? error.message : 'marketplace_instruction_invalid'); }
    catch { /* Storage denial must not prevent the rest of OpenAkita booting. */ }
  } finally { history.replaceState(history.state, '', restore); }
}

export function pendingWebInstall(base: string): WebInstallContext | null {
  const error = sessionStorage.getItem(ERROR_KEY);
  if (error) { sessionStorage.removeItem(ERROR_KEY); throw new Error(error); }
  const context = read();
  if (!context || (!context.token && !context.jobId)) return null;
  if (context.base !== baseUrl(base)) throw new Error('marketplace_target_changed');
  if (!context.jobId && context.expires <= Date.now()) throw new Error('marketplace_context_expired');
  return context;
}
export function saveWebInstallJob(context: WebInstallContext, jobId: string) {
  if (read()?.state === context.state) write({ ...context, token: undefined, jobId, consumed: true });
}
export function dismissWebInstall() {
  const context = read();
  if (context) write({ ...context, token: undefined, jobId: undefined, consumed: true });
}
