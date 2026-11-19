"use client";

import { ChevronLeft, ChevronRight } from "lucide-react";
import type { Page } from "@/lib/api";
import { Button, cx } from "./ui";

export function Pagination({ data, label, onPageChange, loading, className, anchorId }: {
  data: Pick<Page<unknown>, "page" | "page_size" | "total"> | null | undefined;
  label: string;
  onPageChange: (page: number) => void;
  loading?: boolean;
  className?: string;
  anchorId?: string;
}) {
  if (!data || data.total <= data.page_size) return null;
  const { page, page_size: size, total } = data;
  const pages = Math.ceil(total / size);

  function changePage(next: number) {
    onPageChange(next);
    const anchor = anchorId ? document.getElementById(anchorId) : null;
    if (anchor && anchor.getBoundingClientRect().top < 48) anchor.scrollIntoView({ block: "start" });
  }

  return (
    <nav aria-label={`${label} pagination`} aria-busy={loading || undefined}
      className={cx("flex flex-wrap items-center justify-between gap-x-3 gap-y-2", className)}>
      <p className="num text-xs text-ink-2" role="status">
        {loading ? "Loading page…" : `${(page - 1) * size + 1}–${Math.min(page * size, total)} of ${total}`}
      </p>
      <div className="flex items-center gap-2">
        <Button aria-label="Previous page" size="icon" onClick={() => changePage(page - 1)} disabled={loading || page === 1}>
          <ChevronLeft aria-hidden />
        </Button>
        <span className="num whitespace-nowrap text-xs text-ink-2" aria-current="page">Page {page} of {pages}</span>
        <Button aria-label="Next page" size="icon" onClick={() => changePage(page + 1)} disabled={loading || page === pages}>
          <ChevronRight aria-hidden />
        </Button>
      </div>
    </nav>
  );
}
