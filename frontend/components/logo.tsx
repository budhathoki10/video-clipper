/** The Clipper mark: a play button framed by two viewfinder corners. Same drawing as the app icons
 * in public/ (made on the same 100-unit grid), so the header and the home-screen icon match. */
export function Logo({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 100 100" aria-hidden className={`flex-none ${className ?? "size-7"}`}>
      <rect width="100" height="100" rx="22" fill="#3478f6" />
      <path d="M24 40V30a6 6 0 0 1 6-6h10" fill="none" stroke="#ffc93c" strokeWidth="7" strokeLinecap="round" />
      <path d="M76 60v10a6 6 0 0 1-6 6H60" fill="none" stroke="#ff6b6b" strokeWidth="7" strokeLinecap="round" />
      <path d="M42 36v28l24-14z" fill="#fff" stroke="#fff" strokeWidth="6" strokeLinejoin="round" />
    </svg>
  );
}
