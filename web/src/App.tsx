import { Route, Routes } from "react-router-dom";
import AppLayout from "./layouts/AppLayout";
import PublicLayout from "./layouts/PublicLayout";
import Apps from "./pages/Apps";
import Chat from "./pages/Chat";
import Dashboard from "./pages/Dashboard";
import Landing from "./pages/Landing";
import NotFound from "./pages/NotFound";
import NotificationsPage from "./pages/NotificationsPage";
import Settings from "./pages/Settings";
import SignIn from "./pages/SignIn";

/** Only /app/... is served on the real domain (Caddy, from the build). The landing page at / is local-only until
 *  it's updated: the domain's home page stays the simple one in site/. */
export default function App() {
  return (
    <Routes>
      <Route element={<PublicLayout />}>
        <Route index element={<Landing />} />
      </Route>
      <Route path="app/signin" element={<SignIn />} />
      <Route path="app" element={<AppLayout />}>
        <Route index element={<Dashboard />} />
        <Route path="chat" element={<Chat />} />
        <Route path="chat/:id" element={<Chat />} />
        <Route path="apps" element={<Apps />} />
        <Route path="notifications" element={<NotificationsPage />} />
        <Route path="settings" element={<Settings />} />
      </Route>
      <Route path="*" element={<NotFound />} />
    </Routes>
  );
}
