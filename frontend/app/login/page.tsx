"use client";

import { Suspense } from "react";
import { useSearchParams } from "next/navigation";

import { PublicHeader } from "@/components/public-header";
import { API_URL, useApi } from "@/lib/api";
import { Notice, SkeletonRows } from "@/components/ui";

function GoogleMark() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden className="size-4">
      <path fill="#4285F4" d="M21.6 12.2c0-.7-.1-1.4-.2-2H12v3.9h5.4a4.6 4.6 0 0 1-2 3v2.5h3.2c1.9-1.8 3-4.3 3-7.4Z" />
      <path fill="#34A853" d="M12 22c2.7 0 5-.9 6.6-2.4l-3.2-2.5c-.9.6-2 1-3.4 1-2.6 0-4.8-1.8-5.6-4.1H3.1v2.6A10 10 0 0 0 12 22Z" />
      <path fill="#FBBC05" d="M6.4 14a6 6 0 0 1 0-4V7.4H3.1a10 10 0 0 0 0 9.2L6.4 14Z" />
      <path fill="#EA4335" d="M12 5.9c1.5 0 2.8.5 3.8 1.5l2.9-2.8A9.6 9.6 0 0 0 3.1 7.4L6.4 10c.8-2.4 3-4.1 5.6-4.1Z" />
    </svg>
  );
}

function LoginContent() {
  const params = useSearchParams();
  const status = useApi<{ configured: boolean }>("/api/auth/google/status");
  const error = params.get("error");

  return (
    <div className="min-h-dvh bg-bg">
      <PublicHeader />
      <main className="mx-auto grid min-h-[calc(100dvh-3.5rem)] max-w-[960px] content-center gap-10 px-4 py-12 sm:px-6 md:grid-cols-[minmax(0,1fr)_400px] md:items-center md:py-20">
        <div className="max-w-md">
          <p className="text-xs font-medium tracking-wide text-accent uppercase">Continue processing</p>
          <h1 className="mt-3 text-xl tracking-tight">Sign in to Clipper</h1>
          <p className="mt-3 text-sm leading-6 text-ink-2">Choose your Google account to return to videos, live watches, rendered clips and scheduled posts.</p>
        </div>
        <section className="rounded-xl border border-line bg-surface p-5 shadow-lift sm:p-6" aria-label="Google sign in">
          {status.loading && !status.data ? (
            <SkeletonRows rows={2} />
          ) : (
            <div className="flex flex-col gap-4">
              {error && <Notice tone="bad">{error}</Notice>}
              {status.error && <Notice tone="bad">{status.error}</Notice>}
              {status.data?.configured ? (
                <a href={`${API_URL}/api/auth/google`} className="btn btn-primary btn-lg w-full">
                  <GoogleMark /> Continue with Google
                </a>
              ) : (
                <button type="button" className="btn btn-primary btn-lg w-full" disabled>
                  <GoogleMark /> Continue with Google
                </button>
              )}
              {!status.data?.configured && !status.loading && (
                <p className="text-sm leading-6 text-ink-2">Google sign-in is not configured. The server owner must add the Google Web application client values.</p>
              )}
              <p className="text-xs leading-5 text-ink-3">Your videos, clips and keys are private to your account.</p>
            </div>
          )}
        </section>
      </main>
    </div>
  );
}

export default function LoginPage() {
  return <Suspense fallback={<div className="min-h-dvh bg-bg" />}><LoginContent /></Suspense>;
}
