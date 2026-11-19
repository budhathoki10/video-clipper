"use client";

import { ArrowLeft, CalendarClock, Download, ExternalLink, Send, Trash2 } from "lucide-react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useState } from "react";

import { Badge, Button, Dialog, ErrorState, Field, Notice, Score, Section, SkeletonRows, Spinner, Toggle, useToast } from "@/components/ui";
import { API_URL, api, formatDuration, mediaUrl, useApi, type Clip, type Panel, type Settings, type SystemStatus } from "@/lib/api";

type Draft = {
  title: string;
  hook: string;
  description: string;
  start: number;
  end: number;
  aspect: Clip["format"]["aspect"];
  layout: Clip["format"]["layout"];
  quality: number;
  captions: boolean;
};

const toDraft = (clip: Clip): Draft => ({
  title: clip.title ?? "",
  hook: clip.hook ?? "",
  description: clip.description ?? "",
  start: clip.start,
  end: clip.end,
  aspect: clip.format.aspect ?? "9:16",
  layout: clip.format.layout ?? "auto",
  quality: clip.format.quality ?? 1080,
  captions: clip.captions.enabled ?? true,
});

type PostKind = "short" | "video";

const KINDS: { id: PostKind; label: string; hint: string }[] = [
  { id: "short", label: "Short", hint: "Vertical, up to 3 minutes. Shown in the Shorts feed." },
  { id: "video", label: "Video", hint: "A regular upload, best in 16:9. Shown in the main feed." },
];

function KindPicker({ value, onChange }: { value: PostKind; onChange: (kind: PostKind) => void }) {
  return (
    <div role="radiogroup" aria-label="Post as" className="flex flex-col gap-1.5">
      <span className="text-xs font-medium text-ink-2">Post as</span>
      <div className="grid grid-cols-2 gap-2">
        {KINDS.map((option) => {
          const active = option.id === value;
          return (
            <button
              key={option.id}
              type="button"
              role="radio"
              aria-checked={active}
              onClick={() => onChange(option.id)}
              className={`flex flex-col gap-0.5 rounded-md border px-3 py-2 text-left transition-colors duration-100 ${active ? "border-accent bg-accent-soft" : "border-line bg-bg hover:border-line-strong"}`}
            >
              <span className="text-sm font-medium">{option.label}</span>
              <span className="text-xs text-ink-3">{option.hint}</span>
            </button>
          );
        })}
      </div>
    </div>
  );
}

function PanelBreakdown({ panel }: { panel: Panel }) {
  const stats = [
    { label: "Average watched", value: `${Math.round(panel.watch)}%` },
    { label: "Watched to the end", value: `${Math.round(panel.finished)}%` },
    { label: "Would like", value: `${Math.round(panel.like)}%` },
    { label: "Would share", value: `${Math.round(panel.share)}%` },
  ];
  return (
    <div className="flex flex-col gap-4">
      <dl className="grid grid-cols-2 gap-px overflow-hidden rounded-md border border-line-subtle bg-line-subtle sm:grid-cols-4">
        {stats.map((stat) => (
          <div key={stat.label} className="bg-surface px-3 py-2">
            <dt className="text-xs text-ink-3">{stat.label}</dt>
            <dd className="num text-md font-semibold">{stat.value}</dd>
          </div>
        ))}
      </dl>
      <div>
        <h3 className="mb-2 text-xs font-medium text-ink-2">Watch time by viewer type</h3>
        <ul className="grid gap-x-6 gap-y-1.5 sm:grid-cols-2">
          {panel.by_type.map((group) => (
            <li key={group.type} className="flex items-center gap-2 text-xs">
              <span className="w-32 flex-none truncate text-ink-2">{group.type}</span>
              <span aria-hidden className="h-1 flex-1 overflow-hidden rounded-full bg-raised">
                <span className="block h-full rounded-full bg-ink-3" style={{ width: `${group.watch}%` }} />
              </span>
              <span className="num w-8 flex-none text-right text-ink">{group.watch}%</span>
            </li>
          ))}
        </ul>
      </div>
      {panel.comments.length > 0 && (
        <div>
          <h3 className="mb-2 text-xs font-medium text-ink-2">What they said</h3>
          <ul className="flex flex-col gap-1.5">
            {panel.comments.map((comment, i) => (
              <li key={i} className="text-sm">
                <span className="text-ink">“{comment.text}”</span> <span className="text-xs text-ink-3">{comment.type}, watched {comment.watch}%</span>
              </li>
            ))}
          </ul>
        </div>
      )}
      <p className="text-xs text-ink-3">
        A simulation: {panel.viewers} AI personas read the clip&apos;s transcript and title. Use it to compare clips, not as a view forecast.
      </p>
    </div>
  );
}

function ClipDetail() {
  const id = useSearchParams().get("id");
  const router = useRouter();
  const toast = useToast();
  const { data: clip, error, loading, reload, setData } = useApi<Clip>(id ? `/api/clips/${id}` : null, 3000);
  const system = useApi<SystemStatus>("/api/system");
  const [draft, setDraft] = useState<Draft | null>(null);
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState<"save" | "render" | null>(null);
  const [formError, setFormError] = useState<string | null>(null);
  const [postOpen, setPostOpen] = useState<"now" | "schedule" | null>(null);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [when, setWhen] = useState("");
  const [privacy, setPrivacy] = useState("private");
  const [kind, setKind] = useState<PostKind | null>(null);
  const [posting, setPosting] = useState(false);
  const [reformatting, setReformatting] = useState(false);
  const [hookIdeas, setHookIdeas] = useState<string[]>([]);
  const [suggesting, setSuggesting] = useState(false);
  const defaults = useApi<{ settings: Settings }>("/api/settings");
  const defaultPrivacy = defaults.data?.settings.posting.privacy;

  useEffect(() => {
    if (defaultPrivacy) setPrivacy(defaultPrivacy);
  }, [defaultPrivacy]);

  useEffect(() => {
    if (clip && !dirty) setDraft(toDraft(clip));
  }, [clip, dirty]);

  if (!id) return <ErrorState message="No clip was selected. Open one from the Clips page." />;
  if (error && !clip) return <ErrorState message={error} onRetry={reload} />;
  if (loading || !clip || !draft) return <SkeletonRows rows={6} height="h-12" />;

  const edit = <K extends keyof Draft>(key: K, value: Draft[K]) => {
    setDraft((d) => (d ? { ...d, [key]: value } : d));
    setDirty(true);
    setFormError(null);
  };
  const needsRender =
    draft.start !== clip.start || draft.end !== clip.end || draft.hook !== (clip.hook ?? "") || draft.aspect !== clip.format.aspect ||
    draft.layout !== clip.format.layout || draft.quality !== clip.format.quality || draft.captions !== clip.captions.enabled;
  const length = draft.end - draft.start;
  const connected = system.data?.youtube.connected ?? false;

  async function save() {
    if (!draft || !clip) return;
    if (!draft.title.trim()) return setFormError("Give the clip a title.");
    if (length < 3) return setFormError("The clip must be at least 3 seconds long. Move the end later.");
    if (length > 180) return setFormError("The clip can be at most 3 minutes long.");
    setSaving(needsRender ? "render" : "save");
    try {
      const updated = await api<Clip>(`/api/clips/${clip.id}`, {
        method: "PATCH",
        json: {
          title: draft.title.trim(), hook: draft.hook.trim(), description: draft.description, start: draft.start, end: draft.end,
          settings: { format: { aspect: draft.aspect, layout: draft.layout, quality: draft.quality }, captions: { enabled: draft.captions } },
          rerender: needsRender, rescore: needsRender,
        },
      });
      setData(updated);
      setDirty(false);
      toast(needsRender ? "Saved. Rendering the new version." : "Changes saved.");
    } catch (err) {
      setFormError(err instanceof Error ? err.message : "Could not save.");
    } finally {
      setSaving(null);
    }
  }

  async function post() {
    if (!clip || !postOpen) return;
    setPosting(true);
    try {
      await api("/api/posts", {
        method: "POST",
        json: { clip_id: clip.id, mode: postOpen, kind: chosenKind, privacy, publish_at: postOpen === "schedule" ? new Date(when).toISOString() : null, title: draft?.title, description: draft?.description },
      });
      toast(postOpen === "schedule" ? "Scheduled. Uploading to YouTube now." : "Posting. Uploading to YouTube now.");
      setPostOpen(null);
      void reload();
    } catch (err) {
      toast(err instanceof Error ? err.message : "Could not post.", "bad");
    } finally {
      setPosting(false);
    }
  }

  async function remove() {
    if (!clip) return;
    try {
      await api(`/api/clips/${clip.id}`, { method: "DELETE" });
      toast("Clip deleted.");
      router.push("/clips/");
    } catch (err) {
      toast(err instanceof Error ? err.message : "Could not delete.", "bad");
    }
  }

  // YouTube decides: a vertical or square clip of up to 3 minutes is a Short.
  const knownShape = clip.width != null && clip.height != null;
  const actualKind: PostKind = !knownShape || (clip.height! >= clip.width! && clip.duration <= 180) ? "short" : "video";
  const chosenKind: PostKind = kind ?? actualKind;
  const kindMismatch = chosenKind !== actualKind;
  const tooLongForShort = chosenKind === "short" && clip.duration > 180;

  async function suggestHooks() {
    if (!clip) return;
    setSuggesting(true);
    try {
      const result = await api<{ hooks: string[] }>(`/api/clips/${clip.id}/hooks`, { method: "POST" });
      setHookIdeas(result.hooks);
    } catch (err) {
      toast(err instanceof Error ? err.message : "Could not write hooks.", "bad");
    } finally {
      setSuggesting(false);
    }
  }

  async function reformat() {
    if (!clip) return;
    setReformatting(true);
    try {
      await api<Clip>(`/api/clips/${clip.id}`, {
        method: "PATCH",
        json: { settings: { format: { aspect: chosenKind === "short" ? "9:16" : "16:9" } }, rerender: true, rescore: false },
      });
      toast(chosenKind === "short" ? "Rendering as a 9:16 Short. Post it when it is ready." : "Rendering as a 16:9 video. Post it when it is ready.");
      setPostOpen(null);
      setKind(null);
      void reload();
    } catch (err) {
      toast(err instanceof Error ? err.message : "Could not start rendering.", "bad");
    } finally {
      setReformatting(false);
    }
  }

  const minWhen = new Date(Date.now() + 10 * 60000 - new Date().getTimezoneOffset() * 60000).toISOString().slice(0, 16);
  const vertical = (clip.height ?? 16) >= (clip.width ?? 9);

  return (
    <>
      <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
        <Link href={`/clips/?job=${clip.job_id}`} className="btn btn-ghost -ml-2">
          <ArrowLeft /> Clips
        </Link>
        <div className="flex flex-wrap items-center gap-2">
          {clip.file_url && (
            <a href={`${API_URL}/api/clips/${clip.id}/download`} className="btn btn-secondary">
              <Download /> Download
            </a>
          )}
          <Button disabled={clip.status !== "ready"} onClick={() => setPostOpen("schedule")}>
            <CalendarClock /> Schedule
          </Button>
          <Button variant="primary" disabled={clip.status !== "ready"} onClick={() => setPostOpen("now")}>
            <Send /> Post now
          </Button>
        </div>
      </div>

      <div className="grid gap-6 lg:grid-cols-[minmax(0,340px)_minmax(0,1fr)]">
        <div className="flex flex-col gap-3">
          <div className={`overflow-hidden rounded-lg border border-line bg-ink shadow-soft ${vertical ? "mx-auto w-full max-w-[340px]" : "w-full"}`} style={{ aspectRatio: `${clip.width ?? 9} / ${clip.height ?? 16}` }}>
            {clip.status === "ready" && clip.file_url ? (
              <video key={clip.file_url} src={mediaUrl(clip.file_url)} poster={mediaUrl(clip.thumb_url)} controls playsInline preload="metadata" className="size-full" aria-label={`Preview of ${clip.title}`} />
            ) : clip.status === "failed" ? (
              <div className="flex size-full items-center justify-center p-4 text-center text-sm text-bad-ink">{clip.error || "Render failed."}</div>
            ) : (
              <div role="status" className="flex size-full flex-col items-center justify-center gap-2 text-sm text-ink-2">
                <Spinner className="size-5" />
                Rendering this clip
              </div>
            )}
          </div>
          <dl className="panel grid grid-cols-3 divide-x divide-line-subtle text-center">
            <div className="px-2 py-2.5">
              <dt className="text-xs text-ink-3">Panel score</dt>
              <dd><Score value={clip.panel_score} size="lg" /></dd>
            </div>
            <div className="px-2 py-2.5">
              <dt className="text-xs text-ink-3">Editor score</dt>
              <dd><Score value={clip.ai_score} size="lg" /></dd>
            </div>
            <div className="px-2 py-2.5">
              <dt className="text-xs text-ink-3">Length</dt>
              <dd className="num text-xl font-semibold tracking-tight">{formatDuration(clip.duration)}</dd>
            </div>
          </dl>
          {clip.reason && <p className="text-sm text-ink-2">{clip.reason}</p>}
          {clip.source_url && (
            <a href={clip.source_url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1.5 text-xs text-ink-3 hover:text-ink">
              <ExternalLink aria-hidden className="size-3" />
              <span className="truncate">From “{clip.source_title}” by {clip.source_channel}</span>
            </a>
          )}
        </div>

        <div className="flex min-w-0 flex-col gap-4">
          {!connected && system.data && (
            <Notice tone="accent">
              To post from here, <Link href="/settings/#youtube" className="underline underline-offset-2">connect your YouTube channel</Link>. Until then, download the clip and upload it by hand.
            </Notice>
          )}
          {(clip.posts ?? []).length > 0 && (
            <ul className="panel divide-y divide-line-subtle">
              {clip.posts!.map((p) => (
                <li key={p.id} className="flex flex-wrap items-center gap-2 px-4 py-2.5 text-sm">
                  <Badge tone={p.status === "failed" ? "bad" : p.status === "published" ? "ok" : "accent"}>{p.status[0].toUpperCase() + p.status.slice(1)}</Badge>
                  <span className="min-w-0 flex-1 truncate text-ink-2">
                    {p.status === "failed" ? p.error : p.publish_at ? `Goes public ${new Date(p.publish_at).toLocaleString()}` : `Posted as ${p.privacy}`}
                  </span>
                  {p.youtube_id && (
                    <a href={`https://youtu.be/${p.youtube_id}`} target="_blank" rel="noreferrer" className="btn btn-ghost">
                      Open on YouTube <ExternalLink />
                    </a>
                  )}
                </li>
              ))}
            </ul>
          )}

          <Section title="Details" description="Title and description are used when you post. Changing the hook, trim or format renders the clip again.">
            <div className="flex flex-col gap-4">
              <Field label="Title" hint={`${draft.title.length} of 100 characters`}>
                {(p) => <input {...p} className="field" maxLength={100} value={draft.title} onChange={(e) => edit("title", e.target.value)} />}
              </Field>
              <div className="flex flex-col gap-2">
                <Field label="On-screen hook" hint="The line over the first seconds that makes people stop scrolling. Leave empty for none.">
                  {(p) => (
                    <div className="flex gap-2">
                      <input {...p} className="field" maxLength={60} value={draft.hook} onChange={(e) => edit("hook", e.target.value)} />
                      <Button loading={suggesting} onClick={suggestHooks}>Suggest hooks</Button>
                    </div>
                  )}
                </Field>
                {hookIdeas.length > 0 && (
                  <div role="group" aria-label="Suggested hooks" className="animate-enter flex flex-wrap gap-1.5">
                    {hookIdeas.map((idea) => (
                      <button
                        key={idea}
                        type="button"
                        onClick={() => edit("hook", idea)}
                        className={`rounded-sm border px-2 py-1 text-sm transition-colors duration-100 ${draft.hook === idea ? "border-accent bg-accent-soft" : "border-line bg-raised hover:border-line-strong"}`}
                      >
                        {idea}
                      </button>
                    ))}
                  </div>
                )}
              </div>
              <Field label="Description">
                {(p) => <textarea {...p} rows={5} className="field" value={draft.description} onChange={(e) => edit("description", e.target.value)} />}
              </Field>
              <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
                <Field label="Start (seconds)">
                  {(p) => <input {...p} type="number" step={0.5} min={0} className="field num" value={draft.start} onChange={(e) => edit("start", Number(e.target.value))} />}
                </Field>
                <Field label="End (seconds)" hint={`${formatDuration(Math.max(length, 0))} long`}>
                  {(p) => <input {...p} type="number" step={0.5} min={0} className="field num" value={draft.end} onChange={(e) => edit("end", Number(e.target.value))} />}
                </Field>
                <Field label="Screen format">
                  {(p) => (
                    <select {...p} className="field" value={draft.aspect} onChange={(e) => edit("aspect", e.target.value as Draft["aspect"])}>
                      <option value="9:16">9:16 Shorts</option>
                      <option value="16:9">16:9 Video</option>
                      <option value="1:1">1:1 Square</option>
                    </select>
                  )}
                </Field>
                <Field label="Quality">
                  {(p) => (
                    <select {...p} className="field" value={draft.quality} onChange={(e) => edit("quality", Number(e.target.value))}>
                      <option value={720}>720p</option>
                      <option value={1080}>1080p</option>
                      <option value={1440}>1440p</option>
                      <option value={2160}>4K</option>
                    </select>
                  )}
                </Field>
              </div>
              <div className="grid gap-3 sm:grid-cols-2">
                <Field label="Layout" hint="Applies when the source shape differs from the format.">
                  {(p) => (
                    <select {...p} className="field" value={draft.layout} onChange={(e) => edit("layout", e.target.value as Draft["layout"])}>
                      <option value="auto">Automatic: creator's face on top when there is a webcam</option>
                      <option value="blur">Whole frame on a blurred background</option>
                      <option value="crop">Crop to fill the screen</option>
                    </select>
                  )}
                </Field>
                <div className="flex items-end pb-1.5">
                  <div className="w-full"><Toggle label="Captions" checked={draft.captions} onChange={(v) => edit("captions", v)} /></div>
                </div>
              </div>
              {formError && <p role="alert" className="text-sm text-bad-ink">{formError}</p>}
              <div className="flex flex-wrap items-center justify-between gap-2 border-t border-line-subtle pt-4">
                <Button variant="danger" onClick={() => setDeleteOpen(true)}>
                  <Trash2 /> Delete clip
                </Button>
                <div className="flex items-center gap-2">
                  {dirty && (
                    <Button variant="ghost" onClick={() => { setDraft(toDraft(clip)); setDirty(false); setFormError(null); }}>
                      Discard
                    </Button>
                  )}
                  <Button variant="primary" disabled={!dirty || clip.status === "rendering"} loading={saving !== null} onClick={save}>
                    {needsRender ? "Save and render" : "Save changes"}
                  </Button>
                </div>
              </div>
            </div>
          </Section>

          <Section title="Audience panel" description="How simulated viewers reacted.">
            {clip.panel ? <PanelBreakdown panel={clip.panel} /> : <p className="text-sm text-ink-2">{clip.status === "rendering" ? "The panel runs once the clip has rendered." : "This clip has no panel result. Turn the panel on in Settings, then save a change that renders the clip again."}</p>}
          </Section>
        </div>
      </div>

      <Dialog
        open={postOpen !== null}
        onClose={() => { setPostOpen(null); setKind(null); }}
        title={postOpen === "schedule" ? "Schedule on YouTube" : "Post to YouTube now"}
        footer={
          <>
            <Button onClick={() => { setPostOpen(null); setKind(null); }}>Cancel</Button>
            <Button variant="primary" loading={posting} disabled={!connected || kindMismatch || tooLongForShort || (postOpen === "schedule" && !when)} onClick={post}>
              {postOpen === "schedule" ? "Schedule" : "Post now"}
            </Button>
          </>
        }
      >
        {!connected ? (
          <p className="text-sm text-ink-2">
            No YouTube channel is connected. <Link href="/settings/#youtube" className="text-accent underline underline-offset-2">Connect one in Settings</Link>, then come back.
          </p>
        ) : (
          <div className="flex flex-col gap-4">
            <p className="text-sm text-ink-2">“{draft.title}” will be uploaded to {system.data?.youtube.channel || "your channel"}.</p>
            <KindPicker value={chosenKind} onChange={setKind} />
            {tooLongForShort ? (
              <Notice>A Short can be at most 3 minutes, and this clip is {formatDuration(clip.duration)}. Shorten it under Details, or post it as a video.</Notice>
            ) : kindMismatch ? (
              <div className="flex flex-col items-start gap-2">
                <Notice>
                  {chosenKind === "short"
                    ? "This clip is horizontal, so YouTube would publish it as a regular video. Render it as 9:16 to post it as a Short."
                    : "This clip is vertical and under 3 minutes, so YouTube would publish it as a Short. Render it as 16:9 to post it as a regular video."}
                </Notice>
                <Button loading={reformatting} onClick={reformat}>
                  {chosenKind === "short" ? "Render as 9:16 Short" : "Render as 16:9 video"}
                </Button>
              </div>
            ) : null}
            {postOpen === "schedule" ? (
              <Field label="Goes public at" hint="Your local time. YouTube publishes it for you, even if you are offline.">
                {(p) => <input {...p} type="datetime-local" className="field" min={minWhen} value={when} onChange={(e) => setWhen(e.target.value)} />}
              </Field>
            ) : (
              <Field label="Visibility">
                {(p) => (
                  <select {...p} className="field" value={privacy} onChange={(e) => setPrivacy(e.target.value)}>
                    <option value="public">Public</option>
                    <option value="unlisted">Unlisted</option>
                    <option value="private">Private</option>
                  </select>
                )}
              </Field>
            )}
          </div>
        )}
      </Dialog>

      <Dialog
        open={deleteOpen}
        onClose={() => setDeleteOpen(false)}
        title="Delete this clip?"
        footer={
          <>
            <Button onClick={() => setDeleteOpen(false)}>Keep</Button>
            <Button variant="danger" onClick={remove}>Delete</Button>
          </>
        }
      >
        <p className="text-sm text-ink-2">The rendered clip is deleted. Any copy already posted to YouTube stays there.</p>
      </Dialog>
    </>
  );
}

export default function ClipPage() {
  return (
    <Suspense fallback={<SkeletonRows rows={6} height="h-12" />}>
      <ClipDetail />
    </Suspense>
  );
}
