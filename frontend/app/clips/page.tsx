"use client";

import { ChevronDown, Film, Search, X } from "lucide-react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useState } from "react";

import { CLIP_GRID, ClipCard, ClipGridSkeleton } from "@/components/clip-card";
import { Pagination } from "@/components/pagination";
import { Button, Dialog, EmptyState, ErrorState, PageHeader, SkeletonRows } from "@/components/ui";
import type { ClipSummary, Job, Page } from "@/lib/api";
import { withoutHidden } from "@/lib/optimistic";
import { useDebouncedSearch, usePagedApi } from "@/lib/pagination";

type Sort = "score" | "newest" | "shortest" | "longest";
type ClipPage = Page<ClipSummary> & { unfiltered_total: number; source_title?: string | null };
type Source = Pick<Job, "id" | "title" | "url">;

function SourceOptions({ jobId, onSelect }: { jobId: string | null; onSelect: (id: string | null) => void }) {
  const [query, setQuery] = useState("");
  const search = useDebouncedSearch(query);
  const [page, setPage] = useState(1);
  const jobs = usePagedApi<Page<Source>>("/api/jobs", page, 6, { summary: "true", q: search });

  return (
    <div className="flex flex-col gap-3">
      <input type="search" aria-label="Search source videos" placeholder="Search videos" className="field" maxLength={200}
        value={query} onChange={(e) => { setQuery(e.target.value); setPage(1); }} />
      <Button variant="ghost" aria-pressed={!jobId} onClick={() => onSelect(null)} className="self-start">All videos</Button>
      {jobs.error ? <ErrorState message={jobs.error} onRetry={jobs.reload} /> : jobs.loading ? <SkeletonRows rows={3} /> : (
        jobs.data?.items.length ? (
          <ul className="divide-y divide-line-subtle rounded-md border border-line">
            {jobs.data.items.map((job) => (
              <li key={job.id}>
                <Button variant="ghost" aria-pressed={job.id === jobId} onClick={() => onSelect(job.id)} className="w-full justify-start px-3">
                  <span className="truncate">{job.title || job.url || "Untitled video"}</span>
                </Button>
              </li>
            ))}
          </ul>
        ) : <p className="text-sm text-ink-2">{search ? "No videos match your search." : "No videos yet."}</p>
      )}
      <Pagination data={jobs.data ?? (jobs.loading ? jobs.previousData : null)} label="Source videos" onPageChange={setPage} loading={jobs.loading} />
    </div>
  );
}

function Library() {
  const router = useRouter();
  const jobId = useSearchParams().get("job");
  const [query, setQuery] = useState("");
  const [sort, setSort] = useState<Sort>("score");
  const [minScore, setMinScore] = useState(0);
  const [position, setPosition] = useState({ jobId, page: 1 });
  const [sourceOpen, setSourceOpen] = useState(false);
  const search = useDebouncedSearch(query);
  const page = position.jobId === jobId ? position.page : 1;
  const setPage = (next: number) => setPosition({ jobId, page: next });
  const clips = usePagedApi<ClipPage>("/api/clips", page, 12, {
    ...(jobId ? { job_id: jobId } : {}), q: search, sort, min_score: String(minScore),
  }, 4000);
  const source = clips.data?.source_title;
  const total = clips.data?.unfiltered_total ?? 0;

  function selectSource(id: string | null) {
    setSourceOpen(false);
    if (id === jobId) return;
    setPosition({ jobId: id, page: 1 });
    router.replace(id ? `/clips/?${new URLSearchParams({ job: id })}` : "/clips/", { scroll: false });
  }

  return (
    <>
      <PageHeader
        title="Clips"
        description={
          clips.data ? `${clips.data.total} of ${total} ${total === 1 ? "clip" : "clips"}${source ? ` from “${source}”` : ""}` : "Every clip you have made."
        }
      />

      <div id="clip-filters" className="mb-4 grid scroll-mt-16 grid-cols-2 gap-2 md:flex md:scroll-mt-4 md:flex-wrap md:items-center">
        <div className="relative col-span-2 md:w-72">
          <Search aria-hidden className="pointer-events-none absolute top-1/2 left-2.5 size-3.5 -translate-y-1/2 text-ink-3" />
          <input
            type="search"
            aria-label="Search clips by title or channel"
            placeholder="Search title or channel"
            className="field pl-8"
            maxLength={200}
            value={query}
            onChange={(e) => {
              setQuery(e.target.value);
              setPage(1);
            }}
          />
        </div>
        <Button aria-label="Filter by source video" aria-haspopup="dialog" onClick={() => setSourceOpen(true)} className="min-w-0 justify-between md:w-56">
          <span className="truncate">{source || (jobId ? "Selected video" : "All videos")}</span>
          <ChevronDown aria-hidden />
        </Button>
        <select aria-label="Minimum score" className="field md:w-40" value={minScore} onChange={(e) => { setMinScore(Number(e.target.value)); setPage(1); }}>
          <option value={0}>Any score</option>
          <option value={45}>Score 45 and up</option>
          <option value={60}>Score 60 and up</option>
          <option value={70}>Score 70 and up</option>
        </select>
        <select aria-label="Sort clips" className="field col-span-2 md:ml-auto md:w-44" value={sort} onChange={(e) => { setSort(e.target.value as Sort); setPage(1); }}>
          <option value="score">Highest score first</option>
          <option value="newest">Newest first</option>
          <option value="shortest">Shortest first</option>
          <option value="longest">Longest first</option>
        </select>
      </div>
      <Dialog open={sourceOpen} onClose={() => setSourceOpen(false)} title="Filter by source video">
        {sourceOpen && <SourceOptions jobId={jobId} onSelect={selectSource} />}
      </Dialog>

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
      ) : clips.data?.items.length === 0 ? (
        <EmptyState
          icon={<Search />}
          title="No clips match"
          body="Nothing fits the current search and score filter."
          action={
            <Button onClick={() => { setQuery(""); setMinScore(0); setPage(1); }}>
              <X /> Clear filters
            </Button>
          }
        />
      ) : (
        <div className={CLIP_GRID}>
          {withoutHidden(clips.data?.items ?? []).map((clip) => (
            <ClipCard key={clip.id} clip={clip} />
          ))}
        </div>
      )}
      <Pagination data={clips.data ?? (clips.loading ? clips.previousData : null)} label="Clips" onPageChange={setPage} loading={clips.loading || search !== query.trim()} className="mt-4" anchorId="clip-filters" />
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
