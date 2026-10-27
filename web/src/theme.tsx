import { createContext, useContext, useEffect, useState, type ReactNode } from "react";

/** Light, dark, or follow the device. A per-browser convenience, so localStorage is fine (and optional). */
export type ThemeChoice = "system" | "light" | "dark";

const ThemeContext = createContext<{ theme: ThemeChoice; setTheme: (t: ThemeChoice) => void }>({
  theme: "system",
  setTheme: () => {},
});

function saved(): ThemeChoice {
  const asked = new URLSearchParams(window.location.search).get("theme");  // e.g. a link to show dark mode
  if (asked === "light" || asked === "dark" || asked === "system") return asked;
  try {
    const value = localStorage.getItem("sidekick-theme");
    return value === "light" || value === "dark" ? value : "system";
  } catch {
    return "system";
  }
}

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [theme, setTheme] = useState<ThemeChoice>(saved);
  useEffect(() => {
    const root = document.documentElement;
    if (theme === "system") delete root.dataset.theme;
    else root.dataset.theme = theme;
    try {
      localStorage.setItem("sidekick-theme", theme);
    } catch {
      /* private mode: the choice just isn't remembered */
    }
  }, [theme]);
  return <ThemeContext.Provider value={{ theme, setTheme }}>{children}</ThemeContext.Provider>;
}

export const useTheme = () => useContext(ThemeContext);
