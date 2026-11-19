import Link from "next/link";

import { Logo } from "./logo";

export function PublicHeader() {
  return (
    <header className="border-b border-line bg-surface">
      <div className="mx-auto flex h-14 max-w-[1120px] items-center justify-between px-4 sm:px-6">
        <Link href="/" className="flex items-center gap-2.5 rounded-sm font-semibold tracking-tight">
          <Logo />
          Clipper
        </Link>
        <nav aria-label="Account" className="flex items-center gap-2">
          <Link href="/login/" className="btn btn-ghost">Sign in</Link>
        </nav>
      </div>
    </header>
  );
}
