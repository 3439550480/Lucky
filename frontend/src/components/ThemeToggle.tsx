/**
 * 主题切换按钮（Lucky 设计系统）
 * 明确受控：theme 来自 useTheme（App 顶层传入），避免组件内双状态源
 */
import { Moon, Sun } from "lucide-react";
import type { Theme } from "../lib/theme";

interface ThemeToggleProps {
  theme: Theme;
  onToggle: () => void;
}

export default function ThemeToggle({ theme, onToggle }: ThemeToggleProps) {
  return (
    <button
      type="button"
      onClick={onToggle}
      title={theme === "dark" ? "切换到亮色" : "切换到暗色"}
      className="flex h-9 w-9 items-center justify-center rounded-full text-muted transition-colors hover:bg-surface-2 hover:text-content"
    >
      {theme === "dark" ? <Sun size={17} /> : <Moon size={17} />}
    </button>
  );
}
