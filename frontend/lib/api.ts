"use client";

import { useCallback, useEffect, useRef, useState } from "react";

export const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "";

export type JobStatus = "queued" | "running" | "stopping" | "done" | "failed" | "cancelled";

export interface Job {
  id: string;
  kind: "auto" | "video" | "live";
  url: string;
  status: JobStatus;
  step: string | null;
  progress: number;
  title: string | null;
  channel: string | null;
  duration: number | null;
  thumbnail: string | null;
  error: string | null;
  payload: {
    warning?: string;
    watched_sec?: number;
    clips?: number;
    scores?: number[];
    threshold?: number;
    last_error?: string;
    storage_warning?: string;
  } | null;
  clip_count?: number;
  more?: { id: string; status: JobStatus; step: string | null; progress: number; error: string | null } | null;
  created_at: number;
  updated_at: number;
  clips?: Clip[];
}

export interface Panel {
  score: number;
  viewers: number;
  watch: number;
  finished: number;
  like: number;
  share: number;
  by_type: { type: string; watch: number }[];
  comments: { type: string; text: string; watch: number }[];
}

export interface Clip {
  id: string;
  job_id: string;
  status: "rendering" | "ready" | "failed";
  start: number;
  end: number;
  duration: number;
  title: string;
  hook: string | null;
  description: string | null;
  hashtags: string[] | null;
  reason: string | null;
  ai_score: number | null;
  panel_score: number | null;
  panel: Panel | null;
  file_url: string | null;
  thumb_url: string | null;
  width: number | null;
  height: number | null;
  source_title: string | null;
  source_channel: string | null;
  source_url: string | null;
  format: Settings["format"];
  captions: Settings["captions"];
  error: string | null;
  created_at: number;
  posts?: Post[];
}

export interface Post {
  id: string;
  clip_id: string;
  status: "uploading" | "scheduled" | "published" | "failed";
  kind?: "short" | "video" | null;
  title: string;
  description: string;
  privacy: string;
  publish_at: string | null;
  youtube_id: string | null;
  views: number | null;
  error: string | null;
  thumb?: string | null;
  panel_score?: number | null;
  created_at: number;
}

export type Provider = "nvidia" | "anthropic" | "gemini" | "ollama";

export interface Settings {
  clip: { min_sec: number; max_sec: number; max_clips: number; min_score: number };
  format: {
    aspect: "9:16" | "16:9" | "1:1";
    layout: "blur" | "crop";
    zoom: number;
    quality: number;
    fps: number;
    source_quality: number;
  };
  captions: {
    enabled: boolean;
    font: string;
    size: "small" | "medium" | "large";
    color: string;
    highlight: string;
    position: "lower" | "middle";
    words_per_line: number;
    uppercase: boolean;
    language: string;
  };
  overlay: { hook_title: boolean; credit: boolean };
  ai: {
    provider: Provider;
    models: Record<Provider, string>;
    panel_size: number;
    whisper_model: string;
  };
  live: {
    sensitivity: number;
    segment_sec: number;
    lookback_sec: number;
    cooldown_sec: number;
    min_score: number;
    quality: number;
  };
  trends: { queries: string[]; match_topic: boolean; per_query: number; refresh_hours: number };
  posting: { privacy: "private" | "unlisted" | "public"; hashtags: string; credit: boolean };
}

export type SecretStatus = Record<string, { set: boolean; hint: string }>;

export interface YouTubeStatus {
  client_configured: boolean;
  connected: boolean;
  channel: string;
  connecting: boolean;
  auth_url?: string;
  error: string;
}

export interface SystemStatus {
  worker_alive: boolean;
  provider: Provider;
  model: string;
  ai_ready: boolean;
  youtube: YouTubeStatus;
}

export interface AuthUser {
  id: string;
  email: string;
  name: string;
  avatar_url: string | null;
  is_admin: boolean;
  created_at: number;
  last_login_at: number | null;
}

export interface UserStats {
  total_users: number;
  active_users: number;
}

export interface AuthSession {
  user: AuthUser;
  stats: UserStats | null;
}

export interface ManagedUser extends AuthUser {
  last_seen_at: number | null;
  active: number;
}

export interface TrendVideo {
  id: string;
  title: string;
  channel: string;
  views: number;
  duration: number;
  url: string;
  query: string;
}

export interface Trends {
  data: {
    fetched_at: number;
    videos: TrendVideo[];
    warning: string | null;
    profile: {
      summary?: string;
      hook_patterns?: string[];
      title_patterns?: string[];
      topics?: string[];
      ideal_length_sec?: number;
      avoid?: string[];
    };
  } | null;
  refreshing: boolean;
  error: string | null;
}

export class ApiError extends Error {
  constructor(message: string, public status: number) {
    super(message);
  }
}

const OFFLINE = "Cannot reach the clipper engine. Start it with scripts\\start.ps1, then reload.";

export async function api<T>(path: string, init?: RequestInit & { json?: unknown }): Promise<T> {
  const { json, ...rest } = init ?? {};
  let response: Response;
  try {
    response = await fetch(`${API_URL}${path}`, {
      ...rest,
      credentials: "include",
      headers: json === undefined ? rest.headers : { "Content-Type": "application/json", ...rest.headers },
      body: json === undefined ? rest.body : JSON.stringify(json),
    });
  } catch {
    throw new ApiError(OFFLINE, 0);
  }
  if (response.status === 204) return undefined as T;
  const data = await response.json().catch(() => null);
  if (!response.ok) {
    const detail = data?.detail;
    const message =
      typeof detail === "string"
        ? detail
        : Array.isArray(detail)
          ? detail.map((d: { msg?: string }) => d.msg).join(". ")
          : `Request failed (${response.status}).`;
    throw new ApiError(message, response.status);
  }
  return data as T;
}

export const mediaUrl = (path: string | null | undefined) => (path ? `${API_URL}${path}` : "");

/** Fetches `path` and refetches every `interval` ms while the tab is visible. */
export function useApi<T>(path: string | null, interval = 0) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(path !== null);
  const current = useRef(path);

  const load = useCallback(async () => {
    if (path === null) return;
    try {
      const result = await api<T>(path);
      if (current.current !== path) return;
      setData(result);
      setError(null);
    } catch (err) {
      if (current.current !== path) return;
      setError(err instanceof Error ? err.message : "Something went wrong.");
    } finally {
      if (current.current === path) setLoading(false);
    }
  }, [path]);

  useEffect(() => {
    current.current = path;
    if (path === null) return;
    setLoading(true);
    void load();
    if (!interval) return;
    const timer = setInterval(() => {
      if (document.visibilityState === "visible") void load();
    }, interval);
    return () => clearInterval(timer);
  }, [path, interval, load]);

  return { data, error, loading, reload: load, setData };
}

export function formatDuration(seconds: number | null | undefined): string {
  if (!seconds || seconds < 0) return "0:00";
  const total = Math.round(seconds);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = String(total % 60).padStart(2, "0");
  return h ? `${h}:${String(m).padStart(2, "0")}:${s}` : `${m}:${s}`;
}

export function formatCount(value: number | null | undefined): string {
  if (value === null || value === undefined) return "–";
  if (value >= 1e9) return `${(value / 1e9).toFixed(1)}B`;
  if (value >= 1e6) return `${(value / 1e6).toFixed(1)}M`;
  if (value >= 1e3) return `${(value / 1e3).toFixed(1)}K`;
  return String(value);
}

export function timeAgo(epochSeconds: number): string {
  const diff = Date.now() / 1000 - epochSeconds;
  if (diff < 60) return "just now";
  if (diff < 3600) return `${Math.floor(diff / 60)} min ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)} h ago`;
  return new Date(epochSeconds * 1000).toLocaleDateString(undefined, { day: "numeric", month: "short" });
}

export const clipScore = (clip: Pick<Clip, "panel_score" | "ai_score">) =>
  clip.panel_score ?? clip.ai_score ?? null;
