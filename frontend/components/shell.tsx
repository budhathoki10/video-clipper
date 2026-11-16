"use client";

import { Clapperboard, Film, House, LogOut, Radio, Send, Settings, TrendingUp } from "lucide-react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState, type ReactNode } from "react";

import { ApiError, api, useApi, type AuthSession, type SystemStatus } from "@/lib/api";
import { Button, ErrorState, Spinner, cx, ToastProvider, useToast } from "./ui";

const NAV = [
  { href: "/dashboard/", label: "Home", icon: House },
  { href: "/clips/", label: "Clips", icon: Film },
  { href: "/live/", label: "Live", icon: Radio },
  { href: "/posts/", label: "Posts", icon: Send },
  { href: "/trends/", label: "Trends", icon: TrendingUp },
  { href: "/settings/", label: "Settings", icon: Settings },
];

const MAIN_NAV = NAV.slice(0, -1);
const SETTINGS_NAV = NAV[NAV.length - 1];

const PROVIDER_NAMES = { nvidia: "NVIDIA Nemotron", anthropic: "Claude", gemini: "Gemini", ollama: "Ollama (local)" };

function isActive(pathname: string, href: string) {
  if (href === "/") return pathname === "/";
  return pathname.startsWith(href.replace(/\/$/, "")) || (href === "/clips/" && pathname.startsWith("/clip"));
}

function EngineStatus({ session }: { session: AuthSession }) {
  const { data, error, reload } = useApi<SystemStatus>("/api/system", 5000);
  const toast = useToast();
  if (error) {
    return (
      <div className="text-xs text-bad-ink" role="status">
        Engine offline. Run <span className="font-mono">scripts\start.ps1</span>.
      </div>
    );
  }
  if (!data) return <div className="skeleton h-9 w-full" />;
  return (
    <dl className="flex flex-col gap-2 rounded-md border border-line bg-raised px-3 py-2.5 text-xs" aria-label="Engine status">
      <div className="flex items-center justify-between gap-2">
        <dt className="text-ink-3">Worker</dt>
        <dd className="flex items-center gap-1.5">
          <span aria-hidden className={cx("size-1.5 rounded-full", data.worker_alive ? "bg-ok" : "bg-bad")} />
          {data.worker_alive ? (
            "Running"
          ) : (
            <Button
              variant="ghost"
              className="!h-5 !px-1 text-xs text-bad-ink"
              onClick={async () => {
                await api("/api/system/start-worker", { method: "POST" }).catch(() => null);
                toast("Starting the background worker.");
                setTimeout(reload, 2500);
              }}
            >
              Stopped · Start
            </Button>
          )}
        </dd>
      </div>
      <div className="flex items-center justify-between gap-2">
        <dt className="text-ink-3">AI engine</dt>
        <dd className={cx("truncate", !data.ai_ready && "text-warn-ink")}>{data.ai_ready ? PROVIDER_NAMES[data.provider] : "Key missing"}</dd>
      </div>
    </dl>
  );
}

export function Shell({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const publicPage = pathname === "/" || pathname.startsWith("/login");
  const [session, setSession] = useState<AuthSession | null>(null);
  const [authError, setAuthError] = useState<string | null>(null);

  useEffect(() => {
    if (publicPage) return;
    let current = true;
    api<AuthSession>("/api/auth/me")
      .then((result) => {
        if (current) setSession(result);
      })
      .catch((error) => {
        if (!current) return;
        if (error instanceof ApiError && error.status === 401) router.replace("/login/");
        else setAuthError(error instanceof Error ? error.message : "Could not verify the account.");
      });
    return () => { current = false; };
  }, [publicPage, router]);

  if (publicPage) return <ToastProvider>{children}</ToastProvider>;
  if (authError) {
    return <main className="mx-auto max-w-lg px-4 py-16"><ErrorState message={authError} onRetry={() => window.location.reload()} /></main>;
  }
  if (!session) {
    return <div role="status" className="grid min-h-dvh place-items-center text-sm text-ink-2"><span className="flex items-center gap-2"><Spinner /> Checking your account</span></div>;
  }

  const navLink = ({ href, label, icon: Icon }: (typeof NAV)[number]) => {
    const active = isActive(pathname, href);
    return (
      <Link
        key={href}
        href={href}
        aria-current={active ? "page" : undefined}
        className={cx(
          "group flex h-9 items-center gap-3 rounded-md border px-3 text-sm transition-colors duration-100",
          active
            ? "border-accent/15 bg-accent-soft font-medium text-accent-hover"
            : "border-transparent text-ink-2 hover:bg-raised hover:text-ink",
        )}
      >
        <Icon
          aria-hidden
          className={cx(
            "size-4 transition-colors duration-100",
            active ? "text-accent" : "text-ink-3 group-hover:text-ink-2",
          )}
        />
        <span className="flex-1">{label}</span>
        {active && <span aria-hidden className="size-1.5 rounded-full bg-accent" />}
      </Link>
    );
  };

  return (
    <ToastProvider>
      <a href="#main" className="sr-only focus:not-sr-only focus:fixed focus:top-2 focus:left-2 focus:z-50 focus:rounded-md focus:bg-raised focus:px-3 focus:py-2">
        Skip to content
      </a>
      <div className="flex min-h-dvh">
        <aside className="sticky top-0 hidden h-dvh w-56 flex-none flex-col border-r border-line bg-surface md:flex">
          <div className="flex h-14 items-center gap-2.5 border-b border-line-subtle px-4">
            <span aria-hidden className="grid size-6 place-items-center rounded-md bg-accent text-accent-ink"><Clapperboard className="size-3.5" /></span>
            <span className="text-sm font-semibold tracking-tight text-ink">Clipper</span>
          </div>
          <nav aria-label="Main" className="flex flex-1 flex-col px-2 py-3">
            <div className="flex flex-col gap-1">{MAIN_NAV.map(navLink)}</div>
            <div className="mt-auto flex flex-col gap-1 border-t border-line-subtle pt-2">
              {navLink(SETTINGS_NAV)}
            </div>
          </nav>
          <div className="flex flex-col gap-2 border-t border-line-subtle p-2">
            <EngineStatus session={session} />
            <div className="flex items-center gap-2 px-1 py-1">
              <div className="min-w-0 flex-1">
                <p className="truncate text-xs font-medium text-ink">{session.user.name}</p>
                <p className="truncate text-xs text-ink-3">{session.user.email}</p>
              </div>
              <Button
                variant="ghost"
                size="icon"
                aria-label="Sign out"
                onClick={async () => {
                  await api("/api/auth/logout", { method: "POST" }).catch(() => null);
                  router.replace("/");
                }}
              >
                <LogOut />
              </Button>
            </div>
          </div>
        </aside>

        <div className="flex min-w-0 flex-1 flex-col">
          <main id="main" className="mx-auto w-full max-w-[1200px] flex-1 px-4 pt-6 pb-24 sm:px-6 md:pb-10 lg:px-8">
            {children}
          </main>
        </div>

        <nav aria-label="Main" className="fixed inset-x-0 bottom-0 z-40 grid grid-cols-6 border-t border-line bg-surface shadow-soft pb-[env(safe-area-inset-bottom)] md:hidden">
          {NAV.map(({ href, label, icon: Icon }) => {
            const active = isActive(pathname, href);
            return (
              <Link key={href} href={href} aria-current={active ? "page" : undefined} className={cx("flex h-14 flex-col items-center justify-center gap-1 text-[11px]", active ? "font-medium text-accent-hover" : "text-ink-3")}>
                <Icon aria-hidden className={cx("size-[18px]", active && "text-accent")} />
                {label}
              </Link>
            );
          })}
        </nav>
      </div>
    </ToastProvider>
  );
}
