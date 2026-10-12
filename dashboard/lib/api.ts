"use client";

import { useCallback, useEffect, useRef, useState } from "react";

export type Kind = "ok" | "held" | "stopped" | "warned";

export type Call = {
  time: string;
  tool: string;
  upstream: string;
  mode: string;
  band: string | null;
  outcome: string;
  kind: Kind;
  reasons: string[];
  error: string | null;
  approval_id: number | null;
  user_request: string | null;
  arguments: string;
  scores: Record<string, number>;
  model: string | null;
  cached: boolean;
  latency_ms: number | null;
  second_model: string | null;
  second_model_error: string | null;
  result_action: string | null;
  injected: number | null;
  withheld_result: string | null;
};

export type Decisions = {
  calls: Call[];
  totals: { outcome: string; kind: Kind; count: number }[];
  waiting: number;
};

export type HeldCall = {
  id: number;
  tool: string;
  upstream: string;
  reasons: string[];
  user_request: string | null;
  arguments: string;
  created: string;
  expires: string;
  seconds_left: number;
};

export type Threshold = {
  name: string;
  value: number;
  answer: string;
  direction: "at or above" | "above" | "below";
  effect: string;
  scale_max: number;
  scores: number[];
  judged: number;
  crossed: number;
  nearest: number | null;
};

export type Thresholds =
  | { policy: string; problem: string }
  | {
      policy: string;
      problem: null;
      judged: number;
      thresholds: Threshold[];
      confidence: number;
      result_scan: { enabled: boolean; injected: number };
      second_model: { enabled: boolean; provider: string; model: string };
    };

export type Session = { token: string; state_dir: string; policy: string };

async function get<T>(path: string): Promise<T> {
  const response = await fetch(path, { cache: "no-store" });
  if (!response.ok) {
    throw new Error(`The dashboard's API answered ${response.status}.`);
  }
  return response.json();
}

/** Fetch a path now and again every `everyMs`, keeping the last good answer on screen. */
export function usePoll<T>(path: string, everyMs: number) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [updated, setUpdated] = useState<Date | null>(null);
  const live = useRef(true);

  const refresh = useCallback(async () => {
    try {
      const next = await get<T>(path);
      if (!live.current) return;
      setData(next);
      setError(null);
      setUpdated(new Date());
    } catch (problem) {
      if (!live.current) return;
      setError(
        problem instanceof TypeError
          ? "Cannot reach the dashboard's API. Is tollgate-dashboard still running?"
          : String((problem as Error).message),
      );
    }
  }, [path]);

  useEffect(() => {
    live.current = true;
    refresh();
    const timer = setInterval(refresh, everyMs);
    return () => {
      live.current = false;
      clearInterval(timer);
    };
  }, [refresh, everyMs]);

  return { data, error, updated, refresh };
}

/** Approve or deny a held call. The token proves the request came from these pages. */
export async function answerCall(
  id: number,
  decision: "approve" | "deny",
): Promise<{ changed: boolean; note: string }> {
  const session = await get<Session>("/api/session");
  const response = await fetch(`/api/approvals/${id}`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-Tollgate-Token": session.token,
    },
    body: JSON.stringify({ decision }),
  });
  if (!response.ok) {
    return {
      changed: false,
      note: `Call #${id} was not changed: the dashboard answered ${response.status}.`,
    };
  }
  return response.json();
}

export function timeOfDay(iso: string): string {
  return new Date(iso).toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

export function dayAndTime(iso: string): string {
  return new Date(iso).toLocaleString([], {
    day: "numeric",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

/** "45 seconds", "12 minutes", "2 hours": how long is left before a held call is refused. */
export function timeLeft(seconds: number): string {
  if (seconds < 90) return `${seconds} seconds`;
  if (seconds < 5400) return `${Math.round(seconds / 60)} minutes`;
  return `${Math.round(seconds / 3600)} hours`;
}

/** "12 s ago", "4 min ago", "3 h ago", then the date. */
export function ago(iso: string, now: Date): string {
  const seconds = Math.max(0, Math.round((now.getTime() - new Date(iso).getTime()) / 1000));
  if (seconds < 60) return `${seconds} s ago`;
  if (seconds < 3600) return `${Math.round(seconds / 60)} min ago`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)} h ago`;
  return new Date(iso).toLocaleDateString([], { day: "numeric", month: "short" });
}
