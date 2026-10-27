/**
 * The one place the web app gets data from: Sidekick's API (app/web_api.py), same domain, signed in with the
 * session cookie (HttpOnly: this code never sees it). Read-only for now (W3b).
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
export type Transcript = { id: number; title: string; origin: "telegram" | "web"; messages: Message[] };
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

export async function sendMessage(_text: string, _files: File[]): Promise<Result<never>> {
  return unavailable("Sending messages from the web comes in the next step, so nothing was sent.",
    "Message Sidekick in Telegram: your conversations show up here too.");
}

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
