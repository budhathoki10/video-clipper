"use client";

import { Radio } from "lucide-react";
import Link from "next/link";
import { useState } from "react";

import { CLIP_GRID, ClipCard, ClipGridSkeleton } from "@/components/clip-card";
import { JobRow } from "@/components/job-row";
import { Pagination } from "@/components/pagination";
import { EmptyState, ErrorState, PageHeader, SkeletonRows } from "@/components/ui";
import { formatDuration, type ClipSummary, type Job, type Page } from "@/lib/api";
import { withoutHidden } from "@/lib/optimistic";
import { usePagedApi } from "@/lib/pagination";

/** Indexes of bars that belong to a run of at least `sustain` bars in a row above the threshold. */
function sustainedBars(scores: number[], threshold: number, sustain: number): Set<number> {
  const marked = new Set<number>();
  let start = -1;
  scores.forEach((score, i) => {
    if (score >= threshold) {
      if (start < 0) start = i;
      if (i - start + 1 >= sustain) for (let j = start; j <= i; j++) marked.add(j);
    } else {
      start = -1;
    }
  });
  return marked;
}

/** Excitement per recorded segment, with the trigger line. Only a run of `sustain` bars in a row
 * above the line starts a clip; a single spike is shown lighter. */
function Activity({ scores, threshold, sustain }: { scores: number[]; threshold: number; sustain: number }) {
  const runs = sustainedBars(scores, threshold, sustain);
  const W = 600;
  const H = 72;
  const max = Math.max(threshold * 1.6, ...scores, 1);
  const min = Math.min(0, ...scores);
  const y = (v: number) => H - ((v - min) / (max - min)) * H;
  const slot = W / 60;
  const offset = 60 - scores.length;
  return (
    <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" className="h-[72px] w-full" role="img" aria-label={`Excitement over the last ${scores.length} segments. ${runs.size} were part of a run long enough to start a clip.`}>
      {scores.map((score, i) => {
        const top = y(Math.max(score, min));
        return <rect key={i} x={(i + offset) * slot + 1} y={top} width={slot - 2} height={Math.max(H - top, 1)} rx={1} className={runs.has(i) ? "fill-accent" : score >= threshold ? "fill-accent/35" : "fill-line-strong"} />;
      })}
      <line x1={0} x2={W} y1={y(threshold)} y2={y(threshold)} className="stroke-ink-3" strokeWidth={1} strokeDasharray="3 4" vectorEffect="non-scaling-stroke" />
    </svg>
  );
}

function LiveSession({ job, onChange }: { job: Job; onChange: () => void }) {
  const [page, setPage] = useState(1);
  const clips = usePagedApi<Page<ClipSummary>>("/api/clips", page, 6, { job_id: job.id }, 4000);
  const clipPage = clips.data ?? clips.previousData;
  const scores = job.payload?.scores ?? [];
  const running = job.status === "running";
  return (
    <article className="panel">
      <ul>
        <JobRow job={job} onChange={onChange} />
      </ul>
      <div className="border-t border-line-subtle px-4 py-3">
        <dl className="mb-3 flex flex-wrap gap-x-8 gap-y-2">
          <div>
            <dt className="text-xs text-ink-3">Watched</dt>
            <dd className="num text-md font-semibold">{formatDuration(job.payload?.watched_sec ?? 0)}</dd>
          </div>
          <div>
            <dt className="text-xs text-ink-3">Clips made</dt>
            <dd className="num text-md font-semibold">{clipPage?.total ?? job.clip_count ?? 0}</dd>
          </div>
          <div>
            <dt className="text-xs text-ink-3">Clip threshold</dt>
            <dd className="num text-md font-semibold">{job.payload?.threshold ?? "–"}</dd>
          </div>
        </dl>
        {scores.length > 0 ? (
          <>
            <Activity scores={scores} threshold={job.payload?.threshold ?? 2.2} sustain={job.payload?.sustain ?? 1} />
            <p className="mt-1.5 text-xs text-ink-3">
              Excitement in the last {scores.length} segments (loudness, speaking speed, hype phrases).{" "}
              {(job.payload?.sustain ?? 1) > 1
                ? `${job.payload?.sustain} bars in a row above the dashed line start a clip; a single spike (lighter) does not.`
                : "Bars above the dashed line start a clip."}
            </p>
          </>
        ) : (
          running && <p className="text-sm text-ink-2">Recording the first segments. The activity graph appears within a minute.</p>
        )}
        {job.payload?.last_error && <p className="mt-2 text-xs text-warn-ink">Last clip attempt failed: {job.payload.last_error}</p>}
      </div>
      {(clips.error || clips.loading || !!clipPage?.total) && (
        <div id={`stream-clips-${job.id}`} className="scroll-mt-16 border-t border-line-subtle p-4 md:scroll-mt-4">
          {clips.error ? <ErrorState message={clips.error} onRetry={clips.reload} /> : clips.loading ? <ClipGridSkeleton /> : (
            <div className={CLIP_GRID}>
              {withoutHidden(clips.data?.items ?? []).map((clip) => (
                <ClipCard key={clip.id} clip={clip} />
              ))}
            </div>
          )}
          <Pagination data={clips.error ? null : clipPage} label={`Clips for ${job.title || "this stream"}`}
            onPageChange={setPage} loading={clips.loading} className="mt-3" anchorId={`stream-clips-${job.id}`} />
        </div>
      )}
    </article>
  );
}

export default function LivePage() {
  const [page, setPage] = useState(1);
  const jobs = usePagedApi<Page<Job>>("/api/jobs", page, 3, { kind: "live" }, 2500);

  return (
    <div id="stream-list" className="scroll-mt-16 md:scroll-mt-4">
      <PageHeader title="Live" description="Streams being watched. Clips are cut while the stream is still running, about one to two minutes after the moment." />
      {jobs.error ? (
        <ErrorState message={jobs.error} onRetry={jobs.reload} />
      ) : jobs.loading && !jobs.data ? (
        <SkeletonRows rows={2} height="h-40" />
      ) : !jobs.data?.items.length ? (
        <EmptyState
          icon={<Radio />}
          title="No streams are being watched"
          body="Paste the link of a stream that is live right now on the Home page. It is watched until it ends, even with this page closed."
          action={
            <Link href="/dashboard/" className="btn btn-primary">
              Add a live stream
            </Link>
          }
        />
      ) : (
        <div className="flex flex-col gap-4">
          {jobs.data.items.map((job) => (
            <LiveSession key={job.id} job={job} onChange={() => void jobs.reload()} />
          ))}
        </div>
      )}
      <Pagination data={jobs.data ?? (jobs.loading ? jobs.previousData : null)} label="Live streams" onPageChange={setPage} loading={jobs.loading} className="mt-4" anchorId="stream-list" />
    </div>
  );
}
