"use client";

import { ArrowDown, ArrowUp, ExternalLink, Send } from "lucide-react";
import Link from "next/link";
import { useMemo, useState } from "react";

import { Badge, Button, EmptyState, ErrorState, PageHeader, SkeletonRows } from "@/components/ui";
import { formatCount, timeAgo, useApi, type Post } from "@/lib/api";

const PAGE_SIZE = 20;
type SortKey = "created_at" | "views" | "title";
type Filter = "all" | Post["status"];

const TONE = { uploading: "accent", scheduled: "warn", published: "ok", failed: "bad" } as const;
const LABEL = { uploading: "Uploading", scheduled: "Scheduled", published: "Published", failed: "Failed" } as const;

export default function PostsPage() {
  const posts = useApi<Post[]>("/api/posts", 5000);
  const [filter, setFilter] = useState<Filter>("all");
  const [sort, setSort] = useState<{ key: SortKey; dir: 1 | -1 }>({ key: "created_at", dir: -1 });
  const [page, setPage] = useState(0);

  const rows = useMemo(() => {
    const list = (posts.data ?? []).filter((p) => filter === "all" || p.status === filter);
    return list.sort((a, b) => {
      const x = sort.key === "title" ? a.title.localeCompare(b.title) : (a[sort.key] ?? -1) - (b[sort.key] ?? -1);
      return x * sort.dir;
    });
  }, [posts.data, filter, sort]);
  const pages = Math.max(Math.ceil(rows.length / PAGE_SIZE), 1);
  const current = Math.min(page, pages - 1);
  const visible = rows.slice(current * PAGE_SIZE, (current + 1) * PAGE_SIZE);

  const header = (key: SortKey, label: string, align = "text-left") => {
    const active = sort.key === key;
    return (
      <th scope="col" aria-sort={active ? (sort.dir === 1 ? "ascending" : "descending") : "none"} className={`px-4 py-2 font-medium ${align}`}>
        <button type="button" className="inline-flex items-center gap-1 rounded-sm hover:text-ink" onClick={() => setSort({ key, dir: active && sort.dir === -1 ? 1 : -1 })}>
          {label}
          {active && (sort.dir === 1 ? <ArrowUp aria-hidden className="size-3" /> : <ArrowDown aria-hidden className="size-3" />)}
        </button>
      </th>
    );
  };

  return (
    <>
      <PageHeader
        title="Posts"
        description="Clips sent to your YouTube channel. View counts refresh every 30 minutes."
        actions={
          <select aria-label="Filter posts by status" className="field w-40" value={filter} onChange={(e) => { setFilter(e.target.value as Filter); setPage(0); }}>
            <option value="all">All statuses</option>
            <option value="published">Published</option>
            <option value="scheduled">Scheduled</option>
            <option value="uploading">Uploading</option>
            <option value="failed">Failed</option>
          </select>
        }
      />
      {posts.error ? (
        <ErrorState message={posts.error} onRetry={posts.reload} />
      ) : posts.loading && !posts.data ? (
        <SkeletonRows rows={5} />
      ) : (posts.data ?? []).length === 0 ? (
        <EmptyState
          icon={<Send />}
          title="Nothing posted yet"
          body="Open a clip and choose Post now or Schedule. Posts and their view counts are listed here."
          action={
            <Link href="/clips/" className="btn btn-primary">
              Choose a clip
            </Link>
          }
        />
      ) : rows.length === 0 ? (
        <EmptyState title="No posts with this status" body="Pick another status to see the rest." action={<Button onClick={() => setFilter("all")}>Show all posts</Button>} />
      ) : (
        <>
          <div className="panel overflow-x-auto">
            <table className="w-full min-w-[720px] text-sm">
              <thead className="border-b border-line-subtle text-xs text-ink-3">
                <tr>
                  {header("title", "Clip")}
                  <th scope="col" className="px-4 py-2 text-left font-medium">Type</th>
                  <th scope="col" className="px-4 py-2 text-left font-medium">Status</th>
                  <th scope="col" className="px-4 py-2 text-left font-medium">Goes public</th>
                  {header("views", "Views", "text-right")}
                  {header("created_at", "Sent", "text-right")}
                  <th scope="col" className="px-4 py-2"><span className="sr-only">Links</span></th>
                </tr>
              </thead>
              <tbody className="divide-y divide-line-subtle">
                {visible.map((post) => (
                  <tr key={post.id} className="hover:bg-raised">
                    <td className="max-w-[320px] px-4 py-2.5">
                      <Link href={`/clip/?id=${post.clip_id}`} className="block truncate font-medium hover:underline">
                        {post.title}
                      </Link>
                      {post.status === "failed" && <p className="truncate text-xs text-bad-ink">{post.error}</p>}
                    </td>
                    <td className="px-4 py-2.5 text-ink-2">{post.kind === "short" ? "Short" : post.kind === "video" ? "Video" : "–"}</td>
                    <td className="px-4 py-2.5">
                      <div className="flex items-center gap-2">
                        <Badge tone={TONE[post.status]}>{LABEL[post.status]}</Badge>
                        {post.status !== "failed" && <span className="text-xs text-ink-3">{post.privacy[0].toUpperCase() + post.privacy.slice(1)}</span>}
                      </div>
                    </td>
                    <td className="px-4 py-2.5 text-ink-2">{post.publish_at ? new Date(post.publish_at).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }) : post.status === "published" ? "Now" : "–"}</td>
                    <td className="num px-4 py-2.5 text-right">{formatCount(post.views)}</td>
                    <td className="px-4 py-2.5 text-right whitespace-nowrap text-ink-2">{timeAgo(post.created_at)}</td>
                    <td className="px-4 py-2.5 text-right">
                      {post.youtube_id && (
                        <a href={`https://youtu.be/${post.youtube_id}`} target="_blank" rel="noreferrer" aria-label={`Open ${post.title} on YouTube`} className="btn btn-ghost btn-icon">
                          <ExternalLink />
                        </a>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {pages > 1 && (
            <nav aria-label="Pagination" className="mt-4 flex items-center justify-between">
              <span className="num text-xs text-ink-3">Page {current + 1} of {pages}</span>
              <div className="flex gap-2">
                <Button disabled={current === 0} onClick={() => setPage(current - 1)}>Previous</Button>
                <Button disabled={current >= pages - 1} onClick={() => setPage(current + 1)}>Next</Button>
              </div>
            </nav>
          )}
        </>
      )}
    </>
  );
}
