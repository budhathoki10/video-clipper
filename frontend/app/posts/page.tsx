"use client";

import { ArrowDown, ArrowUp, ExternalLink, Send } from "lucide-react";
import Link from "next/link";
import { useState } from "react";

import { Pagination } from "@/components/pagination";
import { Badge, Button, EmptyState, ErrorState, PageHeader, SkeletonRows } from "@/components/ui";
import { formatCount, timeAgo, type Page, type Post } from "@/lib/api";
import { usePagedApi } from "@/lib/pagination";

type SortKey = "created_at" | "views" | "title";
type Filter = "all" | Post["status"];

const TONE = { uploading: "accent", scheduled: "warn", published: "ok", failed: "bad" } as const;
const LABEL = { uploading: "Uploading", scheduled: "Scheduled", published: "Published", failed: "Failed" } as const;

export default function PostsPage() {
  const [filter, setFilter] = useState<Filter>("all");
  const [sort, setSort] = useState<{ key: SortKey; direction: "asc" | "desc" }>({ key: "created_at", direction: "desc" });
  const [page, setPage] = useState(1);
  const posts = usePagedApi<Page<Post> & { unfiltered_total: number }>("/api/posts", page, 20, {
    ...(filter === "all" ? {} : { status: filter }), sort: sort.key, direction: sort.direction,
  }, 5000);

  const header = (key: SortKey, label: string, align = "text-left") => {
    const active = sort.key === key;
    return (
      <th scope="col" aria-sort={active ? (sort.direction === "asc" ? "ascending" : "descending") : "none"} className={`px-4 py-2 font-medium ${align}`}>
        <button type="button" className="inline-flex items-center gap-1 rounded-sm hover:text-ink" onClick={() => { setSort({ key, direction: active && sort.direction === "desc" ? "asc" : "desc" }); setPage(1); }}>
          {label}
          {active && (sort.direction === "asc" ? <ArrowUp aria-hidden className="size-3" /> : <ArrowDown aria-hidden className="size-3" />)}
        </button>
      </th>
    );
  };

  return (
    <div id="posts-list" className="scroll-mt-16 md:scroll-mt-4">
      <PageHeader
        title="Posts"
        description="Clips sent to your YouTube channel. View counts refresh every 30 minutes."
        actions={
          <select aria-label="Filter posts by status" className="field w-40" value={filter} onChange={(e) => { setFilter(e.target.value as Filter); setPage(1); }}>
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
      ) : !posts.data?.unfiltered_total ? (
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
      ) : posts.data.items.length === 0 ? (
        <EmptyState title="No posts with this status" body="Pick another status to see the rest." action={<Button onClick={() => setFilter("all")}>Show all posts</Button>} />
      ) : (
          <div className="panel relative overflow-x-auto">
            <table className="w-full min-w-[720px] text-sm">
              <thead className="border-b border-line-subtle text-xs text-ink-3">
                <tr>
                  {header("title", "Clip")}
                  <th scope="col" className="px-4 py-2 text-left font-medium">Type</th>
                  <th scope="col" className="px-4 py-2 text-left font-medium">Status</th>
                  <th scope="col" className="px-4 py-2 text-left font-medium">Goes public</th>
                  {header("views", "Views", "text-right")}
                  {header("created_at", "Sent", "text-right")}
                  <th scope="col" aria-label="Links" className="px-4 py-2" />
                </tr>
              </thead>
              <tbody className="divide-y divide-line-subtle">
                {posts.data.items.map((post) => (
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
      )}
      <Pagination data={posts.data ?? (posts.loading ? posts.previousData : null)} label="Posts" onPageChange={setPage} loading={posts.loading} className="mt-4" anchorId="posts-list" />
    </div>
  );
}
