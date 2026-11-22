"use client";

import { ChevronDown, Film, Link2, Upload } from "lucide-react";
import Link from "next/link";
import { useEffect, useRef, useState, type FormEvent } from "react";

import { CLIP_GRID, ClipCard, ClipGridSkeleton } from "@/components/clip-card";
import { ClipCountSelect, clipCountLabel } from "@/components/clip-count";
import { LayoutPicker } from "@/components/layout-options";
import { JobRow } from "@/components/job-row";
import { Pagination } from "@/components/pagination";
import { Button, EmptyState, ErrorState, Field, Notice, SkeletonRows, cx, useToast } from "@/components/ui";
import { api, useApi, type ClipSummary, type Job, type Page, type Settings, type SecretStatus, type SystemStatus } from "@/lib/api";
import { withoutHidden } from "@/lib/optimistic";
import { usePagedApi } from "@/lib/pagination";

type Overrides = { max_clips: number; min_sec: number; max_sec: number; aspect: Settings["format"]["aspect"]; quality: number; layout: Settings["format"]["layout"] };

export default function HomePage() {
  const toast = useToast();
  const [jobPage, setJobPage] = useState(1);
  const [clipPage, setClipPage] = useState(1);
  const jobs = usePagedApi<Page<Job>>("/api/jobs", jobPage, 8, {}, 2000);
  const clips = usePagedApi<Page<ClipSummary>>("/api/clips", clipPage, 6, { ready: "true", sort: "score" }, 5000);
  const settings = useApi<{ settings: Settings; secrets: SecretStatus }>("/api/settings");
  const system = useApi<SystemStatus>("/api/system", 10000);
  const input = useRef<HTMLInputElement>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const [uploading, setUploading] = useState<{ name: string; percent: number } | null>(null);
  const [url, setUrl] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [showOptions, setShowOptions] = useState(false);
  const [overrides, setOverrides] = useState<Overrides | null>(null);

  useEffect(() => {
    const s = settings.data?.settings;
    if (s && !overrides) {
      setOverrides({ max_clips: s.clip.max_clips, min_sec: s.clip.min_sec, max_sec: s.clip.max_sec, aspect: s.format.aspect, quality: s.format.quality, layout: s.format.layout });
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

  function jobOptions() {
    return overrides
      ? { clip: { max_clips: overrides.max_clips, min_sec: overrides.min_sec, max_sec: overrides.max_sec }, format: { aspect: overrides.aspect, quality: overrides.quality, layout: overrides.layout } }
      : {};
  }

  // A video file is sent in parts of a few MB, in order, so no single request is large or slow.
  async function uploadFile(file: File) {
    setError(null);
    setUploading({ name: file.name, percent: 0 });
    try {
      const started = await api<{ id: string; chunk_size: number }>("/api/uploads", { method: "POST", json: { name: file.name, size: file.size } });
      for (let offset = 0; offset < file.size; offset += started.chunk_size) {
        const part = file.slice(offset, offset + started.chunk_size);
        for (let attempt = 1; ; attempt++) {
          try {
            await api(`/api/uploads/${started.id}/chunk?offset=${offset}`, { method: "PUT", body: part, headers: { "Content-Type": "application/octet-stream" } });
            break;
          } catch (err) {
            if (attempt >= 3) throw err;
            await new Promise((resolve) => setTimeout(resolve, 1000 * attempt));
          }
        }
        setUploading({ name: file.name, percent: Math.min(100, Math.round(((offset + part.size) / file.size) * 100)) });
      }
      await api(`/api/uploads/${started.id}/complete`, { method: "POST", json: { options: jobOptions() } });
      toast("Uploaded. It keeps working if you close this page.");
      if (jobPage === 1) void jobs.reload();
      else setJobPage(1);
    } catch (err) {
      setError(err instanceof Error ? err.message : "The upload did not finish. Try again.");
    } finally {
      setUploading(null);
      if (fileInput.current) fileInput.current.value = "";
    }
  }

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
      await api("/api/jobs", { method: "POST", json: { url: link, options: jobOptions() } });
      setUrl("");
      toast("Added. It keeps working if you close this page.");
      if (jobPage === 1) void jobs.reload();
      else setJobPage(1);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not add the link.");
    } finally {
      setSubmitting(false);
    }
  }

  const visibleJobs = withoutHidden(jobs.data?.items ?? []);
  const topClips = withoutHidden(clips.data?.items ?? []);
  const set = <K extends keyof Overrides>(key: K, value: Overrides[K]) => setOverrides((o) => (o ? { ...o, [key]: value } : o));

  return (
    <div className="flex flex-col gap-8">
      <section aria-labelledby="new-heading">
        <h1 id="new-heading" className="text-xl tracking-tight">
          Clip a video or live stream
        </h1>
        <p className="mt-1 text-sm text-ink-2">
          Paste a YouTube link or upload a video file. A finished video is cut into its strongest moments; a stream that is live right now is watched until it ends.
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

          <div className="mt-3 flex flex-wrap items-center gap-3 text-xs text-ink-2">
            <input
              ref={fileInput}
              type="file"
              accept="video/*,.mkv,.mov,.m4v,.avi"
              className="sr-only"
              aria-label="Video file"
              tabIndex={-1}
              onChange={(e) => {
                const file = e.target.files?.[0];
                if (file) void uploadFile(file);
              }}
            />
            <Button type="button" variant="secondary" loading={uploading !== null} onClick={() => fileInput.current?.click()}>
              <Upload aria-hidden /> Upload a video file
            </Button>
            {uploading ? (
              <span role="status" className="flex min-w-0 items-center gap-2">
                <span className="truncate">{uploading.name}</span>
                <span className="num font-medium text-ink">{uploading.percent}%</span>
              </span>
            ) : (
              <span className="text-ink-3">mp4, mov, mkv, webm or avi. Works when a link cannot be downloaded.</span>
            )}
          </div>

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
                · {clipCountLabel(overrides.max_clips)} · {overrides.min_sec}–{overrides.max_sec} s · {overrides.aspect} · {overrides.quality}p
              </span>
            )}
          </button>
          {showOptions && overrides && (
            <div id="job-options" className="animate-enter mt-3 grid grid-cols-2 gap-3 border-t border-line-subtle pt-3 md:grid-cols-5">
              <Field label="Clips to make">
                {(p) => <ClipCountSelect {...p} value={overrides.max_clips} onChange={(count) => set("max_clips", count)} />}
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
              <div className="col-span-2 flex flex-col gap-1.5 md:col-span-5">
                <span className="text-xs font-medium text-ink-2">Video style</span>
                <LayoutPicker value={overrides.layout} onChange={(layout) => set("layout", layout)} link={url} />
              </div>
            </div>
          )}
        </form>
      </section>

      <section aria-labelledby="jobs-heading" className="scroll-mt-16 md:scroll-mt-4" id="videos-list">
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
          <div className="panel">
            <ul className="divide-y divide-line-subtle">
              {visibleJobs.map((job) => (
                <JobRow key={job.id} job={job} onChange={() => void jobs.reload()} />
              ))}
            </ul>
          </div>
        )}
        <Pagination data={jobs.data ?? (jobs.loading ? jobs.previousData : null)} label="Videos" onPageChange={setJobPage} loading={jobs.loading} className="mt-3" anchorId="videos-list" />
      </section>

      <section aria-labelledby="top-heading" className="scroll-mt-16 md:scroll-mt-4" id="ranked-clips">
        <div className="mb-3 flex items-center justify-between">
          <h2 id="top-heading" className="text-sm font-medium">
            Highest-scoring clips
          </h2>
          {(clips.data ?? clips.previousData)?.total ? (
            <Link href="/clips/" className="text-xs text-ink-2 underline-offset-2 hover:text-ink hover:underline">
              All clips
            </Link>
          ) : null}
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
        <Pagination data={clips.data ?? (clips.loading ? clips.previousData : null)} label="Highest-scoring clips" onPageChange={setClipPage} loading={clips.loading} className="mt-3" anchorId="ranked-clips" />
      </section>
    </div>
  );
}
