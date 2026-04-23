"use client";

import { FormEvent, useEffect, useMemo, useRef, useState } from "react";
import { Send, X } from "lucide-react";
import { AdminTokenCard } from "@/components/admin/admin-token-card";
import { Button } from "@/components/ui/button";
import {
  Sheet,
  SheetBody,
  SheetContent,
  SheetDescription,
  SheetFooter,
  SheetHeader,
  SheetTitle,
  SheetTrigger,
} from "@/components/ui/sheet";
import { sendResearchQuery } from "@/lib/api";
import { useAdminToken } from "@/lib/admin-token";
import { useHealthStatus, getSyncState } from "@/lib/health-status";
import type { AnalystChatResponse, ResearchCitationRead } from "@/lib/types";
import { fmtRelative, cn } from "@/lib/utils";

interface Message {
  role: "user" | "assistant";
  content: string;
  citations?: ResearchCitationRead[];
  usedWebSearch?: boolean;
  mode?: AnalystChatResponse["mode"];
}

const STARTER_MESSAGES = [
  "What are the strongest NBA or MLB picks today?",
  "Why was the last auto-trade run skipped?",
  "Is model readiness good enough for live trading?",
  "Summarize my open portfolio risk.",
];

/**
 * Cosmos v2 (phase 2) — Analyst drawer reskin.
 *
 * Wiring kept identical to v1:
 *   - useAdminToken gating + AdminTokenCard entry flow
 *   - sendResearchQuery(admin.token, { message }) on submit
 *   - busy / error / messages state shape unchanged
 *
 * What changed
 *   - Floating trigger is now an orbit-avatar pill ("Ask the analyst · reading
 *     live state") instead of the generic Bot button
 *   - Drawer header carries the orbit avatar, a live-dot subtitle, and a
 *     context-chip strip (market sync relative time from /health)
 *   - Assistant messages render with a mini-orbit avatar and a bubble w/
 *     optional metadata row
 *   - Prompt cards get ✦/→ decorators and a hover glow
 *   - Send button has the cosmos violet glow treatment
 */
export function AnalystChatDrawer() {
  const admin = useAdminToken();
  const [open, setOpen] = useState(false);
  const [input, setInput] = useState("");
  const [messages, setMessages] = useState<Message[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const messagesEndRef = useRef<HTMLDivElement | null>(null);

  // Auto-scroll to newest message when opened or when messages change
  useEffect(() => {
    if (!open) return;
    messagesEndRef.current?.scrollIntoView({ block: "end" });
  }, [open, messages, busy]);

  const ask = async (message: string) => {
    const trimmed = message.trim();
    if (!trimmed || busy) return;
    setMessages((current) => [...current, { role: "user", content: trimmed }]);
    setInput("");
    setError(null);
    setBusy(true);
    try {
      const response = await sendResearchQuery(admin.token, { message: trimmed });
      setMessages((current) => [
        ...current,
        {
          role: "assistant",
          content: response.message,
          citations: response.citations,
          usedWebSearch: response.used_web_search,
          mode: response.mode,
        },
      ]);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const handleSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    ask(input);
  };

  return (
    <Sheet open={open} onOpenChange={setOpen}>
      <SheetTrigger asChild>
        <button
          type="button"
          aria-label="Open analyst chat"
          className={cn(
            "group fixed bottom-5 right-5 z-40 inline-flex items-center gap-2.5 rounded-full px-3.5 py-2",
            "text-[12px] font-semibold text-white",
            // Cosmos violet → cyan glow pill
            "bg-[linear-gradient(135deg,hsl(262_60%_48%),hsl(272_65%_58%))]",
            "shadow-[0_10px_30px_hsl(262_70%_55%/0.35),0_0_0_1px_hsl(262_70%_70%/0.45)]",
            "transition-[transform,box-shadow] duration-200",
            "hover:-translate-y-0.5",
            "hover:shadow-[0_14px_36px_hsl(262_70%_55%/0.45),0_0_0_1px_hsl(262_70%_70%/0.6)]",
            "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-cosmos-violet/60 focus-visible:ring-offset-2 focus-visible:ring-offset-background",
          )}
          data-no-sky-drag
        >
          <TriggerOrb />
          <span className="flex flex-col items-start leading-tight">
            <span>Ask the analyst</span>
            <span className="text-[10px] font-normal uppercase tracking-[0.06em] text-white/75">
              read-only · live state
            </span>
          </span>
        </button>
      </SheetTrigger>

      <SheetContent
        side="right"
        className={cn(
          "w-[420px] max-w-[92vw] gap-0 p-0 sm:w-[420px]",
          // Cosmos panel
          "border-l border-white/10",
          "dark:bg-[linear-gradient(180deg,hsl(250_55%_6%/0.92),hsl(250_55%_4%/0.92))]",
          "dark:backdrop-blur-[18px]",
          // shadcn's default close button sits top-right; we render our own in header
          "[&>button[aria-label=Close]]:hidden",
        )}
        data-no-sky-drag
      >
        <CosmosHeader onClose={() => setOpen(false)} />
        <ContextChipsRow />

        <SheetBody className="flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto px-4 py-3">
          {!admin.loaded ? (
            <p className="text-sm text-muted-foreground">Loading owner access…</p>
          ) : !admin.hasToken ? (
            <AdminTokenCard
              title="Owner Token"
              description="Enter the owner admin token to ask account-aware questions."
              onSubmit={admin.setToken}
            />
          ) : (
            <>
              {/* Welcome bubble (always first, even once messages exist — acts as intro) */}
              {messages.length === 0 && <WelcomeBubble />}

              {messages.map((message, index) => (
                <MessageBubble key={index} message={message} />
              ))}

              {busy && <TypingBubble />}

              {/* Prompt cards — only before first user message */}
              {messages.length === 0 && (
                <PromptCards
                  prompts={STARTER_MESSAGES}
                  onPick={(p) => ask(p)}
                />
              )}

              <div ref={messagesEndRef} />

              {error && (
                <p className="rounded-md border border-negative/25 bg-negative/10 px-3 py-2 text-sm text-negative">
                  {error}
                </p>
              )}
            </>
          )}
        </SheetBody>

        <SheetFooter className="items-stretch border-t border-white/5 bg-[hsl(250_55%_4%/0.55)] px-4 py-3">
          {admin.hasToken ? (
            <form onSubmit={handleSubmit} className="flex w-full flex-col gap-1.5">
              <div
                className={cn(
                  "flex items-end gap-2 rounded-lg border border-white/10 bg-[hsl(250_55%_8%/0.85)] p-1.5",
                  "focus-within:border-cosmos-violet/50 focus-within:shadow-[0_0_0_3px_hsl(262_70%_55%/0.15)]",
                  "transition-shadow",
                )}
              >
                <textarea
                  value={input}
                  onChange={(event) => setInput(event.target.value)}
                  onKeyDown={(event) => {
                    if (event.key === "Enter" && !event.shiftKey) {
                      event.preventDefault();
                      ask(input);
                    }
                    if (event.key === "Escape") {
                      setOpen(false);
                    }
                  }}
                  placeholder="Ask about tonight's picks, a run, or your portfolio…"
                  rows={1}
                  className={cn(
                    "min-h-9 flex-1 resize-none bg-transparent px-2 py-1.5 text-sm text-foreground",
                    "placeholder:text-muted-foreground/70",
                    "focus:outline-none",
                  )}
                />
                <Button
                  type="submit"
                  size="icon"
                  aria-label="Send"
                  disabled={busy || !input.trim()}
                  className={cn(
                    "h-8 w-8 shrink-0 rounded-md",
                    "bg-[linear-gradient(135deg,hsl(262_60%_48%),hsl(272_65%_58%))]",
                    "text-white shadow-[0_0_0_1px_hsl(262_70%_70%/0.35),0_6px_16px_hsl(262_70%_55%/0.35)]",
                    "transition-[transform,box-shadow,opacity]",
                    "hover:-translate-y-0.5",
                    "hover:shadow-[0_0_0_1px_hsl(262_70%_70%/0.55),0_8px_20px_hsl(262_70%_55%/0.45)]",
                    "disabled:opacity-40 disabled:hover:translate-y-0",
                  )}
                >
                  <Send size={13} />
                </Button>
              </div>
              <p className="px-1 font-mono text-[10.5px] uppercase tracking-[0.06em] text-muted-foreground/80">
                ↵ send · shift+↵ newline · esc close · read-only
              </p>
            </form>
          ) : null}
        </SheetFooter>
      </SheetContent>
    </Sheet>
  );
}

/* ---------- Header ---------- */

function CosmosHeader({ onClose }: { onClose: () => void }) {
  return (
    <SheetHeader className="flex-row items-center gap-3 border-b border-white/5 bg-[hsl(250_55%_4%/0.55)] px-4 py-3">
      <HeaderOrb />
      <div className="flex min-w-0 flex-1 flex-col">
        <SheetTitle className="text-[14px] font-semibold text-foreground">
          Analyst
        </SheetTitle>
        <SheetDescription asChild>
          <span className="mt-0.5 flex items-center gap-1.5 text-[11.5px] text-muted-foreground">
            <span className="cosmos-live-dot" />
            read-only · answers from live state
          </span>
        </SheetDescription>
      </div>
      <button
        type="button"
        onClick={onClose}
        aria-label="Close analyst"
        className={cn(
          "inline-flex h-7 w-7 items-center justify-center rounded-md",
          "text-muted-foreground transition-colors hover:bg-white/5 hover:text-foreground",
        )}
      >
        <X size={15} />
      </button>
    </SheetHeader>
  );
}

/* ---------- Context chips ---------- */

function ContextChipsRow() {
  const { data: health } = useHealthStatus();
  const syncState = getSyncState(health);

  const chips = useMemo(() => {
    const out: { label: string; tone?: "hot" | "pos" | "warn" | "neg"; title?: string }[] = [];

    // Market freshness
    if (health?.last_successful_refresh_at) {
      const rel = fmtRelative(health.last_successful_refresh_at)
        .replace(/^about /, "")
        .replace(/ ago$/, "");
      out.push({
        label: `market · ${rel}`,
        tone:
          syncState === "synced"
            ? "pos"
            : syncState === "stalled" || syncState === "failed"
            ? "neg"
            : syncState === "stale"
            ? "warn"
            : undefined,
        title: `Market data last refreshed ${fmtRelative(health.last_successful_refresh_at)}`,
      });
    } else {
      out.push({ label: "market · awaiting sync", tone: "warn" });
    }

    // Maintenance freshness
    if (health?.last_prop_refresh_at) {
      const rel = fmtRelative(health.last_prop_refresh_at)
        .replace(/^about /, "")
        .replace(/ ago$/, "");
      out.push({
        label: `props · ${rel}`,
        tone: health.prop_data_stale ? "warn" : undefined,
      });
    }

    // Active refresh job if any
    if (health?.active_refresh_job) {
      out.push({
        label: `refresh · ${health.active_refresh_job.scope ?? "running"}`,
        tone: "hot",
        title: `Refresh job ${health.active_refresh_job.scope ?? ""} queued or running`,
      });
    }

    return out;
  }, [health, syncState]);

  if (chips.length === 0) return null;

  return (
    <div className="flex flex-wrap gap-1.5 border-b border-white/5 bg-[hsl(250_55%_4%/0.4)] px-4 py-2.5">
      {chips.map((c, i) => (
        <span
          key={i}
          title={c.title}
          className={cn(
            "inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 font-mono text-[10.5px] leading-none",
            c.tone === "pos" &&
              "border-positive/30 bg-positive/10 text-positive",
            c.tone === "hot" &&
              "border-cosmos-violet/35 bg-cosmos-violet/10 text-[hsl(272_80%_82%)]",
            c.tone === "warn" &&
              "border-warning/30 bg-warning/10 text-warning",
            c.tone === "neg" &&
              "border-negative/30 bg-negative/10 text-negative",
            !c.tone && "border-white/10 bg-white/[0.03] text-muted-foreground",
          )}
        >
          <span className="h-1 w-1 rounded-full bg-current" />
          {c.label}
        </span>
      ))}
    </div>
  );
}

/* ---------- Bubbles ---------- */

function WelcomeBubble() {
  return (
    <div className="flex items-start gap-2">
      <MiniOrb />
      <div
        className={cn(
          "max-w-[88%] rounded-lg border border-white/10 bg-white/[0.03] px-3 py-2 text-sm leading-relaxed",
        )}
      >
        <p>
          Evening. I can walk you through tonight&apos;s board — strongest picks,
          model readiness, stale data, skipped runs, or account snapshots.
          Nothing I say leaves this drawer, and I can only read current state.
        </p>
        <p className="mt-2 flex flex-wrap gap-3 font-mono text-[10.5px] uppercase tracking-[0.06em] text-muted-foreground/80">
          <span>context · trade · portfolio · stats</span>
          <span>read-only</span>
        </p>
      </div>
    </div>
  );
}

function MessageBubble({ message }: { message: Message }) {
  if (message.role === "user") {
    return (
      <div className="flex justify-end">
        <div
          className={cn(
            "max-w-[88%] rounded-lg border border-cosmos-violet/30 bg-[hsl(262_60%_48%/0.12)] px-3 py-2 text-sm leading-relaxed text-foreground",
          )}
        >
          <p className="whitespace-pre-wrap">{message.content}</p>
        </div>
      </div>
    );
  }
  return (
    <div className="flex items-start gap-2">
      <MiniOrb />
      <div
        className={cn(
          "max-w-[88%] rounded-lg border border-white/10 bg-white/[0.03] px-3 py-2 text-sm leading-relaxed text-foreground",
        )}
      >
        {(message.usedWebSearch || message.mode === "internal_fallback") && (
          <div className="mb-2 flex flex-wrap gap-1.5 font-mono text-[10.5px] uppercase tracking-[0.06em] text-muted-foreground/80">
            {message.usedWebSearch ? (
              <span className="rounded-full border border-cosmos-violet/35 bg-cosmos-violet/10 px-2 py-0.5 text-[hsl(272_80%_82%)]">
                web verified
              </span>
            ) : null}
            {message.mode === "internal_fallback" ? (
              <span className="rounded-full border border-warning/30 bg-warning/10 px-2 py-0.5 text-warning">
                internal fallback
              </span>
            ) : null}
          </div>
        )}
        <p className="whitespace-pre-wrap">{message.content}</p>
        {message.citations?.length ? (
          <div className="mt-3 border-t border-white/10 pt-2">
            <p className="mb-1 font-mono text-[10.5px] uppercase tracking-[0.08em] text-muted-foreground/80">
              Sources
            </p>
            <div className="flex flex-col gap-1.5">
              {message.citations.map((citation, index) => (
                <a
                  key={`${citation.url}-${index}`}
                  href={citation.url}
                  target="_blank"
                  rel="noreferrer"
                  className="text-xs text-cosmos-violet transition-colors hover:text-cosmos-violet-light hover:underline"
                >
                  {index + 1}. {citation.title}
                </a>
              ))}
            </div>
          </div>
        ) : null}
      </div>
    </div>
  );
}

function TypingBubble() {
  return (
    <div className="flex items-start gap-2">
      <MiniOrb />
      <div className="inline-flex items-center gap-1.5 rounded-lg border border-white/10 bg-white/[0.03] px-3 py-2 text-sm text-muted-foreground">
        <span className="h-1 w-1 animate-pulse rounded-full bg-current [animation-delay:0ms]" />
        <span className="h-1 w-1 animate-pulse rounded-full bg-current [animation-delay:180ms]" />
        <span className="h-1 w-1 animate-pulse rounded-full bg-current [animation-delay:360ms]" />
        <span className="ml-1 font-mono text-[11px] uppercase tracking-wider text-muted-foreground/80">
          reading context
        </span>
      </div>
    </div>
  );
}

/* ---------- Prompt cards ---------- */

function PromptCards({ prompts, onPick }: { prompts: string[]; onPick: (p: string) => void }) {
  return (
    <div className="mt-1 flex flex-col gap-1.5">
      <p className="px-1 font-mono text-[10.5px] uppercase tracking-[0.08em] text-muted-foreground/80">
        Try one —
      </p>
      {prompts.map((p) => (
        <button
          key={p}
          type="button"
          onClick={() => onPick(p)}
          className={cn(
            "group flex items-center gap-2.5 rounded-lg border border-white/10 bg-white/[0.02] px-3 py-2 text-left text-[13px] text-foreground",
            "transition-[background,border-color,transform] duration-150",
            "hover:-translate-y-px hover:border-cosmos-violet/35 hover:bg-cosmos-violet/[0.06]",
          )}
        >
          <span className="text-[12px] text-cosmos-violet">✦</span>
          <span className="flex-1">{p}</span>
          <span className="text-muted-foreground transition-transform group-hover:translate-x-0.5">
            →
          </span>
        </button>
      ))}
    </div>
  );
}

/* ---------- Orbit avatars ---------- */

function HeaderOrb() {
  return (
    <span
      aria-hidden
      className="orbit relative h-9 w-9 shrink-0 rounded-full border border-cosmos-violet/25 bg-[radial-gradient(circle_at_30%_25%,hsl(262_70%_22%),hsl(250_55%_6%))] shadow-[0_0_0_1px_hsl(262_70%_70%/0.2),0_0_20px_hsl(262_70%_55%/0.35)]"
    >
      <span className="orbit-core" style={{ width: 6, height: 6, margin: "-3px 0 0 -3px" }} />
      <span className="orbit-ring" />
      <span className="orbit-ring orbit-ring-2" />
    </span>
  );
}

function TriggerOrb() {
  return (
    <span
      aria-hidden
      className="orbit relative h-5 w-5 shrink-0 rounded-full bg-white/10"
    >
      <span
        className="orbit-core"
        style={{
          width: 5,
          height: 5,
          margin: "-2.5px 0 0 -2.5px",
          background: "radial-gradient(circle at 30% 30%, #fff, #e8ddff 50%, #9876ff 100%)",
        }}
      />
      <span className="orbit-ring" style={{ borderColor: "rgba(255,255,255,0.7)" }} />
    </span>
  );
}

function MiniOrb() {
  return (
    <span
      aria-hidden
      className="orbit mt-0.5 h-6 w-6 shrink-0 rounded-full border border-cosmos-violet/25 bg-[radial-gradient(circle_at_30%_25%,hsl(262_70%_18%),hsl(250_55%_5%))]"
    >
      <span className="orbit-core" style={{ width: 4, height: 4, margin: "-2px 0 0 -2px" }} />
      <span className="orbit-ring" />
    </span>
  );
}
