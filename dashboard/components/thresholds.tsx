"use client";

import { type Threshold, type Thresholds, usePoll } from "@/lib/api";

import { Code, Empty, Live, PageHeader, Panel, Problem } from "./ui";

function crossed(threshold: Threshold, score: number): boolean {
  if (threshold.direction === "above") return score > threshold.value;
  if (threshold.direction === "below") return score < threshold.value;
  return score >= threshold.value;
}

/** Recent scores as dots along the answer's scale, with the threshold as a line. */
function Strip({ threshold }: { threshold: Threshold }) {
  const at = (value: number) => `${(Math.max(0, Math.min(threshold.scale_max, value)) / threshold.scale_max) * 100}%`;
  const below = threshold.direction === "below";
  const limit = at(threshold.value);
  return (
    <div className="mt-4">
      <div className="relative h-9" aria-hidden="true">
        <div className="absolute inset-x-0 top-1/2 h-px bg-line" />
        <div
          className="absolute top-1/2 h-3 -translate-y-1/2 rounded-sm bg-accent-soft"
          style={below ? { left: 0, width: limit } : { left: limit, right: 0 }}
        />
        {threshold.scores.map((score, index) => (
          <span
            key={index}
            className={`absolute top-1/2 h-2 w-2 -translate-x-1/2 -translate-y-1/2 rounded-full ring-2 ring-panel ${
              crossed(threshold, score) ? "bg-accent" : "bg-muted/50"
            }`}
            style={{ left: at(score) }}
          />
        ))}
        <div className="absolute top-0 h-full w-0.5 -translate-x-1/2 bg-ink" style={{ left: limit }} />
      </div>
      <div className="flex justify-between text-[12px] tabular-nums text-muted">
        <span>0</span>
        <span>{threshold.scale_max}</span>
      </div>
    </div>
  );
}

function ThresholdCard({ threshold }: { threshold: Threshold }) {
  return (
    <Panel className="p-5">
      <div className="flex items-baseline justify-between gap-3">
        <h2 className="font-mono text-[14px] font-medium">{threshold.name}</h2>
        <span className="text-[26px] font-semibold leading-none tracking-tight tabular-nums">
          {threshold.value.toFixed(2)}
        </span>
      </div>
      <p className="mt-2 text-[14px] text-ink-2">
        When <Code>{threshold.answer}</Code> is {threshold.direction} this: {threshold.effect}
      </p>
      <Strip threshold={threshold} />
      <dl className="mt-3 grid grid-cols-2 gap-3 text-[13px]">
        <div>
          <dt className="text-muted">Recent calls that crossed it</dt>
          <dd className="mt-0.5 text-[15px] font-medium tabular-nums">
            {threshold.crossed} of {threshold.judged}
          </dd>
        </div>
        <div>
          <dt className="text-muted">Nearest call that did not</dt>
          <dd className="mt-0.5 text-[15px] font-medium tabular-nums">
            {threshold.nearest === null ? "None" : threshold.nearest.toFixed(2)}
          </dd>
        </div>
      </dl>
    </Panel>
  );
}

function Setting({ name, value, children }: { name: string; value: string; children: React.ReactNode }) {
  return (
    <Panel className="p-5">
      <div className="flex items-baseline justify-between gap-3">
        <h2 className="font-mono text-[14px] font-medium">{name}</h2>
        <span className="text-[20px] font-semibold leading-none tracking-tight tabular-nums">{value}</span>
      </div>
      <p className="mt-2 text-[14px] text-ink-2">{children}</p>
    </Panel>
  );
}

export function ThresholdsView() {
  const { data, error, updated } = usePoll<Thresholds>("/api/thresholds", 10000);

  return (
    <>
      <PageHeader
        title="Thresholds"
        lead={
          <>
            The numbers in your policy file, beside how recent calls scored against them. Each dot
            is one call; the line is the threshold, and the shaded side is where a call crosses it.
          </>
        }
        aside={<Live updated={updated} error={error} />}
      />
      {error ? <Problem>{error}</Problem> : null}
      {!data ? (
        <Empty title={error ? "No data" : "Loading the policy"} />
      ) : data.problem !== null ? (
        <>
          <Problem>
            The policy file <span className="font-mono">{data.policy}</span> could not be read:{" "}
            {data.problem}
          </Problem>
          <p className="text-[14px] text-ink-2">
            Start the dashboard with <Code>--policy</Code> pointing at the file the proxy runs with.
          </p>
        </>
      ) : (
        <>
          <p className="mb-4 text-[13px] text-muted">
            From <span className="font-mono">{data.policy}</span>, against the latest {data.judged}{" "}
            calls that were put to the model.
          </p>
          <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
            {data.thresholds.map((threshold) => (
              <ThresholdCard key={threshold.name} threshold={threshold} />
            ))}
          </div>
          <h2 className="mb-3 mt-8 text-[16px] font-semibold">Other settings</h2>
          <div className="grid gap-4 md:grid-cols-3">
            <Setting name="confidence" value={data.confidence.toFixed(2)}>
              An answer less confident than this counts as uncertain, and the call is not decided
              on it.
            </Setting>
            <Setting
              name="result_scan.injected"
              value={data.result_scan.enabled ? data.result_scan.injected.toFixed(2) : "Off"}
            >
              {data.result_scan.enabled
                ? "A tool result scoring above this is withheld from the agent."
                : "The scan of tool results is switched off in this policy."}
            </Setting>
            <Setting name="escalation" value={data.second_model.enabled ? "On" : "Off"}>
              {data.second_model.enabled
                ? `Uncertain calls go to ${data.second_model.model} (${data.second_model.provider}) before a person.`
                : "Uncertain calls go straight to a person."}
            </Setting>
          </div>
        </>
      )}
    </>
  );
}
