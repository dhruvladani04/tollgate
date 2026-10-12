import type { ReactNode } from "react";

import type { Kind } from "@/lib/api";

const BADGE: Record<Kind, string> = {
  ok: "bg-ok-soft text-ok",
  held: "bg-held-soft text-held",
  stopped: "bg-stopped-soft text-stopped",
  warned: "bg-warned-soft text-warned",
};

// A shape beside the words, so the four groups differ by more than colour.
const MARK: Record<Kind, ReactNode> = {
  ok: <path d="M3.5 8.5l3 3 6-7" />,
  held: (
    <>
      <path d="M8 4.5v4l2.5 1.5" />
      <circle cx="8" cy="8" r="5.5" />
    </>
  ),
  stopped: <path d="M4.5 4.5l7 7M11.5 4.5l-7 7" />,
  warned: (
    <>
      <path d="M8 4.5v4.5" />
      <path d="M8 11.5v.2" />
    </>
  ),
};

export function Badge({ kind, children }: { kind: Kind; children: ReactNode }) {
  return (
    <span
      className={`inline-flex items-center gap-1.5 whitespace-nowrap rounded-full px-2.5 py-0.5 text-[13px] font-medium ${BADGE[kind]}`}
    >
      <svg
        aria-hidden="true"
        viewBox="0 0 16 16"
        className="h-3.5 w-3.5 flex-none"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.8"
        strokeLinecap="round"
        strokeLinejoin="round"
      >
        {MARK[kind]}
      </svg>
      {children}
    </span>
  );
}

export function PageHeader({
  title,
  lead,
  aside,
}: {
  title: string;
  lead: ReactNode;
  aside?: ReactNode;
}) {
  return (
    <div className="mb-6 flex flex-wrap items-end justify-between gap-x-6 gap-y-3">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">{title}</h1>
        <p className="mt-1 max-w-[68ch] text-[15px] text-ink-2">{lead}</p>
      </div>
      {aside}
    </div>
  );
}

export function Live({ updated, error }: { updated: Date | null; error: string | null }) {
  if (error) {
    return (
      <span className="inline-flex items-center gap-2 text-[13px] text-stopped" role="status">
        <span className="h-2 w-2 rounded-full bg-stopped" />
        Not updating
      </span>
    );
  }
  return (
    <span className="inline-flex items-center gap-2 text-[13px] text-muted" role="status">
      <span className="relative flex h-2 w-2">
        <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-ok opacity-60 motion-reduce:hidden" />
        <span className="relative inline-flex h-2 w-2 rounded-full bg-ok" />
      </span>
      {updated ? `Live · updated ${updated.toLocaleTimeString()}` : "Loading"}
    </span>
  );
}

export function Panel({
  children,
  className = "",
}: {
  children: ReactNode;
  className?: string;
}) {
  return (
    <div className={`rounded-xl border border-line bg-panel shadow-[0_1px_2px_rgb(0_0_0/0.04)] ${className}`}>
      {children}
    </div>
  );
}

export function Stat({
  label,
  value,
  hint,
}: {
  label: string;
  value: ReactNode;
  hint?: ReactNode;
}) {
  return (
    <Panel className="px-4 py-3.5">
      <div className="text-[13px] text-ink-2">{label}</div>
      <div className="mt-1 text-[26px] font-semibold leading-none tracking-tight tabular-nums">
        {value}
      </div>
      {hint ? <div className="mt-1.5 text-[13px] text-muted">{hint}</div> : null}
    </Panel>
  );
}

export function Empty({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <Panel className="px-6 py-14 text-center">
      <div className="text-[15px] font-medium">{title}</div>
      {children ? (
        <div className="mx-auto mt-1.5 max-w-[52ch] text-[14px] text-ink-2">{children}</div>
      ) : null}
    </Panel>
  );
}

export function Problem({ children }: { children: ReactNode }) {
  return (
    <div
      role="alert"
      className="mb-5 rounded-xl border border-stopped/30 bg-stopped-soft px-4 py-3 text-[14px] text-stopped"
    >
      {children}
    </div>
  );
}

export function Code({ children }: { children: ReactNode }) {
  return (
    <code className="rounded-md bg-sunken px-1.5 py-0.5 font-mono text-[13px]">{children}</code>
  );
}

/** A small bar showing one score on its scale. */
export function Meter({ label, value, max }: { label: string; value: number; max: number }) {
  const share = Math.max(0, Math.min(1, value / max));
  return (
    <div className="flex items-center gap-2 text-[12px]">
      <span className="w-[76px] flex-none text-muted">{label}</span>
      <span className="h-1.5 w-16 flex-none overflow-hidden rounded-full bg-sunken" aria-hidden="true">
        <span className="block h-full rounded-full bg-accent" style={{ width: `${share * 100}%` }} />
      </span>
      <span className="w-8 flex-none text-right font-mono tabular-nums text-ink-2">
        {value.toFixed(2)}
      </span>
    </div>
  );
}
