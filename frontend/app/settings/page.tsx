"use client";

import { ChevronDown, KeyRound } from "lucide-react";
import { useEffect, useState, type ReactNode } from "react";

import { CaptionPreview } from "@/components/caption-preview";
import { Badge, Button, ErrorState, Field, Notice, PageHeader, SkeletonRows, Toggle, cx, useToast } from "@/components/ui";
import { api, useApi, type Provider, type SecretStatus, type Settings, type YouTubeStatus } from "@/lib/api";

const TABS = [
  { id: "clips", label: "Clips", blurb: "How many clips to cut and how long they may be." },
  { id: "format", label: "Format", blurb: "Screen shape, quality and layout of every clip." },
  { id: "captions", label: "Captions", blurb: "How the words on screen look." },
  { id: "ai", label: "AI engine", blurb: "Picks the moments, writes titles and plays the audience panel." },
  { id: "live", label: "Live streams", blurb: "How eagerly to clip while a stream is running." },
  { id: "posting", label: "Posting", blurb: "Your YouTube channel and defaults for new posts." },
  { id: "trends", label: "Trends", blurb: "What Clipper reads on YouTube to learn what is getting views." },
] as const;
type TabId = (typeof TABS)[number]["id"];

const PROVIDERS: { id: Provider; name: string; secret: string | null; note: string }[] = [
  { id: "nvidia", name: "NVIDIA Nemotron Ultra", secret: "NVIDIA_API_KEY", note: "Default. Hosted by NVIDIA." },
  { id: "anthropic", name: "Claude", secret: "ANTHROPIC_API_KEY", note: "Anthropic API." },
  { id: "gemini", name: "Gemini", secret: "GEMINI_API_KEY", note: "Google AI Studio key." },
  { id: "ollama", name: "Ollama (local)", secret: null, note: "Runs on this PC. No key, lower quality." },
];
const FONTS = ["Poppins ExtraBold", "Anton", "Bangers", "Arial Black", "Impact"];
const QUALITIES = [[720, "720p"], [1080, "1080p"], [1440, "1440p"], [2160, "4K"]] as const;
const GRID = "grid grid-cols-2 gap-x-3 gap-y-4 md:grid-cols-4";

function YouTubeMark() {
  return (
    <svg viewBox="0 0 28 20" aria-hidden className="!h-[14px] !w-5">
      <rect width="28" height="20" rx="5" fill="#FF0033" />
      <path d="M11.5 5.8v8.4L18.6 10z" fill="#fff" />
    </svg>
  );
}

function Row({ children }: { children: ReactNode }) {
  return <div className="border-t border-line-subtle pt-4 first:border-t-0 first:pt-0">{children}</div>;
}

function ApiKey({ secret, status, onSaved, label = "API key" }: { secret: string; status?: { set: boolean; hint: string }; onSaved: (s: SecretStatus) => void; label?: string }) {
  const toast = useToast();
  const [value, setValue] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function save() {
    if (value.trim().length < 12) return setError("That key looks too short. Paste the whole key.");
    setSaving(true);
    try {
      onSaved(await api<SecretStatus>("/api/secrets", { method: "PUT", json: { name: secret, value: value.trim() } }));
      setValue("");
      toast("Key saved.");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not save the key.");
    } finally {
      setSaving(false);
    }
  }

  return (
    <Field
      label={label}
      error={error}
      hint={status?.set ? `A key ending in ${status.hint.replace("…", "")} is saved. It is never shown again.` : "No key saved yet. It is stored securely and never shown again."}
    >
      {(p) => (
        <div className="flex gap-2">
          <input {...p} type="password" autoComplete="off" spellCheck={false} className="field font-mono" placeholder={status?.set ? "Paste a new key to replace it" : "Paste your key"} value={value} onChange={(e) => { setValue(e.target.value); setError(null); }} />
          <Button loading={saving} disabled={!value} onClick={save}>
            <KeyRound /> {status?.set ? "Replace key" : "Save key"}
          </Button>
        </div>
      )}
    </Field>
  );
}

function YouTubeConnect() {
  const toast = useToast();
  const yt = useApi<YouTubeStatus>("/api/youtube", 2500);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [setupOpen, setSetupOpen] = useState(false);
  const status = yt.data;

  async function run(name: string, path: string, body?: unknown, done?: string) {
    setBusy(name);
    setError(null);
    try {
      await api(path, { method: "POST", json: body });
      if (done) toast(done);
      await yt.reload();
      return true;
    } catch (err) {
      setError(err instanceof Error ? err.message : "Something went wrong.");
      return false;
    } finally {
      setBusy(null);
    }
  }

  if (!status) return <SkeletonRows rows={2} />;
  if (status.connected) {
    return (
      <div className="flex flex-wrap items-center justify-between gap-3 rounded-md border border-line-subtle bg-bg px-3 py-2.5">
        <div className="flex min-w-0 items-center gap-2.5 text-sm">
          <YouTubeMark />
          <span className="truncate font-medium">{status.channel || "Your channel"}</span>
          <Badge tone="ok">Connected</Badge>
        </div>
        <Button variant="danger" loading={busy === "disconnect"} onClick={() => run("disconnect", "/api/youtube/disconnect", undefined, "Channel disconnected.")}>
          Disconnect
        </Button>
      </div>
    );
  }

  const waiting = busy === "connect" || status.connecting;
  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center gap-3">
        <Button
          size="lg"
          loading={waiting}
          onClick={async () => {
            if (!status.client_configured) {
              setSetupOpen(true);
              setError("Google OAuth is not configured. Add GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET to backend/.env, then restart Clipper.");
              return;
            }
            await run("connect", "/api/youtube/connect");
          }}
        >
          {!waiting && <YouTubeMark />} {waiting ? "Waiting for Google sign-in" : "Connect YouTube"}
        </Button>
        <p className="text-xs text-ink-3">Opens Google in your browser. Choose your channel and allow uploads.</p>
      </div>
      {waiting && (
        <div className="flex flex-col gap-2 text-sm text-ink-2">
          <p>
            Finish the Google page that opened. If it says the app is not verified, choose Advanced, then continue. This page updates by itself.
          </p>
          {status.auth_url ? (
            <p>
              Page not on screen?{" "}
              <a href={status.auth_url} target="_blank" rel="noreferrer" className="text-accent underline underline-offset-2">
                Open the Google sign-in here
              </a>
              .
            </p>
          ) : (
            <p className="text-ink-3">Preparing the Google page…</p>
          )}
          <p className="text-xs text-ink-3">
            If Google says “Access blocked”, the channel&apos;s account is not a test user yet: add it under Google Auth Platform → Audience → Test users, then try again.
          </p>
        </div>
      )}
      {(error || status.error) && !waiting && <p role="alert" className="text-sm text-bad-ink">{error || status.error}</p>}

      <div className="rounded-md border border-line-subtle">
        <button type="button" aria-expanded={setupOpen} aria-controls="yt-setup" onClick={() => setSetupOpen((v) => !v)} className="flex w-full items-center justify-between gap-2 px-3 py-2 text-left text-sm text-ink-2 hover:text-ink">
          <span>
            One-time setup for the app owner{" "}
            {status.client_configured && <span className="text-ok-ink">· done</span>}
          </span>
          <ChevronDown aria-hidden className={cx("size-4 transition-transform duration-150", setupOpen && "rotate-180")} />
        </button>
        {setupOpen && (
          <div id="yt-setup" className="animate-enter flex flex-col gap-3 border-t border-line-subtle p-3">
            <p className="text-sm text-ink-2">
              Google requires every app that uploads to YouTube to be registered once. Whoever runs this copy of Clipper does this a single time; everyone using it afterwards only presses Connect YouTube.
            </p>
            <ol className="flex list-decimal flex-col gap-1 pl-5 text-sm text-ink-2">
              <li>In console.cloud.google.com, enable “YouTube Data API v3” for a project.</li>
              <li>Under Google Auth Platform → Audience, add the channel&apos;s Google account as a test user.</li>
              <li>Under Clients, create a client of type “Desktop app”.</li>
              <li>Copy its client ID and client secret into GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in backend/.env.</li>
              <li>Restart Clipper, then press Connect YouTube.</li>
            </ol>
            <p className="text-xs leading-5 text-ink-3">For security, these values are read only from backend/.env and are never returned by the API.</p>
            <Notice>Until Google has audited the project, videos uploaded by the app stay private and daily uploads are limited. You can make them public in YouTube Studio.</Notice>
          </div>
        )}
      </div>
    </div>
  );
}

export default function SettingsPage() {
  const toast = useToast();
  const remote = useApi<{ settings: Settings; secrets: SecretStatus }>("/api/settings");
  const [form, setForm] = useState<Settings | null>(null);
  const [secrets, setSecrets] = useState<SecretStatus>({});
  const [tab, setTab] = useState<TabId>("clips");
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<{ ok: boolean; text: string } | null>(null);

  useEffect(() => {
    if (remote.data && !dirty) {
      setForm(remote.data.settings);
      setSecrets(remote.data.secrets);
    }
  }, [remote.data, dirty]);

  useEffect(() => {
    const hash = window.location.hash.slice(1);
    if (hash === "youtube") setTab("posting");
    else if (TABS.some((t) => t.id === hash)) setTab(hash as TabId);
  }, []);

  useEffect(() => {
    if (!dirty) return;
    const warn = (e: BeforeUnloadEvent) => e.preventDefault();
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);

  if (remote.error && !form) return <ErrorState message={remote.error} onRetry={remote.reload} />;
  if (!form) return <SkeletonRows rows={8} height="h-14" />;

  function set<S extends keyof Settings, K extends keyof Settings[S]>(section: S, key: K, value: Settings[S][K]) {
    setForm((f) => (f ? { ...f, [section]: { ...f[section], [key]: value } } : f));
    setDirty(true);
    setError(null);
  }
  const num = (v: string, fallback: number) => (Number.isFinite(Number(v)) && v !== "" ? Number(v) : fallback);

  async function save() {
    if (!form) return;
    if (form.clip.min_sec > form.clip.max_sec) {
      setTab("clips");
      return setError("Shortest clip cannot be longer than the longest clip.");
    }
    setSaving(true);
    try {
      const result = await api<{ settings: Settings; secrets: SecretStatus }>("/api/settings", { method: "PUT", json: form });
      setForm(result.settings);
      setDirty(false);
      toast("Settings saved. They apply to the next video.");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not save settings.");
    } finally {
      setSaving(false);
    }
  }

  async function testAi() {
    setTesting(true);
    setTestResult(null);
    try {
      const r = await api<{ ok: boolean; model: string; seconds: number }>("/api/ai/test", { method: "POST" });
      setTestResult({ ok: true, text: `${r.model} answered in ${r.seconds} s.` });
    } catch (err) {
      setTestResult({ ok: false, text: err instanceof Error ? err.message : "The test failed." });
    } finally {
      setTesting(false);
    }
  }

  const provider = PROVIDERS.find((p) => p.id === form.ai.provider) ?? PROVIDERS[0];
  const active = TABS.find((t) => t.id === tab)!;
  const qualitySelect = (label: string, value: number, onChange: (v: number) => void, hint?: string) => (
    <Field label={label} hint={hint}>
      {(p) => (
        <select {...p} className="field" value={value} onChange={(e) => onChange(Number(e.target.value))}>
          {QUALITIES.map(([v, name]) => <option key={v} value={v}>{name}</option>)}
        </select>
      )}
    </Field>
  );

  return (
    <>
      <PageHeader title="Settings" description="Defaults for every new video and live stream. Clips already made keep the settings they were rendered with." />
      <div className="grid gap-4 pb-16 md:grid-cols-[176px_minmax(0,1fr)] md:gap-8">
        <div role="tablist" aria-label="Settings sections" aria-orientation="vertical" className="-mx-4 flex gap-1 overflow-x-auto px-4 md:mx-0 md:flex-col md:overflow-visible md:px-0">
          {TABS.map((t) => (
            <button
              key={t.id}
              role="tab"
              type="button"
              id={`tab-${t.id}`}
              aria-selected={tab === t.id}
              aria-controls="settings-panel"
              onClick={() => { setTab(t.id); window.history.replaceState(null, "", `#${t.id}`); }}
              className={cx(
                "h-8 flex-none rounded-md px-2.5 text-left text-sm whitespace-nowrap transition-colors duration-100",
                tab === t.id ? "bg-hover font-medium text-ink" : "text-ink-2 hover:bg-raised hover:text-ink",
              )}
            >
              {t.label}
            </button>
          ))}
        </div>

        <section id="settings-panel" role="tabpanel" aria-labelledby={`tab-${tab}`} className="panel max-w-3xl min-w-0">
          <div className="border-b border-line-subtle px-4 py-3">
            <h2 className="text-sm font-medium">{active.label}</h2>
            <p className="mt-0.5 text-xs text-ink-3">{active.blurb}</p>
          </div>
          <div className="flex flex-col gap-4 p-4">
            {tab === "clips" && (
              <div className={GRID}>
                <Field label="Clips per video">{(p) => <input {...p} type="number" min={1} max={20} className="field num" value={form.clip.max_clips} onChange={(e) => set("clip", "max_clips", num(e.target.value, 5))} />}</Field>
                <Field label="Shortest (seconds)">{(p) => <input {...p} type="number" min={5} max={180} className="field num" value={form.clip.min_sec} onChange={(e) => set("clip", "min_sec", num(e.target.value, 20))} />}</Field>
                <Field label="Longest (seconds)">{(p) => <input {...p} type="number" min={5} max={180} className="field num" value={form.clip.max_sec} onChange={(e) => set("clip", "max_sec", num(e.target.value, 60))} />}</Field>
                <Field label="Minimum AI score" hint="0 keeps every pick.">{(p) => <input {...p} type="number" min={0} max={100} className="field num" value={form.clip.min_score} onChange={(e) => set("clip", "min_score", num(e.target.value, 0))} />}</Field>
              </div>
            )}

            {tab === "format" && (
              <div className={GRID}>
                <Field label="Screen format">
                  {(p) => (
                    <select {...p} className="field" value={form.format.aspect} onChange={(e) => set("format", "aspect", e.target.value as Settings["format"]["aspect"])}>
                      <option value="9:16">9:16 Shorts</option>
                      <option value="16:9">16:9 Video</option>
                      <option value="1:1">1:1 Square</option>
                    </select>
                  )}
                </Field>
                {qualitySelect("Output quality", form.format.quality, (v) => set("format", "quality", v))}
                <Field label="Frame rate">
                  {(p) => (
                    <select {...p} className="field" value={form.format.fps} onChange={(e) => set("format", "fps", Number(e.target.value))}>
                      <option value={30}>30 fps</option>
                      <option value={60}>60 fps</option>
                    </select>
                  )}
                </Field>
                {qualitySelect("Download quality", form.format.source_quality, (v) => set("format", "source_quality", v), "Higher uses more disk.")}
                <Field label="Layout" className="col-span-2">
                  {(p) => (
                    <select {...p} className="field" value={form.format.layout} onChange={(e) => set("format", "layout", e.target.value as Settings["format"]["layout"])}>
                      <option value="blur">Whole frame on a blurred background</option>
                      <option value="crop">Crop to fill the screen</option>
                    </select>
                  )}
                </Field>
                <Field label="Zoom on blurred layout" hint="1 shows the full width; higher crops the sides." className="col-span-2">
                  {(p) => <input {...p} type="number" min={1} max={2} step={0.05} className="field num" disabled={form.format.layout !== "blur"} value={form.format.zoom} onChange={(e) => set("format", "zoom", num(e.target.value, 1.35))} />}
                </Field>
              </div>
            )}

            {tab === "captions" && (
              <div className="grid gap-6 sm:grid-cols-[minmax(0,1fr)_168px]">
                <div className="flex flex-col gap-4">
                  <Toggle label="Burn captions into clips" hint="Word by word, with the spoken word highlighted." checked={form.captions.enabled} onChange={(v) => set("captions", "enabled", v)} />
                  <div className="grid grid-cols-2 gap-x-3 gap-y-4">
                    <Field label="Font">
                      {(p) => (
                        <select {...p} className="field" value={form.captions.font} onChange={(e) => set("captions", "font", e.target.value)}>
                          {FONTS.map((font) => <option key={font}>{font}</option>)}
                        </select>
                      )}
                    </Field>
                    <Field label="Size">
                      {(p) => (
                        <select {...p} className="field" value={form.captions.size} onChange={(e) => set("captions", "size", e.target.value as Settings["captions"]["size"])}>
                          <option value="small">Small</option>
                          <option value="medium">Medium</option>
                          <option value="large">Large</option>
                        </select>
                      )}
                    </Field>
                    <Field label="Position">
                      {(p) => (
                        <select {...p} className="field" value={form.captions.position} onChange={(e) => set("captions", "position", e.target.value as Settings["captions"]["position"])}>
                          <option value="lower">Lower third</option>
                          <option value="middle">Middle</option>
                        </select>
                      )}
                    </Field>
                    <Field label="Words on screen">{(p) => <input {...p} type="number" min={1} max={6} className="field num" value={form.captions.words_per_line} onChange={(e) => set("captions", "words_per_line", num(e.target.value, 3))} />}</Field>
                    <Field label="Text colour">{(p) => <input {...p} type="color" className="field p-1" value={form.captions.color} onChange={(e) => set("captions", "color", e.target.value)} />}</Field>
                    <Field label="Highlight colour">{(p) => <input {...p} type="color" className="field p-1" value={form.captions.highlight} onChange={(e) => set("captions", "highlight", e.target.value)} />}</Field>
                    <Field label="Spoken language" hint="Leave empty to detect it. Or a code: en, es, hi." className="col-span-2">
                      {(p) => <input {...p} className="field" placeholder="Detect automatically" value={form.captions.language === "auto" ? "" : form.captions.language} onChange={(e) => set("captions", "language", e.target.value.trim().toLowerCase() || "auto")} />}
                    </Field>
                  </div>
                  <Row><Toggle label="Capital letters" checked={form.captions.uppercase} onChange={(v) => set("captions", "uppercase", v)} /></Row>
                  <Row><Toggle label="Show the hook text at the top" checked={form.overlay.hook_title} onChange={(v) => set("overlay", "hook_title", v)} /></Row>
                  <Row><Toggle label="Show a credit to the source channel" hint="Recommended when clipping other creators." checked={form.overlay.credit} onChange={(v) => set("overlay", "credit", v)} /></Row>
                </div>
                <CaptionPreview captions={form.captions} overlay={form.overlay} />
              </div>
            )}

            {tab === "ai" && (
              <>
                <div className="grid gap-3 sm:grid-cols-2">
                  <Field label="Provider" hint={provider.note}>
                    {(p) => (
                      <select {...p} className="field" value={form.ai.provider} onChange={(e) => { set("ai", "provider", e.target.value as Provider); setTestResult(null); }}>
                        {PROVIDERS.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
                      </select>
                    )}
                  </Field>
                  <Field label="Model">
                    {(p) => <input {...p} className="field font-mono" spellCheck={false} value={form.ai.models[form.ai.provider]} onChange={(e) => set("ai", "models", { ...form.ai.models, [form.ai.provider]: e.target.value.trim() })} />}
                  </Field>
                </div>
                {provider.secret && <ApiKey key={provider.secret} secret={provider.secret} status={secrets[provider.secret]} onSaved={setSecrets} />}
                <div className="flex flex-wrap items-center gap-3">
                  <Button loading={testing} disabled={dirty} onClick={testAi}>Test connection</Button>
                  {dirty && <span className="text-xs text-ink-3">Save changes before testing.</span>}
                  {testResult && <span role="status" className={`text-sm ${testResult.ok ? "text-ok-ink" : "text-bad-ink"}`}>{testResult.text}</span>}
                </div>
                <Row>
                  <div className="grid gap-3 sm:grid-cols-2">
                    <Field label="Audience panel size" hint="Simulated viewers per clip, up to 100. 0 turns the panel off.">
                      {(p) => <input {...p} type="number" min={0} max={100} step={5} className="field num" value={form.ai.panel_size} onChange={(e) => set("ai", "panel_size", Math.min(num(e.target.value, 100), 100))} />}
                    </Field>
                    <Field label="Speech model" hint="Runs on this PC's graphics card.">
                      {(p) => (
                        <select {...p} className="field" value={form.ai.whisper_model} onChange={(e) => set("ai", "whisper_model", e.target.value)}>
                          <option value="large-v3-turbo">Accurate (large-v3-turbo)</option>
                          <option value="small">Faster (small)</option>
                        </select>
                      )}
                    </Field>
                  </div>
                </Row>
              </>
            )}

            {tab === "live" && (
              <div className={GRID}>
                <Field label="Clip threshold" hint="Lower makes more clips.">{(p) => <input {...p} type="number" min={0.5} max={6} step={0.1} className="field num" value={form.live.sensitivity} onChange={(e) => set("live", "sensitivity", num(e.target.value, 2.2))} />}</Field>
                <Field label="Look back (seconds)" hint="How far before the moment a clip may start.">{(p) => <input {...p} type="number" min={20} max={180} step={10} className="field num" value={form.live.lookback_sec} onChange={(e) => set("live", "lookback_sec", num(e.target.value, 60))} />}</Field>
                <Field label="Gap between clips (s)">{(p) => <input {...p} type="number" min={0} max={900} step={10} className="field num" value={form.live.cooldown_sec} onChange={(e) => set("live", "cooldown_sec", num(e.target.value, 90))} />}</Field>
                <Field label="Minimum AI score">{(p) => <input {...p} type="number" min={0} max={100} className="field num" value={form.live.min_score} onChange={(e) => set("live", "min_score", num(e.target.value, 55))} />}</Field>
                <Field label="Recording quality" className="col-span-2">
                  {(p) => (
                    <select {...p} className="field" value={form.live.quality} onChange={(e) => set("live", "quality", Number(e.target.value))}>
                      <option value={480}>480p</option>
                      <option value={720}>720p</option>
                      <option value={1080}>1080p</option>
                    </select>
                  )}
                </Field>
              </div>
            )}

            {tab === "posting" && (
              <>
                <div id="youtube">
                  <h3 className="mb-2 text-xs font-medium text-ink-2">YouTube channel</h3>
                  <YouTubeConnect />
                </div>
                <Row>
                  <div className="grid gap-3 sm:grid-cols-2">
                    <Field label="Visibility for new posts">
                      {(p) => (
                        <select {...p} className="field" value={form.posting.privacy} onChange={(e) => set("posting", "privacy", e.target.value as Settings["posting"]["privacy"])}>
                          <option value="private">Private</option>
                          <option value="unlisted">Unlisted</option>
                          <option value="public">Public</option>
                        </select>
                      )}
                    </Field>
                    <Field label="Extra hashtags" hint="Optional. Each clip already gets its own hashtags written from what happens in it.">
                      {(p) => <input {...p} className="field" placeholder="#yourchannel" value={form.posting.hashtags} onChange={(e) => set("posting", "hashtags", e.target.value)} />}
                    </Field>
                  </div>
                </Row>
                <Row><Toggle label="Credit the source in the description" hint="Adds the original title, channel and link." checked={form.posting.credit} onChange={(v) => set("posting", "credit", v)} /></Row>
              </>
            )}

            {tab === "trends" && (
              <>
                <Toggle label="Match each video's topic" hint="Also searches the category and tags of the video being clipped, so advice fits its subject." checked={form.trends.match_topic} onChange={(v) => set("trends", "match_topic", v)} />
                <Row>
                  <Field label="General searches, one per line" hint="Read once a day. These feed the Trends page.">
                    {(p) => <textarea {...p} rows={4} className="field" value={form.trends.queries.join("\n")} onChange={(e) => set("trends", "queries", e.target.value.split("\n").map((q) => q.trimStart()).filter((q, i, all) => q || i === all.length - 1))} />}
                  </Field>
                </Row>
              </>
            )}
          </div>
        </section>
      </div>

      {(dirty || error) && (
        <div className="animate-enter fixed inset-x-0 bottom-14 z-30 border-t border-line bg-raised px-4 py-3 md:bottom-0 md:left-52">
          <div className="mx-auto flex max-w-[1200px] flex-wrap items-center justify-between gap-3 sm:px-2 lg:px-4">
            <p role={error ? "alert" : undefined} className={`text-sm ${error ? "text-bad-ink" : "text-ink-2"}`}>{error || "You have unsaved changes."}</p>
            <div className="flex gap-2">
              <Button variant="ghost" onClick={() => { setForm(remote.data!.settings); setDirty(false); setError(null); }}>Discard</Button>
              <Button variant="primary" loading={saving} onClick={save}>Save changes</Button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}
