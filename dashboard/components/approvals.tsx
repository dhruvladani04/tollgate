"use client";

import { useState } from "react";

import { answerCall, type HeldCall, timeLeft, usePoll } from "@/lib/api";

import { Empty, Live, PageHeader, Panel, Problem } from "./ui";

function Held({
  call,
  busy,
  onAnswer,
}: {
  call: HeldCall;
  busy: boolean;
  onAnswer: (decision: "approve" | "deny") => void;
}) {
  const total = Math.max(
    1,
    (new Date(call.expires).getTime() - new Date(call.created).getTime()) / 1000,
  );
  const left = Math.min(1, call.seconds_left / total);
  return (
    <Panel className="overflow-hidden">
      <div className="h-1 bg-sunken" aria-hidden="true">
        <div className="h-full bg-held" style={{ width: `${left * 100}%` }} />
      </div>
      <div className="p-5">
        <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
          <h2 className="text-[17px] font-semibold">
            <span className="text-muted">#{call.id}</span>{" "}
            <span className="font-mono text-[15px]">{call.tool}</span>
          </h2>
          <span className="text-[13px] text-muted">from {call.upstream}</span>
          <span className="ml-auto text-[13px] tabular-nums text-ink-2">
            {timeLeft(call.seconds_left)} left
          </span>
        </div>

        <div className="mt-4 rounded-lg bg-held-soft px-4 py-3 text-[14px] text-held">
          <div className="font-medium">Why Tollgate is asking</div>
          <ul className="mt-1 list-disc space-y-0.5 pl-4">
            {call.reasons.map((reason) => (
              <li key={reason}>{reason}</li>
            ))}
          </ul>
        </div>

        <dl className="mt-4 space-y-3 text-[14px]">
          {call.user_request ? (
            <div>
              <dt className="text-[13px] text-muted">The user asked</dt>
              <dd className="mt-0.5">{call.user_request}</dd>
            </div>
          ) : (
            <div>
              <dt className="text-[13px] text-muted">The user asked</dt>
              <dd className="mt-0.5 text-ink-2">
                Tollgate was not told. The client sent no request with this call.
              </dd>
            </div>
          )}
          <div>
            <dt className="text-[13px] text-muted">The call&rsquo;s arguments</dt>
            <dd className="mt-1 overflow-x-auto rounded-lg bg-sunken px-3 py-2 font-mono text-[13px] break-words whitespace-pre-wrap">
              {call.arguments}
            </dd>
          </div>
        </dl>

        <div className="mt-5 flex flex-wrap gap-2.5">
          <button
            type="button"
            disabled={busy}
            onClick={() => onAnswer("approve")}
            className="rounded-lg bg-accent px-4 py-2 text-[14px] font-semibold text-on-accent transition-opacity hover:opacity-90 disabled:opacity-50"
          >
            Approve call #{call.id}
          </button>
          <button
            type="button"
            disabled={busy}
            onClick={() => onAnswer("deny")}
            className="rounded-lg border border-line bg-panel px-4 py-2 text-[14px] font-semibold text-stopped transition-colors hover:bg-stopped-soft disabled:opacity-50"
          >
            Deny call #{call.id}
          </button>
        </div>
      </div>
    </Panel>
  );
}

export function ApprovalsView() {
  const { data, error, updated, refresh } = usePoll<{ held: HeldCall[] }>("/api/approvals", 2000);
  const [busy, setBusy] = useState<number | null>(null);
  const [note, setNote] = useState<{ text: string; changed: boolean } | null>(null);

  async function answer(id: number, decision: "approve" | "deny") {
    setBusy(id);
    try {
      const result = await answerCall(id, decision);
      setNote({ text: result.note, changed: result.changed });
    } catch {
      setNote({ text: `Call #${id} was not changed: the dashboard could not be reached.`, changed: false });
    }
    await refresh();
    setBusy(null);
  }

  return (
    <>
      <PageHeader
        title="Waiting for you"
        lead="Calls Tollgate is holding until someone answers. A call nobody answers in time is refused, and the agent is told why."
        aside={<Live updated={updated} error={error} />}
      />
      {error ? <Problem>{error}</Problem> : null}
      {note ? (
        <div
          role="status"
          className={`mb-5 rounded-xl px-4 py-3 text-[14px] ${
            note.changed ? "bg-ok-soft text-ok" : "bg-warned-soft text-warned"
          }`}
        >
          {note.text}
        </div>
      ) : null}
      {!data ? (
        <Empty title={error ? "No data" : "Loading the approval queue"} />
      ) : data.held.length === 0 ? (
        <Empty title="Nothing is waiting for approval">
          When Tollgate holds a call, it appears here within a couple of seconds.
        </Empty>
      ) : (
        <div className="grid gap-4 lg:grid-cols-2">
          {data.held.map((call) => (
            <Held
              key={call.id}
              call={call}
              busy={busy === call.id}
              onAnswer={(decision) => answer(call.id, decision)}
            />
          ))}
        </div>
      )}
    </>
  );
}
