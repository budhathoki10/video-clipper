import { Clapperboard } from "lucide-react";
import Link from "next/link";

export function PublicHeader() {
  return (
    <header className="border-b border-line bg-surface">
      <div className="mx-auto flex h-14 max-w-[1120px] items-center justify-between px-4 sm:px-6">
        <Link href="/" className="flex items-center gap-2.5 rounded-sm font-semibold tracking-tight">
          <span aria-hidden className="grid size-7 place-items-center rounded-md bg-accent text-accent-ink">
            <Clapperboard className="size-4" />
          </span>
          Clipper
        </Link>
        <nav aria-label="Account" className="flex items-center gap-2">
          <Link href="/login/" className="btn btn-ghost">Sign in</Link>
        </nav>
      </div>
    </header>
  );
}
