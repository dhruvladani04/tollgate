"use client";

import { Fragment, useMemo, useState } from "react";

import { ago, type Call, dayAndTime, type Decisions, type Kind, usePoll } from "@/lib/api";

import { Badge, Code, Empty, Live, Meter, PageHeader, Panel, Problem, Stat } from "./ui";

const FILTERS: { key: "all" | Kind; label: string }[] = [
  { key: "all", label: "All" },
  { key: "ok", label: "Allowed" },
  { key: "held", label: "Approved by a person" },
  { key: "stopped", label: "Stopped" },
  { key: "warned", label: "Flagged" },
];

const SCALE: Record<string, number> = { risk: 3 };

function median(values: number[]): number | null {
  if (!values.length) return null;
  const sorted = [...values].sort((a, b) => a - b);
  const middle = Math.floor(sorted.length / 2);
  return sorted.length % 2 ? sorted[middle] : (sorted[middle - 1] + sorted[middle]) / 2;
}

function Detail({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="grid grid-cols-[130px_1fr] gap-x-4 gap-y-1 py-1.5 text-[14px]">
      <dt className="text-muted">{label}</dt>
      <dd className="min-w-0 break-words">{children}</dd>
    </div>
  );
}

function Details({ call }: { call: Call }) {
  return (
    <dl className="divide-y divide-line">
      <Detail label="When">{dayAndTime(call.time)}</Detail>
      {call.user_request ? <Detail label="The user asked">{call.user_request}</Detail> : null}
      <Detail label="Arguments">
        <span className="font-mono text-[13px]">{call.arguments}</span>
      </Detail>
      {call.reasons.length ? (
        <Detail label="Why">
          <ul className="list-disc space-y-0.5 pl-4">
            {call.reasons.map((reason) => (
              <li key={reason}>{reason}</li>
            ))}
          </ul>
        </Detail>
      ) : null}
      {call.error ? <Detail label="No decision">{call.error}</Detail> : null}
      <Detail label="Judged by">
        {call.model ? (
          <>
            {call.model}
            {call.cached ? ", answered from the cache" : ""}
            {call.band ? `, band: ${call.band}` : ""}
          </>
        ) : (
          "An exact check; no model was asked"
        )}
      </Detail>
      {call.second_model ? (
        <Detail label="Second model">
          {call.second_model}
          {call.second_model_error ? ` gave no answer (${call.second_model_error})` : ""}
        </Detail>
      ) : null}
      {call.result_action ? (
        <Detail label="Result scan">
          {call.result_action === "unscanned"
            ? "The result could not be scanned, and was passed on with a warning."
            : `${(call.injected ?? 0).toFixed(2)} probability of instructions aimed at the agent; the result was ${call.result_action}.`}
        </Detail>
      ) : null}
      {call.withheld_result ? (
        <Detail label="Withheld text">
          <span className="font-mono text-[13px]">{call.withheld_result}</span>
        </Detail>
      ) : null}
    </dl>
  );
}

export function DecisionsView() {
  const { data, error, updated } = usePoll<Decisions>("/api/decisions", 5000);
  const [filter, setFilter] = useState<"all" | Kind>("all");
  const [search, setSearch] = useState("");
  const [open, setOpen] = useState<string | null>(null);

  const calls = useMemo(() => data?.calls ?? [], [data]);
  const shown = useMemo(() => {
    const wanted = search.trim().toLowerCase();
    return calls.filter(
      (call) =>
        (filter === "all" || call.kind === filter) &&
        (!wanted ||
          call.tool.toLowerCase().includes(wanted) ||
          call.upstream.toLowerCase().includes(wanted)),
    );
  }, [calls, filter, search]);

  const count = (kind: Kind) => calls.filter((call) => call.kind === kind).length;
  const asked = calls.filter((call) => call.approval_id !== null).length;
  const typical = median(
    calls.filter((call) => call.latency_ms !== null && !call.cached).map((call) => call.latency_ms!),
  );
  const now = updated ?? new Date();

  return (
    <>
      <PageHeader
        title="Decisions"
        lead="Every tool call Tollgate judged, newest first. Select a row to see why it was decided that way."
        aside={<Live updated={updated} error={error} />}
      />
      {error ? <Problem>{error}</Problem> : null}

      <div className="mb-6 grid grid-cols-2 gap-3 lg:grid-cols-5">
        <Stat label="Calls shown" value={calls.length} hint="the latest judged" />
        <Stat label="Allowed" value={count("ok")} hint="ran without a question" />
        <Stat label="Put to a person" value={asked} hint="held for an answer" />
        <Stat label="Stopped" value={count("stopped")} hint="blocked, denied or withheld" />
        <Stat
          label="Typical decision"
          value={typical === null ? "–" : `${Math.round(typical)} ms`}
          hint="median, not from cache"
        />
      </div>

      <div className="mb-3 flex flex-wrap items-center gap-2">
        <div className="flex flex-wrap gap-1" role="group" aria-label="Show">
          {FILTERS.map((item) => {
            const total = item.key === "all" ? calls.length : count(item.key);
            return (
              <button
                key={item.key}
                type="button"
                aria-pressed={filter === item.key}
                onClick={() => setFilter(item.key)}
                className={`rounded-lg border px-3 py-1.5 text-[13px] font-medium transition-colors ${
                  filter === item.key
                    ? "border-accent bg-accent-soft text-accent"
                    : "border-line bg-panel text-ink-2 hover:text-ink"
                }`}
              >
                {item.label} <span className="tabular-nums text-muted">{total}</span>
              </button>
            );
          })}
        </div>
        <label className="ml-auto flex items-center gap-2 text-[13px] text-ink-2">
          Tool
          <input
            type="search"
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            placeholder="write_file"
            className="w-44 rounded-lg border border-line bg-panel px-3 py-1.5 text-[14px] text-ink placeholder:text-muted"
          />
        </label>
      </div>

      {!data ? (
        <Empty title={error ? "No data" : "Loading the audit log"} />
      ) : calls.length === 0 ? (
        <Empty title="No calls recorded yet">
          Start Tollgate in enforce or observe mode with this state folder, and each call it
          judges will appear here.
        </Empty>
      ) : shown.length === 0 ? (
        <Empty title="No call matches this filter" />
      ) : (
        <Panel className="overflow-x-auto">
          <table className="w-full min-w-[820px] border-collapse text-[14px]">
            <thead>
              <tr className="border-b border-line text-left text-[12.5px] text-ink-2">
                <th className="px-4 py-2.5 font-medium">When</th>
                <th className="px-4 py-2.5 font-medium">Tool</th>
                <th className="px-4 py-2.5 font-medium">What happened</th>
                <th className="px-4 py-2.5 font-medium">Why</th>
                <th className="px-4 py-2.5 font-medium">Model&rsquo;s answers</th>
                <th className="px-4 py-2.5 text-right font-medium">Time</th>
                <th className="w-8 px-2 py-2.5">
                  <span className="sr-only">Details</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {shown.map((call, index) => {
                const key = `${call.time}-${index}`;
                const expanded = open === key;
                return (
                  <Fragment key={key}>
                    <tr
                      className={`cursor-pointer border-b border-line align-top transition-colors last:border-b-0 hover:bg-sunken ${expanded ? "bg-sunken" : ""}`}
                      onClick={() => setOpen(expanded ? null : key)}
                    >
                      <td className="whitespace-nowrap px-4 py-3 text-ink-2" title={dayAndTime(call.time)}>
                        {ago(call.time, now)}
                      </td>
                      <td className="px-4 py-3">
                        <button
                          type="button"
                          aria-expanded={expanded}
                          className="text-left font-mono text-[13px] font-medium"
                          onClick={(event) => {
                            event.stopPropagation();
                            setOpen(expanded ? null : key);
                          }}
                        >
                          {call.tool}
                        </button>
                        <div className="text-[12.5px] text-muted">{call.upstream}</div>
                      </td>
                      <td className="px-4 py-3">
                        <Badge kind={call.kind}>{call.outcome}</Badge>
                        {call.approval_id !== null ? (
                          <div className="mt-1 text-[12.5px] text-muted">
                            held call #{call.approval_id}
                          </div>
                        ) : null}
                      </td>
                      <td className="max-w-[340px] px-4 py-3 text-ink-2">
                        <div className="line-clamp-2">
                          {call.reasons[0] ?? call.error ?? "No reason recorded"}
                        </div>
                        {call.reasons.length > 1 ? (
                          <div className="text-[12.5px] text-muted">
                            and {call.reasons.length - 1} more
                          </div>
                        ) : null}
                      </td>
                      <td className="px-4 py-3">
                        {Object.keys(call.scores).length ? (
                          <div className="space-y-1">
                            {Object.entries(call.scores).map(([name, value]) => (
                              <Meter key={name} label={name} value={value} max={SCALE[name] ?? 1} />
                            ))}
                          </div>
                        ) : (
                          <span className="text-[13px] text-muted">Not asked</span>
                        )}
                      </td>
                      <td className="whitespace-nowrap px-4 py-3 text-right tabular-nums text-ink-2">
                        {call.latency_ms === null ? "–" : `${Math.round(call.latency_ms)} ms`}
                        {call.cached ? <div className="text-[12.5px] text-muted">cached</div> : null}
                      </td>
                      <td className="px-2 py-3 text-muted">
                        <svg
                          aria-hidden="true"
                          viewBox="0 0 16 16"
                          className={`h-4 w-4 transition-transform ${expanded ? "rotate-90" : ""}`}
                          fill="none"
                          stroke="currentColor"
                          strokeWidth="1.8"
                          strokeLinecap="round"
                          strokeLinejoin="round"
                        >
                          <path d="M6 3.5l4.5 4.5L6 12.5" />
                        </svg>
                      </td>
                    </tr>
                    {expanded ? (
                      <tr className="border-b border-line bg-sunken last:border-b-0">
                        <td colSpan={7} className="px-4 pb-4 pt-1">
                          <Details call={call} />
                        </td>
                      </tr>
                    ) : null}
                  </Fragment>
                );
              })}
            </tbody>
          </table>
        </Panel>
      )}
      {data && shown.length ? (
        <p className="mt-3 text-[13px] text-muted">
          Showing {shown.length} of the latest {calls.length} calls. The full record is in{" "}
          <Code>audit.sqlite</Code> in the state folder.
        </p>
      ) : null}
    </>
  );
}
