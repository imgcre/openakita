import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";
import { AlertCircle, CheckCircle2, Download, PackageCheck, ShieldCheck } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle,
} from "@/components/ui/dialog";
import { getCurrentDeepLinks, IS_TAURI, IS_CAPACITOR, onDeepLinkOpen, openExternalUrl } from "../platform";
import {
  buildMarketplaceContextUrlFromDeepLink,
  hasMarketplaceClientVersion,
  marketplaceDeepLinkAction,
} from "../marketplace/navigation";

import { acceptMobileInstall, installRequest, pendingInstall, saveInstall, openMarketplace, marketplaceOpenErrorKey, type PendingInstall } from '../marketplace/mobile';

type InstallJob = {
  id: string;
  status: "ready" | "downloading" | "verifying" | "installing" | "installed" | "failed" | "cancelled";
  progress: number;
  resource_name: string;
  resource_type: "plugin" | "skill" | "mcp";
  version: string;
  permissions: string[];
  dependencies: string[];
  failure_code?: string;
  restart_required?: boolean;
};

type ParsedLink = { token: string; endpoint: string };

function parseInstallLink(value: string): ParsedLink | null {
  try {
    const url = new URL(value);
    const token = (url.searchParams.get("token") || "").toLowerCase();
    const endpoint = url.searchParams.get("endpoint") || "";
    if (url.protocol !== "openakita:" || url.hostname !== "marketplace" || url.pathname !== "/install") return null;
    if (!/^[a-f0-9]{64}$/.test(token) || !endpoint) return null;
    const source = new URL(endpoint);
    const local = source.protocol === "http:" && ["localhost", "127.0.0.1", "[::1]"].includes(source.hostname);
    if (source.protocol !== "https:" && !local) return null;
    if (source.username || source.password || source.search || source.hash || (source.pathname !== "/" && source.pathname !== "")) return null;
    return { token, endpoint: source.origin };
  } catch {
    return null;
  }
}

async function requestJson<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, { ...init, headers: { "Content-Type": "application/json", ...(init?.headers || {}) } });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const code = payload?.detail?.code || "marketplace_connection_failed";
    throw new Error(code);
  }
  return payload.data as T;
}

export function MarketplaceInstallDialog({
  apiBaseUrl,
  desktopVersion,
  onManageServers,
  onViewResource,
}: {
  apiBaseUrl: string;
  desktopVersion: string;
  onManageServers?: () => void;
  onViewResource?: (type: string) => void;
}) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const [job, setJob] = useState<InstallJob | null>(null);
  const [errorCode, setErrorCode] = useState("");
  const recentlyHandled = useRef(new Map<string, number>());
  const pendingOpenLinks = useRef(new Set<string>());
  const mobile = useRef<PendingInstall | undefined>(undefined);
  const loadingMobile = useRef(false);
  const mounted = useRef(true);
  const [acting, setActing] = useState(false);
  const [accountLabel, setAccountLabel] = useState('');
  const requestJob = useCallback(<T,>(path: string, init?: RequestInit): Promise<T> => {
    if (IS_CAPACITOR) {
      if (!mobile.current) throw new Error('marketplace_context_expired');
      return installRequest(mobile.current.target)<T>(path, init);
    }
    return requestJson<T>(apiBaseUrl + path, init);
  }, [apiBaseUrl]);

  const restoreMobile = useCallback(async (pending: PendingInstall) => {
    if (pending.dismissed || loadingMobile.current) return;
    loadingMobile.current = true;
    mobile.current = pending;
    setOpen(true); setLoading(true); setErrorCode(''); setJob(null); setAccountLabel('');
    try {
      const request = installRequest(pending.target);
      const prepared = pending.jobId
        ? await request<InstallJob>(`/api/marketplace/installs/${encodeURIComponent(pending.jobId)}`)
        : await request<InstallJob>('/api/marketplace/installs/prepare', {
          method: 'POST', body: JSON.stringify({ token: pending.token, endpoint: pending.endpoint }),
        });
      const saved = { ...pending, jobId: prepared.id, token: undefined };
      saveInstall(saved); mobile.current = saved;
      if (mounted.current) setJob(prepared);
    } catch (error) {
      if (mounted.current) setErrorCode(error instanceof Error ? error.message : 'marketplace_connection_failed');
      if (error instanceof Error && error.message === 'marketplace_account_mismatch') {
        try {
          const { readNativeAccountStatus } = await import('../platform/nativeAccountAuth');
          const account = await readNativeAccountStatus(pending.target.base);
          if (mounted.current) setAccountLabel(account.profile?.email || account.profile?.name || '');
        } catch { /* The original authorization error remains visible. */ }
      }
    } finally {
      loadingMobile.current = false;
      if (mounted.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    mounted.current = true;
    if (!IS_CAPACITOR) return () => { mounted.current = false; };
    const resume = () => {
      const pending = pendingInstall();
      if (pending && !pending.dismissed) void restoreMobile(pending);
    };
    resume();
    window.addEventListener('openakita-marketplace-resume', resume);
    return () => { mounted.current = false; window.removeEventListener('openakita-marketplace-resume', resume); };
  }, [restoreMobile]);

  const friendlyError = useCallback((code: string) => t(`marketplaceInstall.errors.${code}`, {
    defaultValue: t("marketplaceInstall.errors.marketplace_install_failed"),
  }), [t]);

  const prepare = useCallback(async (raw: string) => {
    if (!IS_TAURI) return;
    const parsed = parseInstallLink(raw);
    setOpen(true);
    setJob(null);
    setErrorCode("");
    if (!parsed) {
      setErrorCode("marketplace_instruction_invalid");
      return;
    }
    setLoading(true);
    const deadline = Date.now() + 120_000;
    while (Date.now() < deadline) {
      try {
        const prepared = await requestJson<InstallJob>(`${apiBaseUrl}/api/marketplace/installs/prepare`, {
          method: "POST", body: JSON.stringify(parsed),
        });
        setJob(prepared);
        setLoading(false);
        return;
      } catch (error) {
        const code = error instanceof Error ? error.message : "marketplace_connection_failed";
        if (code !== "marketplace_connection_failed") {
          setErrorCode(code);
          setLoading(false);
          return;
        }
        await new Promise((resolve) => window.setTimeout(resolve, 1500));
      }
    }
    setErrorCode("marketplace_connection_failed");
    setLoading(false);
  }, [apiBaseUrl]);

  const handleDeepLink = useCallback((raw: string) => {
    if (IS_CAPACITOR) {
      try {
        const pending = acceptMobileInstall(raw);
        if (pending && !pending.dismissed) {
          void import('@capacitor/browser').then(({ Browser }) => Browser.close()).catch(() => {});
          void restoreMobile(pending);
        }
      } catch (error) { toast.error(friendlyError(error instanceof Error ? error.message : 'marketplace_instruction_invalid')); }
      return;
    }
    const action = marketplaceDeepLinkAction(raw);
    if (action === "open" && !hasMarketplaceClientVersion(desktopVersion)) {
      pendingOpenLinks.current.add(raw);
      return;
    }

    const now = Date.now();
    const previous = recentlyHandled.current.get(raw) || 0;
    if (now - previous < 1_500) return;
    recentlyHandled.current.set(raw, now);
    if (recentlyHandled.current.size > 100) {
      for (const [value, timestamp] of recentlyHandled.current) {
        if (now - timestamp >= 1_500) recentlyHandled.current.delete(value);
      }
    }

    if (action === "install") {
      void prepare(raw);
      return;
    }
    if (action !== "open") return;

    const target = buildMarketplaceContextUrlFromDeepLink(raw, desktopVersion);
    if (!target) {
      toast.error(t("marketplaceInstall.openLinkInvalid"));
      return;
    }
    void openExternalUrl(target).catch(() => {
      toast.error(t("marketplaceInstall.openFailed"));
    });
  }, [desktopVersion, prepare, restoreMobile, friendlyError, t]);

  useEffect(() => {
    if (!IS_TAURI && !IS_CAPACITOR) return;
    let disposed = false;
    let cleanup = () => {};
    void getCurrentDeepLinks().then((urls) => { if (!disposed) urls.forEach(handleDeepLink); }).catch(() => {});
    void onDeepLinkOpen((urls) => urls.forEach(handleDeepLink)).then((unlisten) => {
      if (disposed) unlisten(); else cleanup = unlisten;
    }).catch(() => {});
    return () => { disposed = true; cleanup(); };
  }, [handleDeepLink]);

  useEffect(() => {
    if (!hasMarketplaceClientVersion(desktopVersion)) return;
    const pending = [...pendingOpenLinks.current];
    pendingOpenLinks.current.clear();
    pending.forEach(handleDeepLink);
  }, [desktopVersion, handleDeepLink]);

  useEffect(() => {
    if (!open || !job || ["ready", "installed", "failed", "cancelled"].includes(job.status)) return;
    let cancelled = false;
    let timer = 0;
    const poll = async () => {
      try {
        const next = await requestJob<InstallJob>(`/api/marketplace/installs/${encodeURIComponent(job.id)}`);
        if (!cancelled) setJob(next);
      } catch (error) {
        if (!cancelled) setErrorCode(error instanceof Error ? error.message : "marketplace_connection_failed");
      }
      if (!cancelled) timer = window.setTimeout(poll, 700);
    };
    timer = window.setTimeout(poll, 500);
    return () => { cancelled = true; window.clearTimeout(timer); };
  }, [requestJob, job, open]);

  async function confirm() {
    if (!job || acting) return;
    setActing(true);
    setErrorCode("");
    try {
      setJob(await requestJob<InstallJob>(`/api/marketplace/installs/${encodeURIComponent(job.id)}/confirm`, { method: "POST" }));
    } catch (error) {
      setErrorCode(error instanceof Error ? error.message : "marketplace_install_failed");
    } finally { setActing(false); }
  }

  const active = !!job && ["downloading", "verifying", "installing"].includes(job.status);

  const close = useCallback(async () => {
    if (loading || acting || (active && !IS_CAPACITOR)) return;
    const current = job;
    setOpen(false);
    if (active) return;
    if (mobile.current) saveInstall({ ...mobile.current, dismissed: true });
    if (current?.status !== "ready") return;
    try {
      await requestJob<InstallJob>(`/api/marketplace/installs/${encodeURIComponent(current.id)}/cancel`, { method: "POST" });
    } catch {
      // The local service persists the pending cancellation and retries delivery.
    }
  }, [active, requestJob, job, loading, acting]);

  const statusLabel = job ? t(`marketplaceInstall.status.${job.status}`) : "";

  return (
    <Dialog open={open} onOpenChange={(next) => { if (!next) void close(); }}>
      <DialogContent overlayClassName="z-[1100]" className="z-[1100] sm:max-w-[520px] max-h-[85dvh] overflow-y-auto" showCloseButton={(!active || IS_CAPACITOR) && !loading && !acting}>
        <DialogHeader>
          <DialogTitle className="flex items-start gap-2 pr-6 text-left">
            {job?.status === "installed" ? <CheckCircle2 className="mt-0.5 shrink-0 text-emerald-500" size={22} /> : <PackageCheck className="mt-0.5 shrink-0 text-blue-600" size={22} />}
            <span className="min-w-0 break-words">{t("marketplaceInstall.title")}</span>
          </DialogTitle>
          <DialogDescription>{job ? `${job.resource_name} · v${job.version}` : t("marketplaceInstall.connecting")}</DialogDescription>
        </DialogHeader>

        {IS_CAPACITOR && mobile.current && <div className="rounded-md border p-3 text-sm break-all">
          <div className="text-muted-foreground">{t('marketplaceInstall.target')}</div>
          <strong>{mobile.current.target.name}</strong><div>{mobile.current.target.base}</div>
          {accountLabel && <div>{accountLabel}</div>}
        </div>}
        {loading && <div className="py-8 text-center text-sm text-muted-foreground"><span className="spinner mx-auto mb-3 block" />{t("marketplaceInstall.connecting")}</div>}

        {errorCode && <div className="flex gap-3 rounded-lg border border-red-200 bg-red-50 p-4 text-sm text-red-700 dark:border-red-900 dark:bg-red-950/30 dark:text-red-300"><AlertCircle className="mt-0.5 shrink-0" size={18} /><span>{friendlyError(errorCode)}</span></div>}

        {job && !loading && <>
          <div className="grid grid-cols-2 gap-3 rounded-lg border bg-muted/30 p-4 text-sm">
            <div><span className="block text-muted-foreground">{t("marketplaceInstall.type")}</span><strong>{t(`marketplaceInstall.types.${job.resource_type}`)}</strong></div>
            <div><span className="block text-muted-foreground">{t("marketplaceInstall.statusLabel")}</span><strong>{statusLabel}</strong></div>
          </div>

          {active && <div className="space-y-2"><div className="flex justify-between text-sm"><span>{statusLabel}</span><span>{job.progress}%</span></div><div className="h-2 overflow-hidden rounded-full bg-muted"><div className="h-full rounded-full bg-blue-600 transition-[width] duration-300" style={{ width: `${job.progress}%` }} /></div></div>}

          {job.status === "ready" && <div className="space-y-3"><div className="flex items-center gap-2 text-sm font-medium"><ShieldCheck size={18} className="text-emerald-600" />{t("marketplaceInstall.permissions")}</div>{job.permissions.length ? <div className="flex flex-wrap gap-2">{job.permissions.map((permission) => <span key={permission} className="rounded-md border bg-background px-2.5 py-1 text-xs">{permission}</span>)}</div> : <p className="text-sm text-muted-foreground">{t("marketplaceInstall.noPermissions")}</p>}</div>}

          {job.status === "ready" && job.dependencies.length > 0 && <div className="space-y-3"><div className="text-sm font-medium">{t("marketplaceInstall.dependencies")}</div><div className="flex flex-wrap gap-2">{job.dependencies.map((dependency) => <span key={dependency} className="rounded-md border bg-background px-2.5 py-1 text-xs">{dependency}</span>)}</div></div>}

          {job.status === "installed" && <div className="rounded-lg border border-emerald-200 bg-emerald-50 p-4 text-sm text-emerald-700 dark:border-emerald-900 dark:bg-emerald-950/30 dark:text-emerald-300">{t(job.restart_required ? "marketplaceInstall.completedRestart" : "marketplaceInstall.completed")}</div>}
          {job.status === "failed" && <div className="rounded-lg border border-red-200 bg-red-50 p-4 text-sm text-red-700 dark:border-red-900 dark:bg-red-950/30 dark:text-red-300">{friendlyError(job.failure_code || "marketplace_install_failed")}</div>}
        </>}

        <DialogFooter>
          {IS_CAPACITOR && errorCode === 'marketplace_account_required' && !acting && <Button onClick={async () => {
            const pending = mobile.current;
            if (!pending) return;
            setActing(true);
            try {
              const { runNativeAccountLogin, readNativeAccountStatus } = await import('../platform/nativeAccountAuth');
              const { dispatchAccountStatusChanged } = await import('../utils/accountStatusEvents');
              await runNativeAccountLogin(pending.target.base);
              dispatchAccountStatusChanged(await readNativeAccountStatus(pending.target.base));
              await restoreMobile(pending);
            } catch { setErrorCode('marketplace_account_required'); }
            finally { setActing(false); }
          }}>{t('marketplaceInstall.loginAccount')}</Button>}
          {(!active || IS_CAPACITOR) && !loading && !acting && <Button variant="outline" onClick={() => void close()}>{active ? t("marketplaceInstall.background") : job?.status === "installed" ? t("common.done", "完成") : t("common.cancel")}</Button>}
          {job?.status === "ready" && <Button onClick={confirm} disabled={acting || loading || errorCode === "marketplace_target_changed"}><Download size={16} />{t("marketplaceInstall.install")}</Button>}
          {IS_CAPACITOR && errorCode && !loading && <Button variant="outline" onClick={() => { if (mobile.current) void restoreMobile(mobile.current); }}>{t('common.retry')}</Button>}
          {IS_CAPACITOR && ['marketplace_target_changed', 'marketplace_server_login_required'].includes(errorCode) && <Button onClick={onManageServers}>{t('marketplaceInstall.manageServers')}</Button>}
          {IS_CAPACITOR && ['marketplace_account_required', 'marketplace_account_mismatch', 'marketplace_instruction_unavailable', 'marketplace_install_not_found'].includes(errorCode) && <Button variant="outline" onClick={async () => {
            await close();
            try { await openMarketplace(desktopVersion); } catch (error) { toast.error(t(marketplaceOpenErrorKey(error))); }
          }}>{t('marketplaceInstall.backToMarket')}</Button>}
          {job?.status === 'installed' && onViewResource && <Button onClick={() => { void close(); onViewResource(job.resource_type); }}>{t('marketplaceInstall.viewResource')}</Button>}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
