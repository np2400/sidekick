/**
 * The one place the web app talks to Sidekick: its API (app/web_api.py), same domain, signed in with the session
 * cookie (HttpOnly: this code never sees it). Reading since W3b; chatting since W5: a message is queued, and the
 * page asks jobStatus() until the answer is in the conversation.
 *
 * Nothing is invented: every call answers with real data, "signed out", or an honest "unavailable" with the
 * reason and, where it's true, how to do the same thing today in Telegram.
 */

export type Unavailable = { state: "unavailable"; reason: string; today?: string };
export type SignedOut = { state: "signed_out" };
export type Ready<T> = { state: "ready"; data: T };
export type Result<T> = Ready<T> | Unavailable | SignedOut;

export type Session = { name: string; role: "owner" | "member"; telegram: boolean; google: string | null };
export type Conversation = {
  id: number; title: string; origin: "telegram" | "web"; started: string; updated: string; messages: number;
};
export type Message = { role: "user" | "assistant"; text: string; channel: "telegram" | "web"; at: string };
export type Attached = { type: "image/jpeg" | "application/pdf"; name: string };
export type Waiting = { job_id: number; text: string; attachment: Attached | null };
export type Proposal = { id: number; action: string };
export type Transcript = {
  id: number; title: string; origin: "telegram" | "web"; messages: Message[];
  waiting: Waiting[];          // sent, still being answered
  proposal: Proposal | null;   // a change waiting for your yes or no
  recent_file: (Attached & { until: string }) | null;  // a photo or PDF more questions can still use
};
export type Sent = { conversation_id: number; job_id: number };
export type JobStatus = { status: "waiting" | "done" | "failed" };
export type Reminder = { id: number; text: string; next: string; repeat: string };
export type Task = { id: number; title: string; due: string | null };
export type Tasks = { tasks: Task[]; elsewhere?: string };
export type Brief = { date: string; text: string };
export type GoogleLink = { managed: "server" | "you"; calendar: boolean; gmail: boolean; email: string | null };
export type Connections = { google: GoogleLink; telegram: boolean; sign_in: string | null };
export type Preferences = { timezone: string; brief_time: string | null };

/** True now that the web app reads the real backend: pages stop calling themselves "not connected". */
export const BACKEND_CONNECTED = true;

function unavailable(reason: string, today?: string): Unavailable {
  return { state: "unavailable", reason, today };
}

async function get<T>(path: string): Promise<Result<T>> {
  let response: Response;
  try {
    response = await fetch(`/api/${path}`, { credentials: "same-origin", headers: { Accept: "application/json" } });
  } catch {
    return unavailable("Can't reach Sidekick right now. Check your connection and try again.");
  }
  if (response.status === 401) return { state: "signed_out" };
  if (response.status === 404) return unavailable("Not found. It may have been deleted.");
  if (!response.ok) return unavailable(`Sidekick couldn't load this (error ${response.status}). Try again soon.`);
  try {
    return { state: "ready", data: (await response.json()) as T };
  } catch {
    return unavailable("Sidekick sent something unexpected. Try again soon.");
  }
}

export const getSession = () => get<Session>("me");
export const listConversations = () => get<Conversation[]>("conversations");
export const getConversation = (id: string) => get<Transcript>(`conversations/${encodeURIComponent(id)}`);
export const listReminders = () => get<Reminder[]>("reminders");
export const listTasks = () => get<Tasks>("tasks");
export const getConnections = () => get<Connections>("connections");
export const getPreferences = () => get<Preferences>("settings");

const PROBLEMS: Record<number, string> = {
  403: "That didn't come from this page as Sidekick expected. Reload the page and try again.",
  404: "Not found. It may have been deleted.",
  409: "That message was already sent.",
  413: "That file is too large (5 MB at most).",
  422: "Sidekick couldn't take that message: it's too long (4,000 characters at most), or the file isn't a real photo or PDF.",
};

async function post<T>(path: string, body?: object): Promise<Result<T>> {
  const init: RequestInit = {
    method: "POST", credentials: "same-origin",
    headers: { Accept: "application/json", "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  };
  let response: Response;
  try {
    response = await fetch(`/api/${path}`, init);
  } catch {
    try {  // the network dropped: send the very same request once more (same request id, so it's queued once)
      response = await fetch(`/api/${path}`, init);
    } catch {
      return unavailable("Can't reach Sidekick right now. Check your connection and try again.");
    }
  }
  if (response.status === 401) return { state: "signed_out" };
  if (!response.ok) {
    return unavailable(PROBLEMS[response.status] ?? `Sidekick couldn't do that (error ${response.status}). Try again soon.`);
  }
  try {
    return { state: "ready", data: (await response.json()) as T };
  } catch {
    return unavailable("Sidekick sent something unexpected. Try again soon.");
  }
}

function requestId(): string {
  return typeof crypto.randomUUID === "function" ? crypto.randomUUID()
    : Array.from(crypto.getRandomValues(new Uint8Array(16)), (b) => b.toString(16).padStart(2, "0")).join("");
}

/** Send a message: into a conversation, or (null) as the first message of a new one. Answers right away with
 *  where it went; the reply comes later (jobStatus). The request id makes sending twice harmless.
 *  `attachment`: one photo (already made a JPEG) or PDF, as base64. */
export function sendMessage(conversationId: number | null, text: string,
                            attachment?: Attached & { data: string }): Promise<Result<Sent>> {
  const body = { text, request_id: requestId(), ...(attachment ? { attachment } : {}) };
  return conversationId === null ? post<Sent>("conversations", body)
    : post<Sent>(`conversations/${conversationId}/messages`, body);
}

export const jobStatus = (jobId: number) => get<JobStatus>(`jobs/${jobId}`);

/** ✅ or ❌ under a proposal. The server decides it exactly once; the reply says what happened. */
export const decideProposal = (kind: "approve" | "reject", actionId: number) =>
  post<{ reply: string }>(`actions/${kind}/${actionId}`);

/** The sidebar listens for this: a new conversation, or a new message moving one to the top. */
export const CONVERSATIONS_CHANGED = "sidekick:conversations-changed";
export const conversationsChanged = () => window.dispatchEvent(new Event(CONVERSATIONS_CHANGED));

export async function getBrief(): Promise<Result<Brief>> {
  return unavailable("Your morning brief comes to the web in a later step.", "/brief");
}

/** Sign out this browser. The server checks the request comes from Sidekick's own page (Origin). */
export async function signOut(): Promise<void> {
  try {
    await fetch("/auth/logout", { method: "POST", credentials: "same-origin" });
  } catch {
    // Offline: the cookie stays until it expires; the page still goes to sign-in.
  }
}
