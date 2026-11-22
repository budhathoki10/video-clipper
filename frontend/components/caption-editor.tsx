"use client";

import { RotateCcw } from "lucide-react";
import { useEffect, useState } from "react";

import { Button, Notice, Section, SkeletonRows, useToast } from "@/components/ui";
import { api, useApi, type CaptionLines, type Clip } from "@/lib/api";

const MAX_LINE = 200;

/** 0:07.4 style time, to a tenth of a second. */
function at(seconds: number) {
  const tenths = Math.round(seconds * 10);
  return `${Math.floor(tenths / 600)}:${String(Math.floor((tenths % 600) / 10)).padStart(2, "0")}.${tenths % 10}`;
}

/**
 * Fix the words of a clip's captions. Each line keeps its time; only the text changes. Saving renders the
 * clip again with the corrected words.
 */
export function CaptionEditor({ clip, onClipChange, onSeek }: { clip: Clip; onClipChange: (clip: Clip) => void; onSeek: (seconds: number) => void }) {
  const toast = useToast();
  const ready = clip.status === "ready";
  // `created_at` changes whenever the clip is rendered again, so the lines are fetched again then.
  const data = useApi<CaptionLines>(ready ? `/api/clips/${encodeURIComponent(clip.id)}/captions?v=${Math.round(clip.created_at)}` : null);
  const [texts, setTexts] = useState<string[]>([]);
  const [busy, setBusy] = useState<"save" | "reset" | null>(null);
  const lines = data.data?.lines ?? [];

  useEffect(() => {
    setTexts((data.data?.lines ?? []).map((line) => line.text));
  }, [data.data]);

  const dirty = texts.length === lines.length && texts.some((text, i) => text.trim() !== lines[i].text);

  async function save() {
    setBusy("save");
    try {
      const updated = await api<Clip>(`/api/clips/${clip.id}/captions`, {
        method: "PUT",
        json: { lines: lines.map((line, i) => ({ s: line.s, e: line.e, text: texts[i].trim() })) },
      });
      onClipChange(updated);
      toast("Saved. Rendering the clip with your captions.");
    } catch (err) {
      toast(err instanceof Error ? err.message : "Could not save the captions.", "bad");
    } finally {
      setBusy(null);
    }
  }

  async function reset() {
    setBusy("reset");
    try {
      onClipChange(await api<Clip>(`/api/clips/${clip.id}/captions`, { method: "DELETE" }));
      toast("Back to the recognised words. Rendering the clip again.");
    } catch (err) {
      toast(err instanceof Error ? err.message : "Could not reset the captions.", "bad");
    } finally {
      setBusy(null);
    }
  }

  return (
    <Section
      title="Captions"
      description="Fix a name or a word the speech recognition got wrong. Each line keeps its time. Saving renders the clip again."
      aside={
        data.data?.edited && ready ? (
          <Button variant="ghost" loading={busy === "reset"} disabled={busy === "save"} onClick={reset}>
            <RotateCcw /> Use recognised words
          </Button>
        ) : undefined
      }
    >
      {!ready ? (
        <p className="text-sm text-ink-2">{clip.status === "failed" ? "This clip did not render. Fix it under Details, then edit its captions." : "The captions can be edited once the clip has rendered."}</p>
      ) : data.error && !data.data ? (
        <Notice tone="accent">{data.error}</Notice>
      ) : !data.data ? (
        <SkeletonRows rows={3} />
      ) : lines.length === 0 ? (
        <p className="text-sm text-ink-2">No speech was recognised in this clip, so there are no captions to edit.</p>
      ) : (
        <div className="flex flex-col gap-3">
          {clip.captions.enabled === false && (
            <Notice tone="accent">Captions are turned off for this clip. Turn them on under Details to show these words.</Notice>
          )}
          <ul className="-mr-1 flex max-h-96 flex-col gap-1.5 overflow-y-auto pr-1" aria-label="Caption lines">
            {lines.map((line, i) => (
              <li key={`${line.s}-${i}`} className="grid grid-cols-[3.25rem_minmax(0,1fr)] items-center gap-2">
                <button
                  type="button"
                  onClick={() => onSeek(line.s)}
                  aria-label={`Play from ${at(line.s)}`}
                  className="num rounded-sm px-1 py-1 text-left text-xs text-ink-3 transition-colors duration-100 hover:text-accent"
                >
                  {at(line.s)}
                </button>
                <input
                  className="field"
                  aria-label={`Caption at ${at(line.s)}`}
                  maxLength={MAX_LINE}
                  value={texts[i] ?? ""}
                  disabled={busy !== null}
                  onChange={(e) => setTexts((all) => all.map((text, j) => (j === i ? e.target.value : text)))}
                />
              </li>
            ))}
          </ul>
          <p className="text-xs text-ink-3">If you change how many words a line has, its words share the line&apos;s time evenly. Empty a line to show nothing there.</p>
          <div className="flex justify-end gap-2 border-t border-line-subtle pt-3">
            {dirty && (
              <Button variant="ghost" disabled={busy !== null} onClick={() => setTexts(lines.map((line) => line.text))}>
                Discard
              </Button>
            )}
            <Button variant="primary" disabled={!dirty} loading={busy === "save"} onClick={save}>
              Save and render
            </Button>
          </div>
        </div>
      )}
    </Section>
  );
}
