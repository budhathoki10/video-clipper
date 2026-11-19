"use client";

import { ExternalLink, RefreshCw, TrendingUp } from "lucide-react";
import { useState, type ReactNode } from "react";

import { Pagination } from "@/components/pagination";
import { Button, EmptyState, ErrorState, Notice, PageHeader, SkeletonRows, useToast } from "@/components/ui";
import { api, formatCount, formatDuration, timeAgo, type Trends } from "@/lib/api";
import { usePagedApi } from "@/lib/pagination";

function Card({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="panel">
      <h2 className="border-b border-line-subtle px-4 py-2.5 text-xs font-medium text-ink-2">{title}</h2>
      <div className="p-4">{children}</div>
    </section>
  );
}

function Rows({ items }: { items: string[] }) {
  return (
    <ol className="flex flex-col divide-y divide-line-subtle">
      {items.map((item, i) => {
        // "Format: example" becomes a label with its example underneath.
        const cut = item.indexOf(":");
        let [head, tail] = cut > 0 && cut < 40 ? [item.slice(0, cut), item.slice(cut + 1).trim()] : [item, ""];
        // A label that only says "Format" carries nothing: show the example itself.
        if (/^(format|title|example)$/i.test(head.trim()) && tail) [head, tail] = [tail, ""];
        return (
          <li key={i} className="flex gap-3 py-2 first:pt-0 last:pb-0">
            <span aria-hidden className="num w-4 flex-none text-xs leading-5 text-ink-3">{i + 1}</span>
            <div className="min-w-0 text-sm">
              <p className="text-ink">{head}</p>
              {tail && <p className="truncate text-xs text-ink-3">{tail}</p>}
            </div>
          </li>
        );
      })}
    </ol>
  );
}

export default function TrendsPage() {
  const toast = useToast();
  const [page, setPage] = useState(1);
  const trends = usePagedApi<Trends>("/api/trends", page, 10, {}, 4000);
  const [starting, setStarting] = useState(false);
  const snapshot = trends.loading ? trends.previousData : trends.data;
  const data = snapshot?.data;
  const profile = data?.profile ?? {};
  const refreshing = starting || (snapshot?.refreshing ?? false);

  const stats = [
    { label: "Best clip length", value: profile.ideal_length_sec ? `${profile.ideal_length_sec} s` : "–" },
    { label: "Videos read", value: String(data?.videos.total ?? 0) },
    { label: "Most views", value: formatCount(data?.most_views) },
    { label: "Typical length", value: data?.typical_length ? formatDuration(data.typical_length) : "–" },
  ];

  async function refresh() {
    setStarting(true);
    try {
      await api("/api/trends/refresh", { method: "POST" });
      toast("Reading YouTube. This takes about a minute.");
      await trends.reload();
    } catch (err) {
      toast(err instanceof Error ? err.message : "Could not start the refresh.", "bad");
    } finally {
      setStarting(false);
    }
  }

  const refreshButton = (
    <Button loading={refreshing} onClick={refresh}>
      {!refreshing && <RefreshCw />} {refreshing ? "Reading YouTube" : "Refresh now"}
    </Button>
  );

  return (
    <>
      <PageHeader
        title="Trends"
        description={data ? `Read from YouTube ${timeAgo(data.fetched_at)}. Refreshed daily and used when clips are picked and titled.` : "What is getting views on YouTube, used when clips are picked and titled."}
        actions={refreshButton}
      />
      {trends.error ? (
        <ErrorState message={trends.error} onRetry={trends.reload} />
      ) : trends.loading && !data ? (
        <SkeletonRows rows={6} />
      ) : !data ? (
        <div className="flex flex-col gap-3">
          {trends.data?.error && !refreshing && <Notice tone="bad">The last refresh failed: {trends.data.error}</Notice>}
          <EmptyState icon={<TrendingUp />} title="No trend data yet" body="Read the current top Shorts from YouTube. The result guides which moments get clipped and how they are titled." action={refreshButton} />
        </div>
      ) : (
        <div className="flex flex-col gap-4">
          {data.warning && <Notice>YouTube was read, but the AI summary failed: {data.warning}</Notice>}

          <dl className="grid grid-cols-2 gap-px overflow-hidden rounded-lg border border-line bg-line shadow-soft md:grid-cols-4">
            {stats.map((stat) => (
              <div key={stat.label} className="bg-surface px-4 py-3">
                <dt className="text-xs text-ink-3">{stat.label}</dt>
                <dd className="num mt-0.5 text-lg font-semibold tracking-tight">{stat.value}</dd>
              </div>
            ))}
          </dl>

          {profile.summary && <p className="max-w-3xl text-md text-ink">{profile.summary}</p>}

          <div className="grid gap-4 md:grid-cols-2">
            {!!profile.hook_patterns?.length && <Card title="Open with"><Rows items={profile.hook_patterns} /></Card>}
            {!!profile.title_patterns?.length && <Card title="Title formats"><Rows items={profile.title_patterns} /></Card>}
            {!!profile.topics?.length && (
              <Card title="Moments getting views">
                <ul className="flex flex-wrap gap-1.5">
                  {profile.topics.map((topic, i) => (
                    <li key={i} className="rounded-sm border border-line bg-raised px-2 py-1 text-sm">{topic}</li>
                  ))}
                </ul>
              </Card>
            )}
            {!!profile.avoid?.length && (
              <Card title="Avoid">
                <ul className="flex flex-col gap-2 text-sm">
                  {profile.avoid.map((item, i) => (
                    <li key={i} className="flex gap-2.5">
                      <span aria-hidden className="mt-2 h-px w-2.5 flex-none bg-bad" />
                      {item}
                    </li>
                  ))}
                </ul>
              </Card>
            )}
          </div>

          <section className="panel scroll-mt-16 md:scroll-mt-4" id="trend-results" aria-labelledby="videos-heading">
            <h2 id="videos-heading" className="border-b border-line-subtle px-4 py-2.5 text-xs font-medium text-ink-2">
              Top results read
            </h2>
            {trends.loading ? <div className="p-4"><SkeletonRows rows={5} /></div> : !data.videos.items.length ? (
              <p className="p-4 text-sm text-ink-2">No video results were saved. Refresh to read YouTube again.</p>
            ) : (
            <div className="overflow-x-auto">
            <table className="w-full min-w-[560px] text-sm">
              <thead className="border-b border-line-subtle text-xs text-ink-3">
                <tr>
                  <th scope="col" className="px-4 py-2 text-left font-medium">Video</th>
                  <th scope="col" className="px-4 py-2 text-right font-medium">Views</th>
                  <th scope="col" className="px-4 py-2 text-right font-medium">Length</th>
                  <th scope="col" className="px-4 py-2 text-left font-medium">Search</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-line-subtle">
                {data.videos.items.map((video) => (
                  <tr key={video.id} className="hover:bg-raised">
                    <td className="max-w-[420px] px-4 py-2">
                      <a href={video.url} target="_blank" rel="noreferrer" className="group flex items-center gap-1.5">
                        <span className="truncate font-medium group-hover:underline">{video.title}</span>
                        <ExternalLink aria-hidden className="size-3 flex-none text-ink-3" />
                      </a>
                      <p className="truncate text-xs text-ink-3">{video.channel}</p>
                    </td>
                    <td className="num px-4 py-2 text-right">{formatCount(video.views)}</td>
                    <td className="num px-4 py-2 text-right text-ink-2">{formatDuration(video.duration)}</td>
                    <td className="px-4 py-2 text-xs whitespace-nowrap text-ink-3">{video.query}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            </div>
            )}
            <Pagination data={data.videos} label="Trend results" onPageChange={setPage} loading={trends.loading} className="border-t border-line-subtle px-4 py-3" anchorId="trend-results" />
          </section>
        </div>
      )}
    </>
  );
}
