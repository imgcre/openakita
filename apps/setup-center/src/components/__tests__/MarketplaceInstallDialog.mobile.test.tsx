import { fireEvent, render, screen, waitFor, cleanup } from '@testing-library/react';
import { beforeEach, afterEach, expect, it, vi } from 'vitest';
import i18n from '../../i18n';
const mocks = vi.hoisted(() => ({ server: { id: 'home', name: 'Home server', url: 'https://home.example' } }));
vi.mock('../../platform/detect', () => ({ IS_CAPACITOR: true }));
vi.mock('../../platform/auth', () => ({ getAccessToken: () => 'home-credential' }));
vi.mock('../../platform/servers', () => ({ getActiveServer: () => mocks.server }));
vi.mock('../../platform', () => ({ IS_CAPACITOR: true, IS_TAURI: false,
  getCurrentDeepLinks: async () => [], onDeepLinkOpen: async () => () => {}, openExternalUrl: vi.fn() }));
import { MarketplaceInstallDialog } from '../MarketplaceInstallDialog';
import { saveInstall, pendingInstall } from '../../marketplace/mobile';
const baseJob = { id: 'job', status: 'ready', resource_name: 'Test Skill', resource_type: 'skill', version: '1.0.0', permissions: ['network'], dependencies: [], progress: 0 };
beforeEach(async () => {
  localStorage.clear(); mocks.server = { id: 'home', name: 'Home server', url: 'https://home.example' };
  await i18n.changeLanguage('en');
  saveInstall({ key: 'install-one', target: { id: 'home', name: 'Home server', base: 'https://home.example', state: 'state', expires: Date.now() + 10000 }, endpoint: 'https://marketplace.openakita.cn', token: 'a'.repeat(64) });
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
const props = { apiBaseUrl: 'https://home.example', desktopVersion: '1.27.40' };
it('shows the target and permissions, persists the job and resumes after remount without reinstalling', async () => {
  let status = 'ready';
  const calls: string[] = [];
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    calls.push(url);
    if (url.endsWith('/confirm')) status = 'downloading';
    return new Response(JSON.stringify({ data: { ...baseJob, status } }));
  }));
  const first = render(<MarketplaceInstallDialog {...props} />);
  await screen.findByText('Test Skill · v1.0.0');
  expect(screen.getByRole('dialog')).toHaveTextContent('Home server');
  expect(screen.getByRole('dialog')).toHaveTextContent('network');
  expect(pendingInstall()?.token).toBeUndefined();
  fireEvent.click(screen.getByRole('button', { name: i18n.t('marketplaceInstall.install') }));
  await screen.findByRole('button', { name: i18n.t('marketplaceInstall.background') });
  first.unmount();
  status = 'installed';
  render(<MarketplaceInstallDialog {...props} onViewResource={vi.fn()} />);
  await screen.findByRole('button', { name: i18n.t('marketplaceInstall.viewResource') });
  expect(calls.filter(url => url.endsWith('/prepare'))).toHaveLength(1);
  expect(calls.filter(url => url.endsWith('/confirm'))).toHaveLength(1);
});
it('does not confirm on a different server and offers target connection recovery', async () => {
  const fetcher = vi.fn(async () => new Response(JSON.stringify({ data: baseJob })));
  vi.stubGlobal('fetch', fetcher);
  const manage = vi.fn();
  render(<MarketplaceInstallDialog {...props} onManageServers={manage} />);
  await screen.findByText('Test Skill · v1.0.0');
  mocks.server = { id: 'office', name: 'Office', url: 'https://office.example' };
  fireEvent.click(screen.getByRole('button', { name: i18n.t('marketplaceInstall.install') }));
  await screen.findByText(i18n.t('marketplaceInstall.errors.marketplace_target_changed'));
  expect(fetcher).toHaveBeenCalledTimes(1);
  fireEvent.click(screen.getByRole('button', { name: i18n.t('marketplaceInstall.manageServers') }));
  expect(manage).toHaveBeenCalledOnce();
});
it('keeps authorization failures actionable and retries the same instruction', async () => {
  let attempts = 0;
  const fetcher = vi.fn(async (url: string) => {
    if (url.endsWith('/api/account/status')) return new Response(JSON.stringify({ profile: {email:'owner@example.test'} }));
    return ++attempts === 1 ? new Response(JSON.stringify({ detail: { code: 'marketplace_account_mismatch' } }), { status: 400 })
      : new Response(JSON.stringify({ data: baseJob }));
  });
  vi.stubGlobal('fetch', fetcher);
  render(<MarketplaceInstallDialog {...props} />);
  await screen.findByText(i18n.t('marketplaceInstall.errors.marketplace_account_mismatch'));
  expect(screen.queryByRole('button', { name: i18n.t('marketplaceInstall.install') })).toBeNull();
  await screen.findByText('owner@example.test');
  fireEvent.click(screen.getByRole('button', { name: i18n.t('common.retry') }));
  await waitFor(() => expect(screen.getByText('Test Skill · v1.0.0')).toBeVisible());
  expect(attempts).toBe(2);
});
