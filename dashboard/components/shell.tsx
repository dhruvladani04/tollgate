"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import type { ReactNode } from "react";

import { type HeldCall, type Session, usePoll } from "@/lib/api";

const PAGES = [
  { href: "/", label: "Decisions" },
  { href: "/approvals/", label: "Waiting for you" },
  { href: "/thresholds/", label: "Thresholds" },
];

function Logo() {
  return (
    <svg viewBox="0 0 28 28" className="h-7 w-7 flex-none" aria-hidden="true">
      <rect width="28" height="28" rx="7" className="fill-ink" />
      <path
        d="M8 20V9.5M8 12.5h12M11.5 12.5l2-3M15.5 12.5l2-3"
        className="stroke-panel"
        strokeWidth="2"
        strokeLinecap="round"
        fill="none"
      />
    </svg>
  );
}

export function Shell({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const { data: queue } = usePoll<{ held: HeldCall[] }>("/api/approvals", 3000);
  const { data: session } = usePoll<Session>("/api/session", 60000);
  const waiting = queue?.held.length ?? 0;

  return (
    <div className="flex min-h-full flex-col">
      <header className="sticky top-0 z-10 border-b border-line bg-panel/90 backdrop-blur">
        <div className="mx-auto flex max-w-[1240px] flex-wrap items-center gap-x-8 gap-y-2 px-4 py-3 sm:px-6">
          <Link href="/" className="flex items-center gap-2.5">
            <Logo />
            <span className="text-[17px] font-semibold tracking-tight">Tollgate</span>
          </Link>
          <nav aria-label="Pages" className="flex flex-wrap items-center gap-1">
            {PAGES.map((page) => {
              const current =
                page.href === "/" ? pathname === "/" : pathname.startsWith(page.href.slice(0, -1));
              return (
                <Link
                  key={page.href}
                  href={page.href}
                  aria-current={current ? "page" : undefined}
                  className={`flex items-center gap-2 rounded-lg px-3 py-1.5 text-[14px] font-medium transition-colors ${
                    current
                      ? "bg-accent-soft text-accent"
                      : "text-ink-2 hover:bg-sunken hover:text-ink"
                  }`}
                >
                  {page.label}
                  {page.href === "/approvals/" && waiting > 0 ? (
                    <span className="rounded-full bg-held-soft px-1.5 text-[12px] font-semibold tabular-nums text-held">
                      {waiting}
                      <span className="sr-only"> waiting</span>
                    </span>
                  ) : null}
                </Link>
              );
            })}
          </nav>
          {session ? (
            <span
              className="ml-auto hidden max-w-[320px] truncate text-[13px] text-muted lg:block"
              title={session.state_dir}
              dir="rtl"
            >
              <bdi>
                Reading <span className="font-mono">{session.state_dir}</span>
              </bdi>
            </span>
          ) : null}
        </div>
      </header>
      <main className="mx-auto w-full max-w-[1240px] flex-1 px-4 py-8 sm:px-6">{children}</main>
      <footer className="border-t border-line py-4 text-center text-[13px] text-muted">
        Tollgate reads its own audit log and approval queue. Nothing leaves this machine.
      </footer>
    </div>
  );
}
