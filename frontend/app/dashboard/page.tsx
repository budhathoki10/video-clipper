"use client";

import { ChevronDown, Film, Link2 } from "lucide-react";
import Link from "next/link";
import { useEffect, useMemo, useRef, useState, type FormEvent } from "react";

import { CLIP_GRID, ClipCard, ClipGridSkeleton } from "@/components/clip-card";
import { JobRow } from "@/components/job-row";
import { Button, EmptyState, ErrorState, Field, Notice, SkeletonRows, cx, useToast } from "@/components/ui";
import { api, clipScore, useApi, type Clip, type Job, type Settings, type SecretStatus, type SystemStatus } from "@/lib/api";

type Overrides = { max_clips: number; min_sec: number; max_sec: number; aspect: Settings["format"]["aspect"]; quality: number };

export default function HomePage() {
  const toast = useToast();
  const jobs = useApi<Job[]>("/api/jobs", 2000);
  const clips = useApi<Clip[]>("/api/clips", 5000);
  const settings = useApi<{ settings: Settings; secrets: SecretStatus }>("/api/settings");
  const system = useApi<SystemStatus>("/api/system", 10000);
  const input = useRef<HTMLInputElement>(null);
  const [url, setUrl] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [showOptions, setShowOptions] = useState(false);
  const [overrides, setOverrides] = useState<Overrides | null>(null);

  useEffect(() => {
    const s = settings.data?.settings;
    if (s && !overrides) {
      setOverrides({ max_clips: s.clip.max_clips, min_sec: s.clip.min_sec, max_sec: s.clip.max_sec, aspect: s.format.aspect, quality: s.format.quality });
    }
  }, [settings.data, overrides]);

  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      const target = event.target as HTMLElement;
      if (event.key === "/" && !["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName)) {
        event.preventDefault();
        input.current?.focus();
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  async function submit(event: FormEvent) {
    event.preventDefault();
    const link = url.trim();
    if (!/^https?:\/\/\S+$/.test(link)) {
      setError("Paste a full link that starts with https://");
      input.current?.focus();
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      const options = overrides
        ? { clip: { max_clips: overrides.max_clips, min_sec: overrides.min_sec, max_sec: overrides.max_sec }, format: { aspect: overrides.aspect, quality: overrides.quality } }
        : {};
      await api("/api/jobs", { method: "POST", json: { url: link, options } });
      setUrl("");
      toast("Added. It keeps working if you close this page.");
      void jobs.reload();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not add the link.");
    } finally {
      setSubmitting(false);
    }
  }

  const visibleJobs = useMemo(() => (jobs.data ?? []).slice(0, 8), [jobs.data]);
  const topClips = useMemo(
    () =>
      (clips.data ?? [])
        .filter((c) => c.status === "ready")
        .sort((a, b) => (clipScore(b) ?? -1) - (clipScore(a) ?? -1))
        .slice(0, 6),
    [clips.data],
  );
  const set = <K extends keyof Overrides>(key: K, value: Overrides[K]) => setOverrides((o) => (o ? { ...o, [key]: value } : o));

  return (
    <div className="flex flex-col gap-8">
      <section aria-labelledby="new-heading">
        <h1 id="new-heading" className="text-xl tracking-tight">
          Clip a video or live stream
        </h1>
        <p className="mt-1 text-sm text-ink-2">
          Paste a YouTube link. A finished video is cut into its strongest moments; a stream that is live right now is watched until it ends.
        </p>

        {system.data && !system.data.ai_ready && (
          <div className="mt-4">
            <Notice>
              No API key is set for the selected AI engine, so clips will be picked from audio peaks only.{" "}
              <Link href="/settings/" className="underline underline-offset-2">
                Add a key in Settings
              </Link>
              .
            </Notice>
          </div>
        )}

        <form onSubmit={submit} noValidate className="mt-4 panel p-3">
          <div className="flex flex-col gap-2 sm:flex-row">
            <div className="relative flex-1">
              <Link2 aria-hidden className="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-ink-3" />
              <input
                ref={input}
                type="url"
                inputMode="url"
                autoComplete="off"
                spellCheck={false}
                aria-label="YouTube link"
                aria-invalid={error ? true : undefined}
                aria-describedby={error ? "url-error" : undefined}
                placeholder="https://www.youtube.com/watch?v=…"
                value={url}
                onChange={(e) => {
                  setUrl(e.target.value);
                  setError(null);
                }}
                className="field field-lg pr-10 pl-9"
              />
              <span aria-hidden className="kbd absolute top-1/2 right-3 hidden -translate-y-1/2 sm:inline-flex">
                /
              </span>
            </div>
            <Button type="submit" variant="primary" size="lg" loading={submitting}>
              Create clips
            </Button>
          </div>
          {error && (
            <p id="url-error" role="alert" className="mt-2 text-xs text-bad-ink">
              {error}
            </p>
          )}

          <button
            type="button"
            aria-expanded={showOptions}
            aria-controls="job-options"
            onClick={() => setShowOptions((v) => !v)}
            className="mt-3 flex items-center gap-1 rounded-sm text-xs text-ink-2 hover:text-ink"
          >
            <ChevronDown aria-hidden className={cx("size-3.5 transition-transform duration-150", showOptions && "rotate-180")} />
            Options for this video
            {overrides && !showOptions && (
              <span className="hidden text-ink-3 sm:inline">
                · up to {overrides.max_clips} clips · {overrides.min_sec}–{overrides.max_sec} s · {overrides.aspect} · {overrides.quality}p
              </span>
            )}
          </button>
          {showOptions && overrides && (
            <div id="job-options" className="animate-enter mt-3 grid grid-cols-2 gap-3 border-t border-line-subtle pt-3 md:grid-cols-5">
              <Field label="Clips to make">
                {(p) => <input {...p} type="number" min={1} max={20} className="field num" value={overrides.max_clips} onChange={(e) => set("max_clips", Number(e.target.value) || 1)} />}
              </Field>
              <Field label="Shortest (seconds)">
                {(p) => <input {...p} type="number" min={5} max={180} className="field num" value={overrides.min_sec} onChange={(e) => set("min_sec", Number(e.target.value) || 5)} />}
              </Field>
              <Field label="Longest (seconds)">
                {(p) => <input {...p} type="number" min={5} max={180} className="field num" value={overrides.max_sec} onChange={(e) => set("max_sec", Number(e.target.value) || 60)} />}
              </Field>
              <Field label="Screen format">
                {(p) => (
                  <select {...p} className="field" value={overrides.aspect} onChange={(e) => set("aspect", e.target.value as Overrides["aspect"])}>
                    <option value="9:16">9:16 Shorts</option>
                    <option value="16:9">16:9 Video</option>
                    <option value="1:1">1:1 Square</option>
                  </select>
                )}
              </Field>
              <Field label="Quality">
                {(p) => (
                  <select {...p} className="field" value={overrides.quality} onChange={(e) => set("quality", Number(e.target.value))}>
                    <option value={720}>720p</option>
                    <option value={1080}>1080p</option>
                    <option value={1440}>1440p</option>
                    <option value={2160}>4K</option>
                  </select>
                )}
              </Field>
            </div>
          )}
        </form>
      </section>

      <section aria-labelledby="jobs-heading">
        <div className="mb-3 flex items-center justify-between">
          <h2 id="jobs-heading" className="text-sm font-medium">
            Videos
          </h2>
        </div>
        {jobs.error ? (
          <ErrorState message={jobs.error} onRetry={jobs.reload} />
        ) : jobs.loading && !jobs.data ? (
          <SkeletonRows rows={3} height="h-16" />
        ) : visibleJobs.length === 0 ? (
          <EmptyState icon={<Link2 />} title="No videos yet" body="Paste a YouTube link above. Clips appear here as they finish, and work continues in the background if you close this page." />
        ) : (
          <ul className="panel divide-y divide-line-subtle">
            {visibleJobs.map((job) => (
              <JobRow key={job.id} job={job} onChange={() => void jobs.reload()} />
            ))}
          </ul>
        )}
      </section>

      <section aria-labelledby="top-heading">
        <div className="mb-3 flex items-center justify-between">
          <h2 id="top-heading" className="text-sm font-medium">
            Highest-scoring clips
          </h2>
          {topClips.length > 0 && (
            <Link href="/clips/" className="text-xs text-ink-2 underline-offset-2 hover:text-ink hover:underline">
              All clips
            </Link>
          )}
        </div>
        {clips.error ? (
          <ErrorState message={clips.error} onRetry={clips.reload} />
        ) : clips.loading && !clips.data ? (
          <ClipGridSkeleton />
        ) : topClips.length === 0 ? (
          <EmptyState icon={<Film />} title="No clips yet" body="Finished clips are ranked here by their audience panel score." />
        ) : (
          <div className={CLIP_GRID}>
            {topClips.map((clip) => (
              <ClipCard key={clip.id} clip={clip} />
            ))}
          </div>
        )}
      </section>
    </div>
  );
}
