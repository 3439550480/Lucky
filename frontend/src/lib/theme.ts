/**
 * 主题状态 hook（Lucky 设计系统）
 *
 * 机制：theme 存 localStorage("lucky_theme")，缺省跟随系统 prefers-color-scheme；
 * 实际生效 = <html> 上的 class（index.html 防 FOUC 脚本在首帧前设置，本 hook 接管后续切换）
 */
import { useCallback, useEffect, useState } from "react";

export type Theme = "light" | "dark";

const THEME_KEY = "lucky_theme";

/** 读取当前生效主题（与防 FOUC 脚本同规则） */
export function readTheme(): Theme {
  const saved = localStorage.getItem(THEME_KEY);
  if (saved === "light" || saved === "dark") return saved;
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

function applyTheme(theme: Theme) {
  document.documentElement.classList.toggle("dark", theme === "dark");
}

export function useTheme(): { theme: Theme; toggle: () => void } {
  const [theme, setTheme] = useState<Theme>(() => readTheme());

  // 跟随系统变化（仅在用户未手动选择时）
  useEffect(() => {
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    const onChange = (e: MediaQueryListEvent) => {
      if (!localStorage.getItem(THEME_KEY)) {
        setTheme(e.matches ? "dark" : "light");
      }
    };
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, []);

  const toggle = useCallback(() => {
    setTheme((prev) => {
      const next: Theme = prev === "dark" ? "light" : "dark";
      localStorage.setItem(THEME_KEY, next);   // 手动切换后固定（不再跟随系统）
      applyTheme(next);
      return next;
    });
  }, []);

  return { theme, toggle };
}
