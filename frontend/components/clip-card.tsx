"use client";

import { AlertCircle } from "lucide-react";
import Link from "next/link";

import { clipScore, formatDuration, mediaUrl, type ClipSummary } from "@/lib/api";
import { Score, Spinner } from "./ui";

export function ClipCard({ clip }: { clip: ClipSummary }) {
  return (
    <Link
      href={`/clip/?id=${clip.id}`}
      className="group flex flex-col overflow-hidden rounded-lg border border-line bg-surface shadow-soft transition-[box-shadow,border-color] duration-150 hover:border-line-strong hover:shadow-lift"
    >
      <div className="relative aspect-[9/16] bg-raised">
        {clip.status === "ready" && clip.thumb_url ? (
          // eslint-disable-next-line @next/next/no-img-element
          <img src={mediaUrl(clip.thumb_url)} alt="" loading="lazy" className="size-full object-cover" />
        ) : clip.status === "failed" ? (
          <div className="flex size-full flex-col items-center justify-center gap-2 p-3 text-center text-xs text-bad-ink">
            <AlertCircle aria-hidden className="size-4" />
            Render failed
          </div>
        ) : (
          <div className="flex size-full flex-col items-center justify-center gap-2 text-xs text-ink-3">
            <Spinner />
            Rendering
          </div>
        )}
        {clip.status === "ready" && (
          <>
            <span className="absolute top-2 left-2 rounded-sm bg-surface/95 px-1.5 py-0.5 shadow-soft">
              <Score value={clipScore(clip)} />
            </span>
            <span className="num absolute right-2 bottom-2 rounded-sm bg-ink/80 px-1.5 py-0.5 text-xs text-white">
              {formatDuration(clip.duration)}
            </span>
          </>
        )}
      </div>
      <div className="flex flex-col gap-0.5 p-2.5">
        <h3 className="line-clamp-2 text-sm font-medium text-ink">{clip.title}</h3>
        <p className="truncate text-xs text-ink-3">{clip.source_channel || "Unknown source"}</p>
      </div>
    </Link>
  );
}

export function ClipGridSkeleton({ count = 6 }: { count?: number }) {
  return (
    <div aria-busy="true" aria-label="Loading clips" className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-6">
      {Array.from({ length: count }, (_, i) => (
        <div key={i} className="skeleton aspect-[9/19] rounded-lg" />
      ))}
    </div>
  );
}

export const CLIP_GRID = "grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-6";
