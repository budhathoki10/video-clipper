"use client";

import { Plus, RotateCw, Square, Trash2 } from "lucide-react";
import Link from "next/link";
import { useState } from "react";

import { api, formatDuration, timeAgo, type Job } from "@/lib/api";
import { hideLocally, isHidden, showAgain } from "@/lib/optimistic";
import { Badge, Button, Dialog, Field, Progress, Toggle, useToast } from "./ui";

const STATUS: Record<Job["status"], { label: string; tone: "neutral" | "accent" | "ok" | "warn" | "bad" }> = {
  queued: { label: "Queued", tone: "neutral" },
  running: { label: "Working", tone: "accent" },
  stopping: { label: "Stopping", tone: "warn" },
  done: { label: "Done", tone: "ok" },
  failed: { label: "Failed", tone: "bad" },
  cancelled: { label: "Cancelled", tone: "neutral" },
};

export function JobRow({ job: serverJob, onChange }: { job: Job; onChange: () => void }) {
  const toast = useToast();
  // What the screen shows right away, until the server's own answer catches up.
  const [expected, setExpected] = useState<{ from: Job["status"]; to: Job["status"] } | null>(null);
  const [removed, setRemoved] = useState(false);
  const job = expected && serverJob.status === expected.from ? { ...serverJob, status: expected.to } : serverJob;
  const [confirm, setConfirm] = useState(false);
  const [moreOpen, setMoreOpen] = useState(false);
  const [moreCount, setMoreCount] = useState(3);
  const [labelPart, setLabelPart] = useState(false);
  const more = job.more;
  const moreActive = more?.status === "queued" || more?.status === "running" || more?.status === "stopping";
  const canMakeMore = job.kind === "video" && job.status === "done" && (job.clip_count ?? 0) > 0 && !moreActive;
  const hasClips = (job.clip_count ?? 0) > 0;
  const active = job.status === "queued" || job.status === "running";
  const live = job.kind === "live";
  const status = live && job.status === "running" ? { label: "Live", tone: "bad" as const } : STATUS[job.status];

  /** Shows the result at once and does the work in the background; a failure puts things back. */
  async function act(name: string, path: string, method: string, done: string) {
    const change = name === "stop" ? { from: job.status, to: live ? ("stopping" as const) : ("cancelled" as const) }
      : name === "retry" ? { from: job.status, to: "queued" as const } : null;
    setConfirm(false);
    if (name === "delete") { hideLocally(job.id); setRemoved(true); }
    if (change) setExpected(change);
    toast(done);
    try {
      await api(path, { method });
    } catch (err) {
      if (name === "delete") { showAgain(job.id); setRemoved(false); }
      setExpected(null);
      toast(err instanceof Error ? err.message : "Something went wrong. Nothing was changed.", "bad");
    } finally {
      onChange();
    }
  }

  async function makeMore() {
    setMoreOpen(false);
    toast("Looking for more moments. This keeps going if you close the page.");
    try {
      await api(`/api/jobs/${job.id}/more`, { method: "POST", json: { count: moreCount, label_part: labelPart } });
    } catch (err) {
      toast(err instanceof Error ? err.message : "Could not start.", "bad");
    } finally {
      onChange();
    }
  }

  if (removed || isHidden(job.id)) return null;

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
          <Button onClick={() => act("stopMore", `/api/jobs/${more.id}/stop`, "POST", "Stopped looking for more clips.")}>
            <Square /> Cancel
          </Button>
        )}
        {canMakeMore && (
          <Button onClick={() => setMoreOpen(true)}>
            <Plus /> More clips
          </Button>
        )}
        {active && (
          <Button onClick={() => act("stop", `/api/jobs/${job.id}/stop`, "POST", live ? "Stopping the watch. Clips are kept." : "Job cancelled.")}>
            <Square /> {live ? "Stop watching" : "Cancel"}
          </Button>
        )}
        {(job.status === "failed" || job.status === "cancelled") && (
          <Button onClick={() => act("retry", `/api/jobs/${job.id}/retry`, "POST", "Job queued again.")}>
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
            <Button variant="primary" onClick={makeMore}>
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
        title={hasClips ? "Remove this video from the list?" : "Delete this video?"}
        footer={
          <>
            <Button onClick={() => setConfirm(false)}>Keep</Button>
            <Button variant={hasClips ? "primary" : "danger"} onClick={() => act("delete", `/api/jobs/${job.id}`, "DELETE", hasClips ? "Removed. Your clips are kept." : "Deleted.")}>
              {hasClips ? "Remove video" : "Delete"}
            </Button>
          </>
        }
      >
        <p className="text-sm text-ink-2">
          {hasClips
            ? `Only the video entry and its downloaded copy are removed. Its ${job.clip_count} ${job.clip_count === 1 ? "clip stays" : "clips stay"} in Clips and in storage. You can delete a clip from its own page.`
            : "This video made no clips. Its entry and any downloaded copy will be deleted."}
        </p>
      </Dialog>
    </li>
  );
}
