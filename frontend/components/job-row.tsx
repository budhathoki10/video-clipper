"use client";

import { Plus, RotateCw, Square, Trash2 } from "lucide-react";
import Link from "next/link";
import { useState } from "react";

import { api, formatDuration, timeAgo, type Job } from "@/lib/api";
import { Badge, Button, Dialog, Field, Progress, Toggle, useToast } from "./ui";

const STATUS: Record<Job["status"], { label: string; tone: "neutral" | "accent" | "ok" | "warn" | "bad" }> = {
  queued: { label: "Queued", tone: "neutral" },
  running: { label: "Working", tone: "accent" },
  stopping: { label: "Stopping", tone: "warn" },
  done: { label: "Done", tone: "ok" },
  failed: { label: "Failed", tone: "bad" },
  cancelled: { label: "Cancelled", tone: "neutral" },
};

export function JobRow({ job, onChange }: { job: Job; onChange: () => void }) {
  const toast = useToast();
  const [busy, setBusy] = useState<string | null>(null);
  const [confirm, setConfirm] = useState(false);
  const [moreOpen, setMoreOpen] = useState(false);
  const [moreCount, setMoreCount] = useState(3);
  const [labelPart, setLabelPart] = useState(false);
  const more = job.more;
  const moreActive = more?.status === "queued" || more?.status === "running" || more?.status === "stopping";
  const canMakeMore = job.kind === "video" && job.status === "done" && (job.clip_count ?? 0) > 0 && !moreActive;
  const active = job.status === "queued" || job.status === "running";
  const live = job.kind === "live";
  const status = live && job.status === "running" ? { label: "Live", tone: "bad" as const } : STATUS[job.status];

  async function act(name: string, path: string, method: string, done: string) {
    setBusy(name);
    try {
      await api(path, { method });
      toast(done);
      onChange();
    } catch (err) {
      toast(err instanceof Error ? err.message : "Something went wrong.", "bad");
    } finally {
      setBusy(null);
      setConfirm(false);
    }
  }

  async function makeMore() {
    setBusy("more");
    try {
      await api(`/api/jobs/${job.id}/more`, { method: "POST", json: { count: moreCount, label_part: labelPart } });
      toast("Looking for more moments. This keeps going if you close the page.");
      setMoreOpen(false);
      onChange();
    } catch (err) {
      toast(err instanceof Error ? err.message : "Could not start.", "bad");
    } finally {
      setBusy(null);
    }
  }

  return (
    <li className="flex flex-col gap-3 px-4 py-3 sm:flex-row sm:items-center">
      <div className="flex min-w-0 flex-1 items-center gap-3">
        <div className="h-10 w-[71px] flex-none overflow-hidden rounded-sm bg-raised">
          {/* eslint-disable-next-line @next/next/no-img-element */}
          {job.thumbnail && <img src={job.thumbnail} alt="" referrerPolicy="no-referrer" className="size-full object-cover" />}
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <p className="truncate text-sm font-medium">{job.title || job.url}</p>
            <Badge tone={status.tone}>{status.label}</Badge>
          </div>
          <p className="truncate text-xs text-ink-3">
            {job.status === "failed" ? (
              <span className="text-bad-ink">{job.error}</span>
            ) : (
              <>
                {job.channel && `${job.channel} · `}
                {job.step || "Queued"}
                {!active && !live && job.duration ? ` · ${formatDuration(job.duration)}` : ""}
                {!active && ` · ${timeAgo(job.created_at)}`}
              </>
            )}
          </p>
          {job.payload?.warning && <p className="truncate text-xs text-warn-ink">{job.payload.warning}</p>}
          {job.payload?.storage_warning && <p className="truncate text-xs text-warn-ink">{job.payload.storage_warning}</p>}
          {job.status === "running" && !live && (
            <div className="mt-2 max-w-md">
              <Progress value={job.progress} label={`Progress for ${job.title || "job"}`} />
            </div>
          )}
          {moreActive && more && (
            <div className="mt-2 max-w-md">
              <p className="mb-1 text-xs text-accent-hover">More clips · {more.step || "Queued"}</p>
              <Progress value={more.progress} label={`Progress finding more clips for ${job.title || "video"}`} />
            </div>
          )}
          {more?.status === "failed" && more.error && <p className="truncate text-xs text-bad-ink">More clips failed: {more.error}</p>}
        </div>
      </div>
      <div className="flex flex-none items-center gap-1.5 pl-[83px] sm:pl-0">
        {(job.clip_count ?? 0) > 0 && (
          <Link href={`/clips/?job=${job.id}`} className="btn btn-secondary">
            <span className="num">{job.clip_count}</span> {job.clip_count === 1 ? "clip" : "clips"}
          </Link>
        )}
        {moreActive && more && more.status !== "stopping" && (
          <Button loading={busy === "stopMore"} onClick={() => act("stopMore", `/api/jobs/${more.id}/stop`, "POST", "Stopped looking for more clips.")}>
            <Square /> Cancel
          </Button>
        )}
        {canMakeMore && (
          <Button onClick={() => setMoreOpen(true)}>
            <Plus /> More clips
          </Button>
        )}
        {active && (
          <Button loading={busy === "stop"} onClick={() => act("stop", `/api/jobs/${job.id}/stop`, "POST", live ? "Stopping the watch. Clips are kept." : "Job cancelled.")}>
            <Square /> {live ? "Stop watching" : "Cancel"}
          </Button>
        )}
        {(job.status === "failed" || job.status === "cancelled") && (
          <Button loading={busy === "retry"} onClick={() => act("retry", `/api/jobs/${job.id}/retry`, "POST", "Job queued again.")}>
            <RotateCw /> Retry
          </Button>
        )}
        {!active && job.status !== "stopping" && (
          <Button variant="ghost" size="icon" aria-label={`Delete ${job.title || "job"}`} onClick={() => setConfirm(true)}>
            <Trash2 />
          </Button>
        )}
      </div>
      <Dialog
        open={moreOpen}
        onClose={() => setMoreOpen(false)}
        title="Make more clips from this video"
        footer={
          <>
            <Button onClick={() => setMoreOpen(false)}>Cancel</Button>
            <Button variant="primary" loading={busy === "more"} onClick={makeMore}>
              Find more clips
            </Button>
          </>
        }
      >
        <div className="flex flex-col gap-4">
          <p className="text-sm text-ink-2">
            Finds the next-best moments in “{job.title}”. Moments already used are skipped, and the new clips are added to this video.
          </p>
          <Field label="How many more clips" hint="Between 1 and 10.">
            {(p) => (
              <input {...p} type="number" min={1} max={10} className="field num" value={moreCount} onChange={(e) => setMoreCount(Math.min(Math.max(Number(e.target.value) || 1, 1), 10))} />
            )}
          </Field>
          <Toggle
            label="Number them as parts"
            hint={`Adds “– Part ${(job.clip_count ?? 0) + 1}”, “– Part ${(job.clip_count ?? 0) + 2}” and so on to the titles, continuing from your ${job.clip_count ?? 0} existing ${job.clip_count === 1 ? "clip" : "clips"}.`}
            checked={labelPart}
            onChange={setLabelPart}
          />
          <p className="text-xs text-ink-3">The video is downloaded again if its copy was removed, so this can take a few minutes.</p>
        </div>
      </Dialog>
      <Dialog
        open={confirm}
        onClose={() => setConfirm(false)}
        title="Delete this video and its clips?"
        footer={
          <>
            <Button onClick={() => setConfirm(false)}>Keep</Button>
            <Button variant="danger" loading={busy === "delete"} onClick={() => act("delete", `/api/jobs/${job.id}`, "DELETE", "Deleted.")}>
              Delete
            </Button>
          </>
        }
      >
        <p className="text-sm text-ink-2">
          The downloaded source and {job.clip_count || 0} rendered {job.clip_count === 1 ? "clip" : "clips"} will be deleted. Videos already posted to YouTube stay there.
        </p>
      </Dialog>
    </li>
  );
}
