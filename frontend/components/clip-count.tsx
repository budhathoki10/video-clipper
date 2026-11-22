"use client";

/** How many clips to make from a video or stream. 0 asks for as many as are found. */
export const MAX_CLIPS = 20;
const COUNTS = Array.from({ length: MAX_CLIPS }, (_, i) => i + 1);

export function clipCountLabel(count: number) {
  return count === 0 ? "as many clips as found" : `up to ${count} ${count === 1 ? "clip" : "clips"}`;
}

export function ClipCountSelect({ value, onChange, ...field }: { value: number; onChange: (count: number) => void; id: string; "aria-describedby"?: string; "aria-invalid"?: boolean }) {
  return (
    <select {...field} className="field num" value={value} onChange={(e) => onChange(Number(e.target.value))}>
      <option value={0}>As many as found</option>
      {COUNTS.map((count) => (
        <option key={count} value={count}>
          {count}
        </option>
      ))}
    </select>
  );
}
