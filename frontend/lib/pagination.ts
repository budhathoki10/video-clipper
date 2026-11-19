"use client";

import { useEffect, useState } from "react";
import { useApi } from "./api";

export function usePagedApi<T>(
  path: string | null,
  page: number,
  pageSize: number,
  params: Record<string, string>,
  interval = 0,
) {
  const query = new URLSearchParams({ ...params, page: String(page), page_size: String(pageSize) });
  return useApi<T>(path && `${path}?${query}`, interval);
}

export function useDebouncedSearch(value: string) {
  const [search, setSearch] = useState(value.trim());
  useEffect(() => {
    const timer = setTimeout(() => setSearch(value.trim()), 250);
    return () => clearTimeout(timer);
  }, [value]);
  return search;
}
