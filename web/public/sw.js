// Sidekick's service worker (W6a): shows the notifications the server sends (app/push.py), also while the app is
// closed, and opens the app when one is tapped. Nothing is cached for offline use: Sidekick needs its server anyway.

self.addEventListener("install", () => self.skipWaiting());  // a new version takes over at once
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));

self.addEventListener("push", (event) => {
  let data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch {
    data = { body: event.data ? event.data.text() : "" };
  }
  const scope = self.registration.scope;  // e.g. https://<domain>/app/
  // Every push must show a notification: browsers (Safari especially) stop delivering to apps that don't.
  event.waitUntil(self.registration.showNotification(data.title || "Sidekick", {
    body: data.body || "",
    tag: data.tag || undefined,  // the same tag replaces an earlier notification instead of adding another
    icon: `${scope}icons/icon-192.png`,
    data: { url: sameSite(data.url, scope) },
  }));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const url = sameSite(event.notification.data && event.notification.data.url, self.registration.scope);
  event.waitUntil((async () => {
    const windows = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    const open = windows.find((w) => w.url.startsWith(self.registration.scope));
    if (!open) return self.clients.openWindow(url);
    await open.focus();
    return "navigate" in open ? open.navigate(url) : undefined;
  })());
});

/** A link from a notification only ever opens a page of this app. */
function sameSite(url, scope) {
  try {
    const target = new URL(url || scope, scope);
    return target.origin === self.location.origin ? target.href : scope;
  } catch {
    return scope;
  }
}
