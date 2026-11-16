import { Check, Radio, Scissors, Send } from "lucide-react";
import Link from "next/link";

import { PublicHeader } from "@/components/public-header";

const STEPS = [
  { label: "Source checked", detail: "Video or live stream detected", state: "Done" },
  { label: "Moments ranked", detail: "Speech, audio peaks and context", state: "Done" },
  { label: "Clips rendered", detail: "Captions, hook and source credit", state: "Working" },
];

export default function LandingPage() {
  return (
    <div className="min-h-dvh bg-bg">
      <PublicHeader />
      <main>
        <section className="mx-auto grid max-w-[1120px] gap-10 px-4 py-16 sm:px-6 sm:py-20 lg:grid-cols-[minmax(0,1fr)_440px] lg:items-center lg:py-24">
          <div className="max-w-2xl">
            <p className="mb-4 text-xs font-medium tracking-wide text-accent uppercase">From source link to YouTube upload</p>
            <h1 className="max-w-[640px] text-[30px] leading-[1.15] font-semibold tracking-tight text-ink sm:text-[40px]">
              Find the moments worth clipping. Finish the edit automatically.
            </h1>
            <p className="mt-5 max-w-xl text-md leading-7 text-ink-2">
              Clipper watches long videos and live streams, finds self-contained moments, adds word-by-word captions, and prepares each clip for posting.
            </p>
            <div className="mt-7 flex flex-wrap items-center gap-3">
              <Link href="/login/" className="btn btn-primary btn-lg">Continue with Google</Link>
            </div>
            <ul className="mt-7 flex flex-col gap-2 text-sm text-ink-2 sm:flex-row sm:flex-wrap sm:gap-x-6">
              {[
                "Finished videos and active streams",
                "Runs in the background",
                "Post now or schedule",
              ].map((item) => (
                <li key={item} className="flex items-center gap-2">
                  <Check aria-hidden className="size-3.5 text-ok-ink" /> {item}
                </li>
              ))}
            </ul>
          </div>

          <div className="overflow-hidden rounded-xl border border-line bg-surface shadow-lift" aria-label="Example clipping progress">
            <div className="flex items-center justify-between border-b border-line-subtle px-4 py-3">
              <div>
                <p className="text-sm font-medium">Weekly interview · 42:18</p>
                <p className="mt-0.5 text-xs text-ink-3">Five clips requested</p>
              </div>
              <span className="rounded-sm border border-accent/30 bg-accent-soft px-2 py-1 text-xs font-medium text-accent">Processing</span>
            </div>
            <ol className="divide-y divide-line-subtle">
              {STEPS.map((step, index) => (
                <li key={step.label} className="flex items-center gap-3 px-4 py-3.5">
                  <span className={`grid size-6 flex-none place-items-center rounded-full border text-xs font-semibold ${step.state === "Done" ? "border-ok/40 bg-ok-soft text-ok-ink" : "border-accent/40 bg-accent-soft text-accent"}`}>
                    {step.state === "Done" ? <Check className="size-3.5" /> : index + 1}
                  </span>
                  <div className="min-w-0 flex-1">
                    <p className="text-sm font-medium">{step.label}</p>
                    <p className="truncate text-xs text-ink-3">{step.detail}</p>
                  </div>
                  <span className="text-xs text-ink-3">{step.state}</span>
                </li>
              ))}
            </ol>
            <div className="border-t border-line-subtle bg-raised px-4 py-3 text-xs text-ink-2">
              You can close the browser. The worker keeps going.
            </div>
          </div>
        </section>

        <section className="border-y border-line-subtle bg-surface">
          <div className="mx-auto max-w-[1120px] px-4 py-14 sm:px-6">
            <div className="max-w-2xl">
              <h2 className="text-xl tracking-tight">One workflow for recorded and live video</h2>
              <p className="mt-2 text-sm leading-6 text-ink-2">The same editing rules, caption style and posting defaults apply whether the source has ended or is still live.</p>
            </div>
            <dl className="mt-8 grid border-y border-line-subtle md:grid-cols-3 md:divide-x md:divide-line-subtle">
              <div className="py-5 md:pr-6">
                <dt className="flex items-center gap-2 text-sm font-medium"><Scissors className="size-4 text-accent" /> Finished videos</dt>
                <dd className="mt-2 text-sm leading-6 text-ink-2">Transcript, audio changes and replay data produce candidates that are ranked together before rendering.</dd>
              </div>
              <div className="border-t border-line-subtle py-5 md:border-t-0 md:px-6">
                <dt className="flex items-center gap-2 text-sm font-medium"><Radio className="size-4 text-accent" /> Live streams</dt>
                <dd className="mt-2 text-sm leading-6 text-ink-2">The stream is measured in short segments. Strong moments become clips while the broadcast continues.</dd>
              </div>
              <div className="border-t border-line-subtle py-5 md:border-t-0 md:pl-6">
                <dt className="flex items-center gap-2 text-sm font-medium"><Send className="size-4 text-accent" /> YouTube posting</dt>
                <dd className="mt-2 text-sm leading-6 text-ink-2">Review the result, adjust the trim or hook, then upload immediately or choose a publication time.</dd>
              </div>
            </dl>
          </div>
        </section>

        <section className="mx-auto flex max-w-[1120px] flex-col items-start justify-between gap-5 px-4 py-14 sm:px-6 md:flex-row md:items-center">
          <div>
            <h2 className="text-lg tracking-tight">Ready to process the first source?</h2>
            <p className="mt-1 text-sm text-ink-2">Sign in with Google, then paste a YouTube link.</p>
          </div>
          <Link href="/login/" className="btn btn-primary btn-lg">Continue with Google</Link>
        </section>
      </main>
      <footer className="border-t border-line-subtle px-4 py-6 text-center text-xs text-ink-3">
        Clipper · Use footage you have permission to publish.
      </footer>
    </div>
  );
}
