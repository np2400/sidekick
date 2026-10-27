/**
 * The one place the web app gets data from.
 *
 * Today Sidekick Web is a UI preview: nothing here talks to the Sidekick backend, and nothing is invented.
 * Every function answers "unavailable" with a reason and, where it's true, how to do the same thing today
 * on Telegram. When the real backend API exists, these functions call it and the pages don't change.
 */

export type Unavailable = { state: "unavailable"; reason: string; today?: string };
export type Ready<T> = { state: "ready"; data: T };
export type Result<T> = Ready<T> | Unavailable;

export type Session = { name: string; email: string; picture?: string };
export type Conversation = { id: string; title: string; updatedAt: string };
export type Reminder = { id: number; text: string; next: string; repeat: string };
export type Task = { id: number; title: string; due: string | null };
export type Brief = { date: string; text: string };
export type Connection = { app: "calendar" | "gmail"; connected: boolean; account?: string };
export type Preferences = { timezone: string; briefTime: string | null };

/** False until the web app is linked to the real backend: pages use it to label themselves as a preview. */
export const BACKEND_CONNECTED = false;

const NOT_LINKED = "Sidekick Web isn't connected to your Sidekick account yet.";

function unavailable(reason: string, today?: string): Unavailable {
  return { state: "unavailable", reason, today };
}

export async function getSession(): Promise<Result<Session>> {
  return unavailable("Google sign-in isn't connected yet.");
}

export async function listConversations(): Promise<Result<Conversation[]>> {
  return unavailable(NOT_LINKED, "Your conversations happen on Telegram for now.");
}

export async function sendMessage(_text: string, _files: File[]): Promise<Result<never>> {
  return unavailable("Chat isn't connected to the assistant yet, so nothing was sent.",
    "Message Sidekick on Telegram to use it today.");
}

export async function getBrief(): Promise<Result<Brief>> {
  return unavailable(NOT_LINKED, "/brief");
}

export async function listReminders(): Promise<Result<Reminder[]>> {
  return unavailable(NOT_LINKED, "/reminders");
}

export async function listTasks(): Promise<Result<Task[]>> {
  return unavailable(NOT_LINKED, "\"what are my tasks?\"");
}

export async function getConnections(): Promise<Result<Connection[]>> {
  return unavailable("Connecting Google from the web isn't available yet.", "/connect  ·  /connect gmail");
}

export async function getPreferences(): Promise<Result<Preferences>> {
  return unavailable(NOT_LINKED, "/timezone  ·  /brieftime");
}
