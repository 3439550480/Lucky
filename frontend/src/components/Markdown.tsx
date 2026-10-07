/**
 * Markdown 渲染器（Lucky 消息渲染升级）
 * GFM 表格/任务列表 + 代码高亮（rehype-highlight + 双主题 hljs 配色见 styles.css）
 * 说明：代码块逐块复制按钮留待后续（消息级复制已存在）
 */
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeHighlight from "rehype-highlight";

export function Markdown({ text }: { text: string }) {
  return (
    <div className="md-content">
      <ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={[rehypeHighlight]}>
        {text}
      </ReactMarkdown>
    </div>
  );
}
