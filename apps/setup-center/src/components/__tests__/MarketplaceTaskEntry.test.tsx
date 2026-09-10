import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import i18n from '../../i18n';
import { dockPosition, MarketplaceTaskEntry } from '../MarketplaceTaskEntry';
import { getInstallTasks, patchInstall, taskPhase, trackInstall } from '../../marketplace/installTasks';

beforeEach(async () => { localStorage.clear(); await i18n.changeLanguage('en'); });
afterEach(() => { cleanup(); vi.useRealTimers(); });
const job = { id: 'a', status: 'installing' as const, resource_type: 'plugin' as const,
  resource_name: 'PPT', version: '1', progress: null, permissions: [], dependencies: [] };
it('clamps remembered positions into the resized visual viewport', () => {
  const bounds = { left: 0, top: 30, width: 390, height: 340 };
  expect(dockPosition({ edge: 'right', ratio: 2 }, bounds, 180, 44)).toEqual({ x: 198, y: 306 });
  expect(dockPosition({ edge: 'left', ratio: -1 }, bounds, 180, 44)).toEqual({ x: 12, y: 86 });
});
it('opening uses a small entry while hiding it leaves the task actionable', () => {
  const task = trackInstall('https://home.example', job, undefined, true);
  const open = vi.fn();
  render(<MarketplaceTaskEntry tasks={[task]} onOpen={open} />);
  fireEvent.click(screen.getByRole('button', { name: /View installations/ }));
  expect(open).toHaveBeenCalledOnce();
  fireEvent.click(screen.getByRole('button', { name: /Hide floating/ }));
  expect(getInstallTasks()[0].hidden).toBe(true);
  expect(taskPhase(getInstallTasks()[0])).toBe('installing');
});
it('completion retires the capsule, while pending permissions persist', () => {
  vi.useFakeTimers();
  const task = trackInstall('https://home.example', { ...job, status: 'installed' }, undefined, true);
  patchInstall(task.key, { setup: 'permissions' });
  const rendered = render(<MarketplaceTaskEntry tasks={getInstallTasks()} onOpen={vi.fn()} />);
  act(() => vi.advanceTimersByTime(60_000));
  expect(screen.getByRole('button', { name: /Needs permission/ })).toBeVisible();
  patchInstall(task.key, { setup: 'ready' });
  rendered.rerender(<MarketplaceTaskEntry tasks={getInstallTasks()} onOpen={vi.fn()} />);
  act(() => vi.advanceTimersByTime(5000));
  expect(screen.queryByRole('button', { name: /View installations/ })).toBeNull();
  expect(getInstallTasks()).toHaveLength(1);
});
