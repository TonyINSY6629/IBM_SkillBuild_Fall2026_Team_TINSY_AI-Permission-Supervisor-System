import { createFileRoute } from "@tanstack/react-router";
import { useEffect, useRef, useState } from "react";

export const Route = createFileRoute("/")({
  head: () => ({
    meta: [
      { title: "Bob Supervisor — Escalation" },
      {
        name: "description",
        content:
          "607×341 approval pop-up for AI agent tool calls escalated by the Permission Supervisor.",
      },
      { property: "og:title", content: "Bob Supervisor — Escalation" },
      { property: "og:description", content: "Approve or deny escalated AI agent tool calls." },
      { property: "og:type", content: "website" },
      { name: "twitter:card", content: "summary" },
    ],
  }),
  component: Index,
});

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

type Esc = {
  tool_use_id: string;
  session_id: string;
  tool_name: string;
  action: string;
  targets: string[];
  label: "Warning" | "Neutral" | "Safe";
  category: string;
  inside_workspace: boolean;
  touches_secrets: boolean;
  reason: string;
  confidence: number | null;
  rule?: string;
  prompt: string;
  tool_input: object;
  policy_version: string;
  matched_rule_id: string | null;
  model_version_id: string | null;
};

//NEW BELOW!!
type View = "pending" | "allowed" | "denied" | "timeout" | "failclosed" | "resolved";
//NEW ABOVE!!




// ---------------------------------------------------------------------------
// Demo samples (used when no ?id= query param is present)
// ---------------------------------------------------------------------------

const SAMPLES: Esc[] = [
  {
    tool_use_id: "toolu_01H8xQ",
    session_id: "sess_7f3a",
    tool_name: "Bash",
    action: "Delete files",
    targets: ["rm -rf ./build ../shared/cache"],
    label: "Warning",
    category: "danger_fs",
    inside_workspace: false,
    touches_secrets: false,
    reason: "Recursive delete reaches a folder outside the workspace root.",
    confidence: 0.62,
    prompt: "Clean up the build output and rebuild the project",
    tool_input: { command: "rm -rf ./build ../shared/cache" },
    policy_version: "2026.11.3",
    matched_rule_id: null,
    model_version_id: "student-1.3b-q4-v7",
  },
  {
    tool_use_id: "toolu_02K1pZ",
    session_id: "sess_7f3a",
    tool_name: "Read",
    action: "Read file",
    targets: [".env.production"],
    label: "Warning",
    category: "secrets_access",
    inside_workspace: true,
    touches_secrets: true,
    reason: "File matches a secrets path pattern.",
    confidence: 0.48,
    rule: "Reading .env files needs a human",
    prompt: "Why does the deploy fail on staging?",
    tool_input: { file_path: "/repo/.env.production" },
    policy_version: "2026.11.3",
    matched_rule_id: "R-114",
    model_version_id: "student-1.3b-q4-v7",
  },
];

// ---------------------------------------------------------------------------
// Helpers — parse ?id= and ?api= from the URL
// ---------------------------------------------------------------------------

function getLiveParams(): { id: string; api: string } | null {
  if (typeof window === "undefined") return null;
  const p = new URLSearchParams(window.location.search);
  const id = p.get("id");
  const api = p.get("api") ?? "http://127.0.0.1:9001";
  return id ? { id, api: api.replace(/\/$/, "") } : null;
}

/** Map the /escalation/{id}/data JSON response to our Esc type. */
function dataToEsc(d: Record<string, unknown>): Esc {
  const det = (d.details ?? {}) as Record<string, unknown>;
  return {
    tool_use_id: String(det.tool_use_id ?? d.escalation_id ?? ""),
    session_id: String(det.session_id ?? ""),
    tool_name: String(d.tool_name ?? ""),
    action: String(det.action_type ?? "tool call"),
    targets: Array.isArray(det.targets) ? (det.targets as string[]) : [],
    label: (det.label as Esc["label"]) ?? "Warning",
    category: String(d.category ?? ""),
    inside_workspace: Boolean(det.inside_workspace ?? true),
    touches_secrets: Boolean(det.touches_secrets ?? false),
    reason: String(d.reason ?? ""),
    confidence: typeof det.confidence === "number" ? det.confidence : null,
    rule: det.matched_rule_id ? `Rule ${det.matched_rule_id}` : undefined,
    prompt: String(det.prompt_text ?? ""),
    tool_input: (det.tool_input ?? {}) as object,
    policy_version: String(det.policy_version ?? ""),
    matched_rule_id: det.matched_rule_id ? String(det.matched_rule_id) : null,
    model_version_id: det.model_version_id ? String(det.model_version_id) : null,
  };
}

// ---------------------------------------------------------------------------
// Size tokens
// ---------------------------------------------------------------------------

const SIZES = {
  big: {
    head: "text-[12px]",
    timer: "text-[12px]",
    title: "text-base",
    target: "text-[12px]",
    badge: "text-[11px] px-2 py-0.5",
    body: "text-[12px]",
    prompt: "text-[11px]",
    note: "text-[12px]",
    btn: "text-[12px] px-3 py-1",
    link: "text-[11px]",
    stateTitle: "text-xl",
    stateBody: "text-[12px]",
    stateCode: "text-[11px]",
    stateReplay: "text-[12px]",
  },
};

// ---------------------------------------------------------------------------
// Popup component
// ---------------------------------------------------------------------------

function Popup({
  esc,
  forced,
  scale,
  liveParams,
  serverTimeout,
}: {
  esc: Esc;
  forced?: View;
  scale: number;
  liveParams: { id: string; api: string } | null;
  serverTimeout: number;
}) {
  const [view, setView] = useState<View>("pending");
  const [left, setLeft] = useState(serverTimeout);
  const [details, setDetails] = useState(false);
  const [note, setNote] = useState("");
  const [win, setWin] = useState<"open" | "min" | "closed">("open");
  // Tracks whether a live POST is in flight to prevent double-clicks
  const posting = useRef(false);

  const v = forced ?? view;
  const s = SIZES.big;
  const isLive = liveParams !== null && !forced;

  // Sync left to serverTimeout when it changes (live mode: value from /data)
  useEffect(() => {
    setLeft(serverTimeout);
  }, [serverTimeout]);

  // Countdown timer
  useEffect(() => {
    if (v !== "pending") return;
    const t = setInterval(() => setLeft((c) => (c <= 1 ? (setView("timeout"), 0) : c - 1)), 1000);
    return () => clearInterval(t);
  }, [v]);

  // Keyboard shortcuts (demo mode only — live mode uses the POST handlers)
  useEffect(() => {
    if (v !== "pending" || isLive) return;
    const k = (e: KeyboardEvent) => {
      const tag = (e.target as HTMLElement).tagName;
      if (tag === "INPUT" || tag === "TEXTAREA") return;
      if (e.key === "a") setView("allowed");
      if (e.key === "d") setView("denied");
    };
    window.addEventListener("keydown", k);
    return () => window.removeEventListener("keydown", k);
  }, [v, isLive]);

  // Live Allow / Deny — POST to the supervisor
  const postDecision = async (approved: boolean) => {
    if (!liveParams || posting.current) return;
    posting.current = true;
    const endpoint = `${liveParams.api}/escalation/${liveParams.id}/${approved ? "allow" : "deny"}`;
    try {
      const res = await fetch(endpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ note }),
      });
      const json = (await res.json()) as { ok?: boolean };
      if (json.ok === true) {
        setView(approved ? "allowed" : "denied");
      } else {
        // Supervisor didn't confirm — treat as fail-closed
        setView("failclosed");
      }
    } catch {
      setView("failclosed");
    } finally {
      posting.current = false;
    }
  };

  const reset = () => {
    setView("pending");
    setLeft(serverTimeout);
    setNote("");
    setDetails(false);
    setWin("open");
    posting.current = false;
  };

  const header = (
    <div className="flex items-center justify-between border-b border-ide-line px-3 py-1.5">
      <span className="flex items-center gap-2">
        {v === "pending" && (
          <span className={`font-mono ${s.timer} ${left <= 10 ? "text-danger" : "text-ide-dim"}`}>
            auto-deny in {left}s
          </span>
        )}
      </span>
      <span className="flex items-center gap-2">
        {!forced && (
          <button
            onClick={() => setWin(win === "min" ? "open" : "min")}
            title={win === "min" ? "Restore" : "Minimize"}
            className={`${s.head} leading-none text-ide-dim hover:text-ide-text`}
          >
            −
          </button>
        )}
        {!forced && (
          <button
            onClick={() => setWin("closed")}
            title="Close"
            className={`${s.head} leading-none text-ide-dim hover:text-danger`}
          >
            ×
          </button>
        )}
      </span>
    </div>
  );

  if (win === "closed") {
    return (
      <button onClick={() => setWin("open")} className={`${s.link} text-ide-dim underline`}>
        Pop-up closed — reopen
      </button>
    );
  }

  if (win === "min") {
    return (
      <div
        className="flex flex-col border border-ide-line bg-ide-panel font-sans shadow-2xl"
        style={{ width: 607 * scale }}
      >
        {header}
      </div>
    );
  }

  if (v !== "pending") {
    const m = {
      allowed: ["Allowed", "Bob is running the tool. Your decision was logged.", "text-safe"],
      denied: ["Denied", "Bob was told why and will try another way. Logged.", "text-danger"],
      //NEW!!
      resolved: ["Already decided", "This request was already answered or timed out. Check Bob for the result.", "text-ide-dim"],
      //NEW!!
      timeout: ["Denied automatically", "No answer in time — blocked for safety.", "text-warn"],
      failclosed: [
        "Blocked for safety",
        "Supervisor unreachable or the response couldn't be confirmed. The call was not run.",
        "text-danger",
      ],
      pending: ["", "", ""],
    }[v];
    return (
      <Frame scale={scale}>
        {header}
        <div className="flex min-h-0 flex-1 flex-col items-center justify-center gap-2 px-6 text-center">
          <div className={`${s.stateTitle} font-semibold ${m[2]}`}>{m[0]}</div>
          <p className={`${s.stateBody} text-ide-dim`}>{m[1]}</p>
          <code className={`font-mono ${s.stateCode} text-ide-dim`}>{esc.tool_use_id}</code>
          {!forced && !isLive && (
            <button onClick={reset} className={`mt-2 ${s.stateReplay} text-ide-dim underline`}>
              Replay
            </button>
          )}
        </div>
      </Frame>
    );
  }

  return (
    <Frame scale={scale}>
      {header}
      <div className="relative h-1 shrink-0 bg-ide-line">
        <div
          className="absolute inset-y-0 left-0 bg-warn transition-all"
          style={{ width: `${(left / serverTimeout) * 100}%` }}
        />
      </div>

      {details ? (
        <div
          className={`scrolly min-h-0 flex-1 overflow-y-auto px-3 py-2 font-mono ${s.link} leading-relaxed text-ide-dim`}
        >
          <pre className="whitespace-pre-wrap text-ide-text">
            {JSON.stringify(esc.tool_input, null, 2)}
          </pre>
          <div className="mt-2">
            policy {esc.policy_version} · rule {esc.matched_rule_id ?? "—"}
          </div>
          <div>model {esc.model_version_id ?? "rule engine"}</div>
          <div>
            tool_use {esc.tool_use_id} · session {esc.session_id}
          </div>
        </div>
      ) : (
        <div className="flex min-h-0 flex-1 flex-col gap-1.5 px-3 py-2">
          <div className={`${s.title} shrink-0 font-semibold text-ide-text`}>
            {esc.tool_name}: {esc.action.toLowerCase()}
          </div>
          <code
            className={`block shrink-0 truncate bg-ide px-2 py-1 font-mono ${s.target} text-ide-text`}
          >
            {esc.targets.join("  ")}
          </code>
          <div className="flex shrink-0 flex-wrap gap-1">
            <Badge cls="bg-warn/15 text-warn" size={s.badge}>
              {esc.label}
            </Badge>
            <Badge cls="bg-ide-line text-ide-text" size={s.badge}>
              {esc.category}
            </Badge>
            {!esc.inside_workspace && (
              <Badge cls="bg-danger/20 text-danger" size={s.badge}>
                Outside workspace
              </Badge>
            )}
            {esc.touches_secrets && (
              <Badge cls="bg-danger/20 text-danger" size={s.badge}>
                Touches secrets
              </Badge>
            )}
          </div>
          <p
            className={`scrolly min-h-0 flex-1 overflow-y-auto whitespace-pre-line pr-1.5 leading-snug text-ide-text ${s.body}`}
          >
            {esc.rule ? <span className="text-ide-dim">Rule: {esc.rule}. </span> : null}
            {esc.reason}
            {esc.confidence != null && (
              <span className="text-ide-dim"> Model {Math.round(esc.confidence * 100)}% sure.</span>
            )}
          </p>
          <p className={`shrink-0 truncate text-ide-dim ${s.prompt}`}>
            Your prompt: &ldquo;{esc.prompt}&rdquo;
          </p>
        </div>
      )}

      <div className="flex shrink-0 items-stretch gap-1.5 border-t border-ide-line px-3 py-2">
        <button
          onClick={() => setDetails(!details)}
          className={`${s.link} shrink-0 text-ide-dim underline`}
        >
          {details ? "Summary" : "Details"}
        </button>
        <input
          type="text"
          value={note}
          onChange={(e) => setNote(e.target.value)}
          placeholder="Note (optional)"
          className={`min-h-0 min-w-0 flex-1 border border-ide-line bg-ide px-2 py-1 leading-snug text-ide-text placeholder:text-ide-dim focus:outline-none ${s.note}`}
        />
        <button
          onClick={() => (isLive ? postDecision(false) : setView("denied"))}
          className={`shrink-0 border border-danger/60 text-danger hover:bg-danger/15 ${s.btn}`}
        >
          Deny {!isLive && <kbd className="font-bold opacity-60">D</kbd>}
        </button>
        <button
          onClick={() => (isLive ? postDecision(true) : setView("allowed"))}
          className={`shrink-0 border border-safe bg-safe font-semibold text-ide hover:opacity-90 ${s.btn}`}
        >
          Allow {!isLive && <kbd className="font-bold opacity-60">A</kbd>}
        </button>
      </div>
    </Frame>
  );
}

// ---------------------------------------------------------------------------
// Badge + Frame
// ---------------------------------------------------------------------------

function Badge({ children, cls, size }: { children: React.ReactNode; cls: string; size: string }) {
  return <span className={`font-medium ${size} ${cls}`}>{children}</span>;
}

function Frame({ children, scale }: { children: React.ReactNode; scale: number }) {
  return (
    <div
      className="flex flex-col overflow-hidden border border-ide-line bg-ide-panel font-sans shadow-2xl"
      style={{ width: 607 * scale, height: 341 * scale }}
    >
      {children}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Index — live mode vs demo mode
// ---------------------------------------------------------------------------

const DEMO_TIMEOUT = 30;

function Index() {
  const liveParams = getLiveParams();

  // scale = 10% of the user's actual screen resolution
    //NEW: removed the extra "/ 10" so the pop-up is 607 x 341 on a 1920 screen (was 61 x 34)
  const [scale, setScale] = useState(() =>
    typeof window !== "undefined" ? window.screen.width / 1920 : 1,
  );
  useEffect(() => {
    const update = () => setScale(window.screen.width / 1920);
    window.addEventListener("resize", update);
    return () => window.removeEventListener("resize", update);
  }, []);

  // --- Live mode: fetch /escalation/{id}/data ---
  const [liveEsc, setLiveEsc] = useState<Esc | null>(null);
  const [liveTimeout, setLiveTimeout] = useState(120);
  const [liveError, setLiveError] = useState<string | null>(null);

  useEffect(() => {
    if (!liveParams) return;
    const { id, api } = liveParams;
    fetch(`${api}/escalation/${id}/data`)
      .then((r) => {
        if (r.status === 410) throw new Error("resolved");
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json() as Promise<Record<string, unknown>>;
      })
      .then((d) => {
        setLiveEsc(dataToEsc(d));
        setLiveTimeout(typeof d.remaining === "number" ? d.remaining : 120);
      })
      .catch((e: Error) => {
        setLiveError(e.message === "resolved" ? "resolved" : "failclosed");
      });
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  // --- Live mode render ---
  if (liveParams) {
    if (liveError) {

      //NEW, allowed changed to resolved and the Popup below now shows the real escalation id
      const forced: View = liveError === "resolved" ? "resolved" : "failclosed";
      return (
        <main className="flex min-h-screen items-center justify-center bg-ide font-sans">
          <Popup
            esc={{ ...SAMPLES[0]!, tool_use_id: liveParams.id }}
            forced={forced}
            scale={scale}
            liveParams={null}
            serverTimeout={DEMO_TIMEOUT}
          />
        </main>
      );
    }
    if (!liveEsc) {
      return (
        <main className="flex min-h-screen items-center justify-center bg-ide font-sans">
          <span className="text-xs text-ide-dim">Loading…</span>
        </main>
      );
    }
    return (
      <main className="flex min-h-screen items-center justify-center bg-ide font-sans">
        <Popup esc={liveEsc} scale={scale} liveParams={liveParams} serverTimeout={liveTimeout} />
      </main>
    );
  }

  // --- Demo mode render ---
  const [i, setI] = useState(0);
  return (
    <main className="min-h-screen bg-ide px-6 py-10 font-sans text-ide-text">
      <div className="mx-auto max-w-5xl">
        <h1 className="text-xl font-semibold">Escalation pop-up · 10% of screen</h1>
        <p className="mt-1 text-sm text-ide-dim">
          Shown only when the Supervisor escalates. Press A / D or click. Try both sample calls.
        </p>
        <div className="mt-4 flex gap-2">
          {SAMPLES.map((s, n) => (
            <button
              key={s.tool_use_id}
              onClick={() => setI(n)}
              className={`border px-3 py-1 text-xs ${i === n ? "border-warn text-warn" : "border-ide-line text-ide-dim"}`}
            >
              {s.tool_name}: {s.action}
            </button>
          ))}
        </div>
        <div className="mt-4">
          <Popup
            key={i}
            esc={SAMPLES[i]!}
            scale={scale}
            liveParams={null}
            serverTimeout={DEMO_TIMEOUT}
          />
        </div>

        <h2 className="mt-12 text-sm font-semibold text-ide-dim">Other states</h2>
        <div className="mt-3 flex flex-col gap-6">
          {(["allowed", "denied", "timeout", "failclosed"] as View[]).map((v) => (
            <div key={v}>
              <div className="mb-1 font-mono text-[11px] text-ide-dim">{v}</div>
              <Popup
                esc={SAMPLES[0]!}
                forced={v}
                scale={scale}
                liveParams={null}
                serverTimeout={DEMO_TIMEOUT}
              />
            </div>
          ))}
        </div>
      </div>
    </main>
  );
}
