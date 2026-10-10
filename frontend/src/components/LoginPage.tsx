/**
 * 登录页（2.0 上下文策略 0.2）
 * 工号 + PIN → AuthSession token；错误行内提示；演示账号提示
 */
import { BarChart3 } from "lucide-react";
import { useState } from "react";
import { login } from "../lib/agentApi";
import type { StaffInfo } from "../lib/agentApi";

export default function LoginPage({ onLogin }: { onLogin: (staff: StaffInfo) => void }) {
  const [staffId, setStaffId] = useState("");
  const [pin, setPin] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!staffId.trim() || !pin.trim() || loading) return;
    setLoading(true);
    setError("");
    try {
      const staff = await login(staffId.trim(), pin.trim());
      onLogin(staff);
    } catch (err) {
      setError(err instanceof Error ? err.message : "登录失败，请稍后重试");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="grid h-dvh place-items-center bg-bg px-4 text-content">
      <div className="pointer-events-none fixed inset-0 grain" />
      <div className="relative w-full max-w-sm rounded-xl2 border border-line bg-surface px-8 py-10 shadow-soft">
        <div className="mb-8 flex flex-col items-center gap-3">
          <div className="grid h-12 w-12 place-items-center rounded-xl2 bg-gradient-to-br from-accent to-accent/70 text-white shadow-soft">
            <BarChart3 className="h-6 w-6" aria-hidden="true" />
          </div>
          <div className="text-center">
            <h1 className="text-xl font-semibold tracking-wide">Lucky</h1>
            <p className="mt-1 text-xs text-muted">安踏特卖店 · 员工数据助手</p>
          </div>
        </div>

        <form onSubmit={handleSubmit} className="space-y-4">
          <div>
            <label htmlFor="staff-id" className="mb-1.5 block text-xs font-medium text-muted">
              工号
            </label>
            <input
              id="staff-id"
              type="text"
              value={staffId}
              onChange={(e) => setStaffId(e.target.value)}
              placeholder="如 M001 / S001"
              autoComplete="username"
              autoFocus
              className="h-11 w-full rounded-lg border border-line bg-bg px-3.5 text-sm outline-none transition placeholder:text-muted/50 focus:border-accent focus:ring-2 focus:ring-accent/30"
            />
          </div>
          <div>
            <label htmlFor="pin" className="mb-1.5 block text-xs font-medium text-muted">
              PIN
            </label>
            <input
              id="pin"
              type="password"
              value={pin}
              onChange={(e) => setPin(e.target.value)}
              placeholder=" PIN"
              autoComplete="current-password"
              className="h-11 w-full rounded-lg border border-line bg-bg px-3.5 text-sm outline-none transition placeholder:text-muted/50 focus:border-accent focus:ring-2 focus:ring-accent/30"
            />
          </div>

          {error && (
            <p className="rounded-lg border border-danger/30 bg-danger/10 px-3 py-2 text-xs text-danger" role="alert">
              {error}
            </p>
          )}

          <button
            type="submit"
            disabled={!staffId.trim() || !pin.trim() || loading}
            className="h-11 w-full rounded-lg bg-accent text-sm font-semibold text-white transition hover:bg-accent/85 focus:outline-none focus:ring-2 focus:ring-accent/40 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {loading ? "登录中…" : "登录"}
          </button>
        </form>

        <p className="mt-6 text-center text-[11px] leading-5 text-muted">
          工号 = 角色前缀（M 店长 / K 库管 / S 店员 / T 临时工）+ 3 位序号
          <br />
          演示环境统一 PIN：123456
        </p>
      </div>
    </div>
  );
}
