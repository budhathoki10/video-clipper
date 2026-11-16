"use client";

import { Film, Search, X } from "lucide-react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useMemo, useState } from "react";

import { CLIP_GRID, ClipCard, ClipGridSkeleton } from "@/components/clip-card";
import { Button, EmptyState, ErrorState, PageHeader } from "@/components/ui";
import { clipScore, useApi, type Clip, type Job } from "@/lib/api";

const PAGE_SIZE = 24;
type Sort = "score" | "newest" | "shortest" | "longest";

const SORTERS: Record<Sort, (a: Clip, b: Clip) => number> = {
  score: (a, b) => (clipScore(b) ?? -1) - (clipScore(a) ?? -1),
  newest: (a, b) => b.created_at - a.created_at,
  shortest: (a, b) => a.duration - b.duration,
  longest: (a, b) => b.duration - a.duration,
};

function Library() {
  const router = useRouter();
  const jobId = useSearchParams().get("job");
  const clips = useApi<Clip[]>(jobId ? `/api/clips?job_id=${jobId}` : "/api/clips", 4000);
  const jobs = useApi<Job[]>("/api/jobs");
  const [query, setQuery] = useState("");
  const [sort, setSort] = useState<Sort>("score");
  const [minScore, setMinScore] = useState(0);
  const [shown, setShown] = useState(PAGE_SIZE);

  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return (clips.data ?? [])
      .filter((c) => !needle || `${c.title} ${c.source_channel ?? ""} ${c.source_title ?? ""}`.toLowerCase().includes(needle))
      .filter((c) => minScore === 0 || (clipScore(c) ?? 0) >= minScore)
      .sort(SORTERS[sort]);
  }, [clips.data, query, sort, minScore]);

  const source = jobs.data?.find((j) => j.id === jobId);
  const filtering = query !== "" || minScore > 0;
  const total = clips.data?.length ?? 0;

  return (
    <>
      <PageHeader
        title="Clips"
        description={
          clips.data ? `${filtered.length} of ${total} ${total === 1 ? "clip" : "clips"}${source ? ` from “${source.title}”` : ""}` : "Every clip you have made."
        }
      />

      <div className="mb-4 grid grid-cols-2 gap-2 md:flex md:flex-wrap md:items-center">
        <div className="relative col-span-2 md:w-72">
          <Search aria-hidden className="pointer-events-none absolute top-1/2 left-2.5 size-3.5 -translate-y-1/2 text-ink-3" />
          <input
            type="search"
            aria-label="Search clips by title or channel"
            placeholder="Search title or channel"
            className="field pl-8"
            value={query}
            onChange={(e) => {
              setQuery(e.target.value);
              setShown(PAGE_SIZE);
            }}
          />
        </div>
        <select aria-label="Filter by source video" className="field md:w-56" value={jobId ?? ""} onChange={(e) => router.replace(e.target.value ? `/clips/?job=${e.target.value}` : "/clips/")}>
          <option value="">All videos</option>
          {(jobs.data ?? []).map((job) => (
            <option key={job.id} value={job.id}>
              {job.title || job.url}
            </option>
          ))}
        </select>
        <select aria-label="Minimum score" className="field md:w-40" value={minScore} onChange={(e) => setMinScore(Number(e.target.value))}>
          <option value={0}>Any score</option>
          <option value={45}>Score 45 and up</option>
          <option value={60}>Score 60 and up</option>
          <option value={70}>Score 70 and up</option>
        </select>
        <select aria-label="Sort clips" className="field col-span-2 md:ml-auto md:w-44" value={sort} onChange={(e) => setSort(e.target.value as Sort)}>
          <option value="score">Highest score first</option>
          <option value="newest">Newest first</option>
          <option value="shortest">Shortest first</option>
          <option value="longest">Longest first</option>
        </select>
      </div>

      {clips.error ? (
        <ErrorState message={clips.error} onRetry={clips.reload} />
      ) : clips.loading && !clips.data ? (
        <ClipGridSkeleton count={12} />
      ) : total === 0 ? (
        <EmptyState
          icon={<Film />}
          title="No clips yet"
          body="Clips appear here once a video has been processed."
          action={
            <Link href="/dashboard/" className="btn btn-primary">
              Clip a video
            </Link>
          }
        />
      ) : filtered.length === 0 ? (
        <EmptyState
          icon={<Search />}
          title="No clips match"
          body="Nothing fits the current search and score filter."
          action={
            filtering && (
              <Button
                onClick={() => {
                  setQuery("");
                  setMinScore(0);
                }}
              >
                <X /> Clear filters
              </Button>
            )
          }
        />
      ) : (
        <>
          <div className={CLIP_GRID}>
            {filtered.slice(0, shown).map((clip) => (
              <ClipCard key={clip.id} clip={clip} />
            ))}
          </div>
          {filtered.length > shown && (
            <div className="mt-6 flex items-center justify-center gap-3">
              <span className="num text-xs text-ink-3">
                Showing {shown} of {filtered.length}
              </span>
              <Button onClick={() => setShown((n) => n + PAGE_SIZE)}>Show {Math.min(PAGE_SIZE, filtered.length - shown)} more</Button>
            </div>
          )}
        </>
      )}
    </>
  );
}

export default function ClipsPage() {
  return (
    <Suspense fallback={<ClipGridSkeleton count={12} />}>
      <Library />
    </Suspense>
  );
}
