// src/components/ManualPage.tsx
import React from 'react';

interface ManualPageProps {
  onClose: () => void;
}

const ManualPage: React.FC<ManualPageProps> = ({ onClose }) => {
  return (
    // 使用 h-full 和 overflow-y-auto 确保滚动
    <div className="h-full overflow-y-auto bg-parchment text-ink">
      {/* 固定头部（不参与滚动，始终置顶） */}
      <header className="sticky top-0 z-10 flex h-16 shrink-0 items-center justify-between border-b border-ink/10 bg-parchment/88 px-4 backdrop-blur lg:px-6">
        <div className="flex items-center gap-3">
          <div className="grid h-9 w-9 shrink-0 place-items-center bg-moss text-white">
            📘
          </div>
          <div className="min-w-0">
            <div className="truncate text-sm font-semibold text-ink">Lucky 操作手册</div>
            <div className="truncate text-xs text-ink/45">帮助文档</div>
          </div>
        </div>
        <button
          onClick={onClose}
          className="rounded-full p-2 text-ink/55 transition hover:bg-ink/5 hover:text-ink"
          title="返回"
        >
          ✕
        </button>
      </header>

      {/* 内容容器，自然撑开高度 */}
      <div className="mx-auto max-w-4xl px-4 py-8 lg:px-6">
        <div className="space-y-8">
          {/* 1. 功能介绍 */}
          <section>
            <h2 className="text-2xl font-bold text-indigo-600">1. 功能介绍</h2>
            <p className="mt-2">通过自然语言自动查询电商数据仓库，无需编写 SQL。系统会逐步展示理解、召回、生成 SQL、执行查询的全过程，并以表格形式返回结果。</p>
          </section>

          {/* 2. 数据范围 */}
          <section className="rounded-md border-l-4 border-yellow-400 bg-yellow-50 p-4">
            <h2 className="text-xl font-bold text-yellow-700">⚠️ 2. 数据范围（重要）</h2>
            <ul className="mt-2 list-inside list-disc space-y-1">
              <li><strong>当前仅包含 2025 年全年数据</strong>（2025-01-01 至 2025-12-31）。</li>
              <li>包含区域（华东/华南/华北等）、60 位客户、105 款商品、全年约 300 条订单模拟数据。</li>
              <li>超出 2025 年范围的问题会返回空结果或提示。</li>
            </ul>
          </section>

          {/* 3. 如何使用 */}
          <section>
            <h2 className="text-2xl font-bold text-indigo-600">3. 如何使用</h2>
            <ol className="mt-2 list-inside list-decimal space-y-1">
              <li>在输入框中用中文描述你的问题。</li>
              <li>按回车或点击发送按钮。</li>
              <li>等待系统逐步执行（右侧会显示进度步骤）。</li>
              <li>最终结果以表格形式展示，支持横向滚动。</li>
            </ol>
          </section>

          {/* 4. 支持的问题类型与示例 */}
          <section>
            <h2 className="text-2xl font-bold text-indigo-600">4. 支持的问题类型与示例</h2>
            <div className="mt-2 overflow-x-auto">
              <table className="min-w-full border text-sm">
                <thead className="bg-gray-100">
                  <tr>
                    <th className="border p-2">类型</th>
                    <th className="border p-2">示例</th>
                  </tr>
                </thead>
                <tbody>
                  <tr><td className="border p-2">销售额 / GMV</td><td className="border p-2">“2025年第一季度各大区的GMV，按从高到低排序”</td></tr>
                  <tr><td className="border p-2">销量</td><td className="border p-2">“2025年3月各商品品类的销量和销售额”</td></tr>
                  <tr><td className="border p-2">客户分析</td><td className="border p-2">“黄金会员贡献的订单总额”</td></tr>
                  <tr><td className="border p-2">商品分析</td><td className="border p-2">“哪个商品品类销量最高”</td></tr>
                  <tr><td className="border p-2">时间趋势</td><td className="border p-2">“2025年每月订单数量趋势”</td></tr>
                  <tr><td className="border p-2">区域对比</td><td className="border p-2">“华北和华东地区哪个月份的销售额最高”</td></tr>
                  <tr><td className="border p-2">品牌表现</td><td className="border p-2">“华为和苹果手机在2025年的销售额对比”</td></tr>
                </tbody>
              </table>
            </div>
          </section>

          {/* 5. 不支持的功能 */}
          <section>
            <h2 className="text-2xl font-bold text-indigo-600">5. 不支持的功能</h2>
            <ul className="mt-2 list-inside list-disc space-y-1">
              <li>❌ 修改数据（只读查询）</li>
              <li>❌ 多轮对话（每次独立提问）</li>
              <li>❌ 复杂逻辑（建议拆成多个问题）</li>
              <li>❌ 超出2025年的数据查询</li>
              <li>❌ 导出结果（后续版本考虑）</li>
            </ul>
          </section>

          {/* 6. 常见问题 */}
          <section>
            <h2 className="text-2xl font-bold text-indigo-600">6. 常见问题（FAQ）</h2>
            <dl className="mt-2 space-y-2">
              <dt className="font-semibold">Q：为什么查询结果为空？</dt>
              <dd>A：可能原因：实体名称不匹配；时间超出2025年；数据库无对应数据。</dd>
              <dt className="font-semibold">Q：系统一直显示“执行中”怎么办？</dt>
              <dd>A：检查后端服务是否运行，可刷新页面重试。</dd>
              <dt className="font-semibold">Q：SQL错误提示是什么意思？</dt>
              <dd>A：系统会自动修正一次，如仍失败可简化问题再试。</dd>
              <dt className="font-semibold">Q：可以查询不同年份对比吗？</dt>
              <dd>A：当前只有2025年数据，可以查询年内对比。</dd>
            </dl>
          </section>

          {/* 7. 快速上手示例 */}
          <section>
            <h2 className="text-2xl font-bold text-indigo-600">7. 快速上手示例</h2>
            <div className="mt-2 space-y-1 rounded-md bg-gray-100 p-3 font-mono text-sm">
              <div>统计 2025 年第一季度各大区的 GMV，并按 GMV 从高到低排序</div>
              <div>2025 年每月订单数量趋势</div>
              <div>2025 年哪个商品品类的销售额最高</div>
              <div>华东地区 2025 年销量最好的 5 个商品</div>
              <div>2025 年黄金会员的订单总额</div>
            </div>
          </section>

          {/* 8. 注意事项 */}
          <section>
            <h2 className="text-2xl font-bold text-indigo-600">8. 注意事项</h2>
            <ul className="mt-2 list-inside list-disc space-y-1">
              <li>问题尽量包含 <strong>时间范围、维度、指标</strong>。</li>
              <li>避免复杂比较逻辑，建议拆分为多个问题。</li>
              <li>如果结果不符预期，尝试更具体的表述。</li>
            </ul>
          </section>

          <div className="flex justify-center pt-8">
            <button
              onClick={onClose}
              className="rounded-md bg-indigo-600 px-6 py-2 text-white transition hover:bg-indigo-700"
            >
              我已了解，返回问数页面
            </button>
          </div>
        </div>
      </div>
    </div>
  );
};

export default ManualPage;