/**
 * Tailwind CSS 主题配置（Lucky 设计系统）
 *
 * 双主题架构：
 * - darkMode: "class" —— <html class="dark"> 切换（ThemeToggle / 防FOUC脚本控制）
 * - 颜色全部指向 CSS 变量（RGB 三元组，支持 /alpha 透明度语法）
 * - 语义 token（bg/surface/text/accent...）供新代码使用；
 *   旧名（parchment/ink/mist...）映射到同一批变量，存量组件零破坏渐进迁移
 */
import type { Config } from "tailwindcss";

/** 语义色 → CSS 变量引用（变量值为 "R G B" 三元组） */
const v = (name: string) => `rgb(var(--c-${name}) / <alpha-value>)`;

export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  darkMode: "class",
  theme: {
    extend: {
      fontFamily: {
        sans: [
          '"LXGW WenKai Screen"',
          '"Noto Sans SC"',
          '"PingFang SC"',
          '"Microsoft YaHei"',
          "sans-serif",
        ],
        mono: ['"JetBrains Mono"', '"SFMono-Regular"', "Consolas", "monospace"],
      },
      colors: {
        // ---- 语义 token（新代码统一用这组）----
        bg: v("bg"),               // 页面背景
        surface: v("surface"),     // 卡片/面板
        "surface-2": v("surface2"),// 次级填充（hover/代码块底）
        line: v("line"),           // 细边框（语义为 line，避免与 shadow.line 混淆）
        content: v("text"),        // 主文本
        muted: v("muted"),         // 次级文本
        accent: v("accent"),       // 品牌主色（Lucky 绿）
        "accent-soft": v("accentSoft"), // 主色浅底
        accent2: v("accent2"),     // 辅助强调（琥珀）
        danger: v("danger"),
        // ---- 旧名映射（存量组件兼容，逐步迁移到语义名）----
        parchment: v("bg"),
        ink: v("text"),
        soot: v("surface2"),
        moss: v("accent"),
        brass: v("accent2"),
        tomato: v("danger"),
        mist: v("line"),
      },
      boxShadow: {
        line: "0 1px 0 rgba(0, 0, 0, 0.06)",
        panel: "0 24px 70px rgba(0, 0, 0, 0.18)",
        soft: "0 2px 12px rgba(0, 0, 0, 0.06)",
      },
      borderRadius: {
        xl2: "1.25rem",
      },
    },
  },
  plugins: [],
} satisfies Config;
