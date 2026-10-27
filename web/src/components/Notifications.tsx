import { useCallback, useEffect, useState, type ReactElement } from "react";
import { addDevice, getPush, removeDevice, testNotification, type Device, type PushSetup } from "../api";
import { Icon } from "./Icons";

/**
 * Settings → Notifications (W6a): allow notifications on this device, see your devices, send a test.
 *
 * Web Push in short: the browser gives a private address for this device at its push service (Apple's, Google's or
 * Mozilla's); Sidekick stores it and sends encrypted messages there; the service worker (public/sw.js) shows them,
 * also while the app is closed. On iPhone that only works for an app added to the home screen.
 */

const THIS_DEVICE = "sidekick-push-device";  // this browser's device id, to mark it in the list (a convenience only)

const isIOS = () =>
  /iPhone|iPad|iPod/.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
const installed = () =>
  window.matchMedia("(display-mode: standalone)").matches || (navigator as { standalone?: boolean }).standalone === true;
const supported = () => "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;

function remembered(): number | null {
  try {
    return Number(localStorage.getItem(THIS_DEVICE)) || null;
  } catch {
    return null;
  }
}

function remember(id: number | null) {
  try {
    if (id === null) localStorage.removeItem(THIS_DEVICE);
    else localStorage.setItem(THIS_DEVICE, String(id));
  } catch {
    // private browsing: the list just won't say "this device"
  }
}

/** A name for this device in the list, e.g. "iPhone · home screen app" or "Mac · Chrome". */
function deviceLabel(): string {
  const ua = navigator.userAgent;
  const device = /iPhone/.test(ua) ? "iPhone" : isIOS() ? "iPad" : /Android/.test(ua) ? "Android"
    : /Mac/.test(ua) ? "Mac" : /Windows/.test(ua) ? "Windows" : "Computer";
  const browser = /Edg\//.test(ua) ? "Edge" : /Firefox\//.test(ua) ? "Firefox" : /Chrome\//.test(ua) ? "Chrome"
    : /Safari\//.test(ua) ? "Safari" : "browser";
  return `${device} · ${installed() ? "home screen app" : browser}`;
}

/** The server's public key, as the bytes pushManager.subscribe() wants. */
function keyBytes(base64url: string): Uint8Array<ArrayBuffer> {
  const base64 = (base64url + "=".repeat((4 - (base64url.length % 4)) % 4)).replace(/-/g, "+").replace(/_/g, "/");
  return Uint8Array.from(atob(base64), (c) => c.charCodeAt(0));
}

function sameKey(a: ArrayBuffer | null, b: Uint8Array): boolean {
  if (!a || a.byteLength !== b.length) return false;
  const bytes = new Uint8Array(a);
  return bytes.every((value, i) => value === b[i]);
}

const when = (iso: string | null) =>
  iso ? new Date(iso).toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }) : "";

export default function Notifications() {
  const [setup, setSetup] = useState<PushSetup | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [thisDevice, setThisDevice] = useState<number | null>(remembered);
  const [permission, setPermission] = useState(() => (supported() ? Notification.permission : "default"));

  const load = useCallback(async () => {
    const result = await getPush();
    if (result.state === "ready") setSetup(result.data);
    else if (result.state === "unavailable") setProblem(result.reason);
  }, []);
  useEffect(() => { load(); }, [load]);

  const devices = setup?.devices ?? [];
  const here = devices.find((d) => d.id === thisDevice && d.status === "active");

  async function turnOn() {
    if (!setup?.public_key) return;
    setBusy(true);
    setProblem(null);
    setNote(null);
    try {
      const answer = await Notification.requestPermission();  // must come first: iPhone asks only right after a tap
      setPermission(answer);
      if (answer !== "granted") {
        setProblem("Notifications weren't allowed, so nothing changed.");
        return;
      }
      const registration = await navigator.serviceWorker.ready;
      const key = keyBytes(setup.public_key);
      let subscription = await registration.pushManager.getSubscription();
      if (subscription && !sameKey(subscription.options.applicationServerKey, key)) {
        await subscription.unsubscribe();  // made with an older server key: it would be refused
        subscription = null;
      }
      subscription ??= await registration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: key });
      const result = await addDevice(subscription.toJSON(), deviceLabel());
      if (result.state !== "ready") {
        setProblem(result.state === "unavailable" ? result.reason : "Sign in again, then try once more.");
        return;
      }
      remember(result.data.id);
      setThisDevice(result.data.id);
      setSetup({ ...setup, devices: result.data.devices });
      setNote("This device is on. Send yourself a test below.");
    } catch {
      setProblem("This browser couldn't turn notifications on. Try again, or use another browser.");
    } finally {
      setBusy(false);
    }
  }

  async function test() {
    setBusy(true);
    setProblem(null);
    setNote(null);
    const result = await testNotification();
    setBusy(false);
    if (result.state !== "ready") {
      setProblem(result.state === "unavailable" ? result.reason : "Sign in again, then try once more.");
      return;
    }
    const { sent, failed, gone, devices: updated } = result.data;
    setSetup((current) => current && { ...current, devices: updated });
    const parts = [`Sent to ${sent} device${sent === 1 ? "" : "s"}.`];
    if (gone) parts.push(`${gone} no longer accept${gone === 1 ? "s" : ""} notifications (marked below).`);
    if (failed) parts.push(`${failed} couldn't be reached just now.`);
    setNote(parts.join(" ") + (sent ? " It can take a few seconds to show up." : ""));
  }

  async function remove(device: Device) {
    setBusy(true);
    const result = await removeDevice(device.id);
    setBusy(false);
    if (result.state !== "ready") return;
    if (device.id === thisDevice) {
      remember(null);
      setThisDevice(null);
      try {
        const registration = await navigator.serviceWorker.ready;
        await (await registration.pushManager.getSubscription())?.unsubscribe();
      } catch {
        // already gone on this side
      }
    }
    setSetup((current) => current && { ...current, devices: result.data.devices });
  }

  let state: ReactElement;
  if (setup && !setup.public_key) {
    state = <p className="muted small">Notifications aren't set up on this server yet.</p>;
  } else if (isIOS() && !installed()) {
    state = (
      <p className="muted small">
        On iPhone, notifications only work for Sidekick on your home screen: tap <strong>Share</strong>{" "}
        (the square with the arrow), then <strong>Add to Home Screen</strong>. Open Sidekick from that icon and come
        back here.
      </p>
    );
  } else if (!supported()) {
    state = <p className="muted small">This browser can't show notifications from websites.</p>;
  } else if (permission === "denied") {
    state = (
      <p className="muted small">
        Notifications are blocked for Sidekick on this device. Allow them in the browser's site settings
        {isIOS() ? " (on iPhone: Settings → Notifications → Sidekick)" : ""}, then come back here.
      </p>
    );
  } else {
    state = here ? (
      <p className="muted small"><Icon name="check" size={14} /> On for this device.</p>
    ) : (
      <button className="btn btn-primary btn-sm" onClick={turnOn} disabled={busy || !setup}>
        Turn on for this device
      </button>
    );
  }

  return (
    <div className="notifications">
      <div className="setting-row">
        <span className="setting-icon"><Icon name="bell" size={18} /></span>
        <div className="setting-text">
          <p className="setting-title">On your phone or computer</p>
          <div className="muted small">
            Your reminders and morning brief on this phone or computer, also when the app is closed.
          </div>
          <div className="notifications-state">{state}</div>
        </div>
      </div>

      {devices.length > 0 && (
        <ul className="device-list" aria-label="Your devices">
          {devices.map((d) => (
            <li key={d.id} className={`device${d.status === "gone" ? " gone" : ""}`}>
              <span className="device-text">
                <span className="device-name">{d.label}{d.id === thisDevice ? " (this device)" : ""}</span>
                <span className="muted small">
                  {d.status === "gone" ? "No longer accepts notifications: turn it on again on that device"
                    : d.last_ok ? `Last notified ${when(d.last_ok)}` : `Added ${when(d.added)}`}
                </span>
              </span>
              <button className="btn btn-ghost btn-sm" onClick={() => remove(d)} disabled={busy}
                aria-label={`Remove ${d.label}`}>Remove</button>
            </li>
          ))}
        </ul>
      )}

      {devices.some((d) => d.status === "active") && (
        <button className="btn btn-secondary btn-sm" onClick={test} disabled={busy}>Send a test notification</button>
      )}
      {note && <p className="muted small" role="status">{note}</p>}
      {problem && <p className="composer-problem" role="alert">{problem}</p>}
    </div>
  );
}
