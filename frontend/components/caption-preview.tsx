"use client";

import { useEffect, useState } from "react";

import { API_URL, type Settings } from "@/lib/api";

const SAMPLE = ["No", "way", "he", "actually", "did", "that!"];
const SIZE = { small: 13, medium: 16, large: 20 } as const;
// The fonts bundled with the engine, served by the API so the preview matches the render.
const FONT_FILES: Record<string, string> = {
  "Poppins ExtraBold": "Poppins-ExtraBold.ttf",
  Anton: "Anton-Regular.ttf",
  Bangers: "Bangers-Regular.ttf",
};

/** A small 9:16 mock-up showing the caption style as it will be rendered. */
export function CaptionPreview({ captions, overlay }: { captions: Settings["captions"]; overlay: Settings["overlay"] }) {
  const [step, setStep] = useState(0);
  const perLine = Math.min(Math.max(captions.words_per_line || 3, 1), 6);

  useEffect(() => {
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    const timer = setInterval(() => setStep((s) => (s + 1) % SAMPLE.length), 420);
    return () => clearInterval(timer);
  }, []);

  const groupStart = Math.floor(step / perLine) * perLine;
  const group = SAMPLE.slice(groupStart, groupStart + perLine);
  const file = FONT_FILES[captions.font];

  return (
    <figure className="mx-auto w-[168px]">
      {file && <style>{`@font-face{font-family:"${captions.font}";src:url("${API_URL}/fonts/${file}");font-display:swap}`}</style>}
      <div className="relative aspect-[9/16] overflow-hidden rounded-md border border-line bg-ink shadow-soft" aria-hidden>
        <div className="absolute inset-x-0 top-[29%] h-[42%] bg-[#33435d]" />
        {overlay.credit && <p className="absolute inset-x-0 top-[6%] text-center text-[7px] text-white/70">Credit: @channel</p>}
        {overlay.hook_title && (
          <p className="absolute inset-x-0 top-[10%] text-center">
            <span className="bg-white px-1.5 py-0.5 text-[9px] font-bold text-black">Hook text goes here</span>
          </p>
        )}
        {captions.enabled && (
          <p
            className="absolute inset-x-1 text-center leading-tight"
            style={{
              top: captions.position === "middle" ? "46%" : "73%",
              fontFamily: `"${captions.font}", "Arial Black", sans-serif`,
              fontSize: SIZE[captions.size] ?? 16,
              fontWeight: 800,
              color: captions.color,
              WebkitTextStroke: "3px #000",
              paintOrder: "stroke fill",
              textTransform: captions.uppercase ? "uppercase" : "none",
            }}
          >
            {group.map((word, i) => (
              <span key={`${groupStart}-${i}`} style={{ color: groupStart + i === step ? captions.highlight : undefined }}>
                {word.replace(/[.,]/g, "")}
                {i < group.length - 1 ? " " : ""}
              </span>
            ))}
          </p>
        )}
      </div>
      <figcaption className="mt-2 text-center text-xs text-ink-3">Preview</figcaption>
    </figure>
  );
}
