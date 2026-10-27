import { useTheme, type ThemeChoice } from "../theme";
import { Icon, type IconName } from "./Icons";

const OPTIONS: { value: ThemeChoice; label: string; icon: IconName }[] = [
  { value: "system", label: "System", icon: "monitor" },
  { value: "light", label: "Light", icon: "sun" },
  { value: "dark", label: "Dark", icon: "moon" },
];

/** Segmented control: System / Light / Dark. A real preference, remembered in this browser. */
export default function ThemeSwitch({ showLabels = false }: { showLabels?: boolean }) {
  const { theme, setTheme } = useTheme();
  return (
    <div className="segmented" role="radiogroup" aria-label="Theme">
      {OPTIONS.map((o) => (
        <button key={o.value} type="button" role="radio" aria-checked={theme === o.value}
          className={theme === o.value ? "active" : ""} onClick={() => setTheme(o.value)}
          title={o.label} aria-label={o.label}>
          <Icon name={o.icon} size={16} />
          {showLabels && <span>{o.label}</span>}
        </button>
      ))}
    </div>
  );
}
