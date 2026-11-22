import type { CSSProperties, ReactNode } from "react";

import type { Settings } from "@/lib/api";
import { cx } from "./ui";

export type Layout = Settings["format"]["layout"];

/** The ways a wide video can be fitted into a vertical clip. Styles that need a webcam fall back to
 *  "blur" for a video without one. */
export const LAYOUTS: { id: Layout; label: string; short: string; needsWebcam?: boolean }[] = [
  { id: "auto", label: "Automatic: webcam in a small corner window when there is one", short: "Automatic" },
  { id: "blur", label: "Whole frame on a blurred background", short: "Blurred frame" },
  { id: "card", label: "Whole frame as a rounded card on a blurred background", short: "Rounded card" },
  { id: "cards", label: "Two cards: webcam on top, action below, on a blurred background", short: "Two cards", needsWebcam: true },
  { id: "streamer", label: "Streamer in view: the video cropped to keep the streamer in it, no window", short: "Streamer in view", needsWebcam: true },
  { id: "corner", label: "Webcam in a small corner window (left), action fills the screen", short: "Corner left", needsWebcam: true },
  { id: "corner_right", label: "Webcam in a small corner window (right), action fills the screen", short: "Corner right", needsWebcam: true },
  { id: "bubble", label: "Webcam in a round bubble, action fills the screen", short: "Round bubble", needsWebcam: true },
  { id: "stack", label: "Stacked: webcam band on top, action below", short: "Stacked", needsWebcam: true },
  { id: "split", label: "Split: webcam on the top half, action on the bottom half", short: "Half and half", needsWebcam: true },
  { id: "speaker", label: "Follow the speaker's face", short: "Follow speaker" },
  { id: "crop", label: "Crop to fill the screen", short: "Fill screen" },
];

export function LayoutOptions() {
  return (
    <>
      {LAYOUTS.map((layout) => (
        <option key={layout.id} value={layout.id}>
          {layout.label}
        </option>
      ))}
    </>
  );
}

/** The picture of a YouTube link, so the preview shows the person's own video. */
export function youtubeThumbnail(link: string): string | null {
  const match = link.match(/(?:youtu\.be\/|[?&]v=|\/shorts\/|\/live\/|\/embed\/)([\w-]{11})/);
  return match ? `https://i.ytimg.com/vi/${match[1]}/mqdefault.jpg` : null;
}

const SAMPLE = "/sample-frame.svg";

/** One phone-shaped preview of the selected style. The video is `image`; the webcam is drawn as a
 *  person tile, since the real webcam is only found when the clip is made. */
function PhonePreview({ id, image }: { id: Layout; image: string | null }) {
  const src = image ?? SAMPLE;
  const picture = (extra: string, style?: CSSProperties) => (
    // eslint-disable-next-line @next/next/no-img-element
    <img src={src} alt="" referrerPolicy="no-referrer" className={cx("absolute object-cover", extra)} style={style} />
  );
  const backdrop = picture("inset-0 size-full scale-125 blur-md brightness-90");
  const webcam = (extra: string) => (
    <div className={cx("absolute overflow-hidden bg-[#cfe0ff]", extra)}>
      <div className="absolute top-[24%] left-1/2 aspect-square w-[34%] -translate-x-1/2 rounded-full bg-accent" />
      <div className="absolute bottom-0 left-1/2 h-[36%] w-[66%] -translate-x-1/2 rounded-t-full bg-accent" />
    </div>
  );
  const ring = "";
  let body: ReactNode;
  let titleTop = "40%";
  switch (id) {
    case "blur":
      body = (<>{backdrop}{picture("inset-x-0 top-[35%] h-[30%] w-full")}</>);
      break;
    case "card":
      body = (<>{backdrop}{picture(cx("inset-x-[5%] top-[35%] h-[30%] w-[90%] rounded-lg", ring))}</>);
      break;
    case "cards":
      body = (<>{backdrop}{webcam(cx("inset-x-[6%] top-[5%] h-[27%] rounded-lg", ring))}{picture(cx("inset-x-[6%] top-[35%] h-[60%] w-[88%] rounded-lg", ring))}</>);
      titleTop = "34%";
      break;
    case "streamer":
      body = (<>{backdrop}{picture("inset-x-0 top-[14%] h-[43%] w-full", { objectPosition: "right center" })}
        <div className="absolute top-[40%] right-[3%] z-[1] h-[17%] w-[30%] overflow-hidden">
          <div className="absolute top-[8%] left-1/2 aspect-square w-[46%] -translate-x-1/2 rounded-full bg-accent" />
          <div className="absolute bottom-0 left-1/2 h-[48%] w-[90%] -translate-x-1/2 rounded-t-full bg-accent" />
        </div></>);
      titleTop = "5%";
      break;
    case "corner":
    case "corner_right":
    case "auto":
      body = (<>{picture("inset-0 size-full")}{webcam(cx("top-[4%] h-[22%] w-[42%] rounded-lg", id === "corner_right" ? "right-[5%]" : "left-[5%]", ring))}</>);
      titleTop = "29%";
      break;
    case "bubble":
      body = (<>{picture("inset-0 size-full")}{webcam(cx("top-[4%] left-[5%] aspect-square w-[32%] rounded-full", ring))}</>);
      titleTop = "29%";
      break;
    case "stack":
      body = (<>{webcam("inset-x-0 top-0 h-[36%]")}{picture("inset-x-0 top-[36%] h-[64%] w-full")}</>);
      titleTop = "38%";
      break;
    case "split":
      body = (<>{webcam("inset-x-0 top-0 h-1/2")}{picture("inset-x-0 top-1/2 h-1/2 w-full")}</>);
      titleTop = "52%";
      break;
    case "speaker":
      body = webcam("inset-0");
      break;
    default:
      body = picture("inset-0 size-full");
  }
  return (
    <div aria-hidden className="relative aspect-[9/16] w-[200px] flex-none overflow-hidden rounded-xl bg-raised shadow-md ring-1 ring-line">
      {body}
      <div className="absolute inset-x-[10%] z-10 rounded-[3px] bg-white px-2 py-1 text-center text-[10px] leading-tight font-bold text-ink shadow-sm" style={{ top: titleTop }}>
        Your hook title
      </div>
      <div className="absolute inset-x-0 bottom-[12%] z-10 text-center text-[13px] font-extrabold tracking-wide text-white [text-shadow:0_1px_3px_rgb(0_0_0/0.8)]">
        THIS IS <span className="text-[#ffd84a]">WILD</span>
      </div>
    </div>
  );
}

const DESCRIPTION: Record<Layout, string> = {
  auto: "Puts the webcam in a small corner window when the video has one. Otherwise shows the whole frame on a blurred background.",
  blur: "The whole video in the middle, with a blurred copy filling the rest of the screen. Nothing is cut off.",
  card: "The whole video as a rounded card in the middle of a blurred background.",
  cards: "Two rounded cards on a blurred background: the webcam on top, the action below.",
  streamer: "The video cropped so the streamer stays in the shot just as they appear, with no window or border around them. It sits on a blurred background with room for captions below.",
  corner: "The action fills the screen. The webcam sits in a small window in the top-left corner.",
  corner_right: "The action fills the screen. The webcam sits in a small window in the top-right corner.",
  bubble: "The action fills the screen. The webcam is a small round bubble in the top-left corner.",
  stack: "A webcam band across the top, the action below it.",
  split: "The screen is cut in half by a thin white line: a close-up of the creator on top, the action underneath, and the captions sitting on the line.",
  speaker: "Fills the screen with the speaker. The crop follows their face. Best for podcasts and interviews.",
  crop: "Crops the middle of the video to fill the screen. Sides are cut off.",
};

/** Pick how the video looks. A large preview shows the selected style on the video's own picture. */
export function LayoutPicker({ value, onChange, link = "" }: { value: Layout; onChange: (layout: Layout) => void; link?: string }) {
  const current = LAYOUTS.find((layout) => layout.id === value);
  const image = youtubeThumbnail(link);
  return (
    <div className="flex flex-col gap-4 sm:flex-row">
      <div className="flex flex-col items-center gap-2">
        <PhonePreview id={value} image={image} />
        <p className="max-w-[200px] text-center text-xs text-ink-3">
          {image ? "Preview with your video's picture. The webcam is found when the clip is made." : "Preview. Paste a link to see it with your video."}
        </p>
      </div>
      <div className="flex min-w-0 flex-1 flex-col gap-3">
        <div role="radiogroup" aria-label="Video style" className="grid grid-cols-2 gap-2 lg:grid-cols-3">
          {LAYOUTS.map((layout) => {
            const selected = layout.id === value;
            return (
              <button
                key={layout.id}
                type="button"
                role="radio"
                aria-checked={selected}
                onClick={() => onChange(layout.id)}
                className={cx(
                  "rounded-md border bg-surface px-3 py-2 text-left text-sm font-medium transition-colors hover:bg-raised focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent",
                  selected ? "border-accent text-ink ring-1 ring-accent" : "border-line text-ink-2",
                )}
              >
                {layout.short}
              </button>
            );
          })}
        </div>
        {current && (
          <p className="text-sm text-ink-2">
            {DESCRIPTION[current.id]}
            {current.needsWebcam ? " Needs a webcam in the video; without one it uses the blurred frame." : ""}
          </p>
        )}
      </div>
    </div>
  );
}
