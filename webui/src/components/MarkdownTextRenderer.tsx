import { Children, isValidElement, useMemo } from "react";
import type { Components } from "react-markdown";
import ReactMarkdown from "react-markdown";
import rehypeKatex from "rehype-katex";
import remarkBreaks from "remark-breaks";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import { useTranslation } from "react-i18next";

import { CodeBlock } from "@/components/CodeBlock";
import { FileReferenceChip, isLikelyFilePath } from "@/components/FileReferenceChip";
import { inferMediaKind } from "@/lib/media";
import { cn } from "@/lib/utils";

import "katex/dist/katex.min.css";

interface MarkdownTextRendererProps {
  children: string;
  className?: string;
  highlightCode?: boolean;
}

const remarkPlugins = [remarkBreaks, remarkGfm, remarkMath];
const rehypePlugins = [rehypeKatex];

/** Markdown 链接 scheme 白名单:只允许普通网页链接。
 *
 * react-markdown 不做 URL 净化,模型输出中的 ``[x](javascript:...)`` 会被
 * 原样放进 ``href``,点击即执行。白名单外的 href 一律降级为纯文本。 */
function safeLinkHref(href: string | undefined): string | null {
  if (!href) return null;
  try {
    const protocol = new URL(href).protocol;
    if (protocol === "http:" || protocol === "https:") return href;
  } catch {
    // 解析失败(非绝对 URL)不是可点击的网页链接。
  }
  return null;
}

/**
 * Heavy markdown stack (GFM, math, KaTeX, syntax highlighting) kept in a
 * separate chunk so the app shell can paint sooner on refresh.
 */
export default function MarkdownTextRenderer({
  children,
  className,
  highlightCode = true,
}: MarkdownTextRendererProps) {
  const { t } = useTranslation();
  const components = useMemo<Components>(
    () => ({
      code({ className: cls, children: kids, ...props }) {
        const match = /language-(\w+)/.exec(cls || "");
        if (match) {
          const code = String(kids).replace(/\n$/, "");
          return (
            <CodeBlock
              language={match[1]}
              code={code}
              className="my-3"
              highlight={highlightCode}
            />
          );
        }
        const raw = String(kids).replace(/\n$/, "");
        if (isLikelyFilePath(raw)) {
          return <FileReferenceChip path={raw} />;
        }
        /** Plain fenced ``` blocks (no language) & wide one-liners: block monospace, not inline pill. */
        const widePlainBlock = raw.includes("\n") || raw.length > 120;
        if (widePlainBlock) {
          return (
            <code
              className={cn(
                "block min-w-0 whitespace-pre bg-transparent p-0 font-mono text-[0.8125rem]",
                "leading-snug text-inherit",
                cls,
              )}
              {...props}
            >
              {kids}
            </code>
          );
        }
        return (
          <code
            className={cn(
              "rounded bg-muted px-1 py-0.5 font-mono text-[0.85em]",
              cls,
            )}
            {...props}
          >
            {kids}
          </code>
        );
      },
      pre({ children: markdownChildren }) {
        const kids = Children.toArray(markdownChildren);
        const lone = kids.length === 1 ? kids[0] : null;
        /** Highlighted fences render ``CodeBlock`` (block shell); skip invalid ``<pre><div>``. */
        if (lone != null && isValidElement(lone) && lone.type === CodeBlock) {
          return <>{markdownChildren}</>;
        }
        return (
          <pre
            className={cn(
              "my-3 overflow-x-auto rounded-lg border border-border/60 bg-muted/35",
              "p-3 font-mono text-[0.8125rem] leading-snug text-foreground/90",
              "whitespace-pre [overflow-wrap:normal]",
            )}
          >
            {markdownChildren}
          </pre>
        );
      },
      a({ href, children: markdownChildren, ...props }) {
        const safeHref = safeLinkHref(href);
        if (!safeHref) {
          return <span className="text-foreground/90 underline decoration-dotted">{markdownChildren}</span>;
        }
        return (
          <a
            href={safeHref}
            target="_blank"
            rel="noreferrer noopener"
            className="text-primary underline underline-offset-2 hover:opacity-80"
            {...props}
          >
            {markdownChildren}
          </a>
        );
      },
      img({ src, alt, node: _node, className: imgClassName, ...props }) {
        void _node;
        const source = typeof src === "string" ? src : "";
        if (!source) return null;
        const label = typeof alt === "string" ? alt : "";
        if (inferMediaKind({ url: source, name: label }) === "video") {
          return (
            <span
              className={cn(
                "not-prose my-3 block w-fit max-w-full overflow-hidden rounded-[14px]",
                "border border-border/70 bg-background shadow-sm",
              )}
            >
              <video
                src={source}
                controls
                preload="metadata"
                className="block max-h-[26rem] max-w-full bg-black"
                aria-label={label ? `${t("message.videoAttachment", { defaultValue: "Video attachment" })}: ${label}` : t("message.videoAttachment", { defaultValue: "Video attachment" })}
              />
              {label ? (
                <span className="block max-w-full truncate px-3 py-2 text-xs text-muted-foreground">
                  {label}
                </span>
              ) : null}
            </span>
          );
        }
        return (
          <span
            className={cn(
              "not-prose my-3 block w-fit max-w-full overflow-hidden rounded-[14px]",
              "border border-border/70 bg-background shadow-sm",
            )}
          >
            <a
              href={source}
              target="_blank"
              rel="noreferrer noopener"
              className="block bg-muted/20"
              aria-label={label ? `${t("message.openImage", { defaultValue: "Open image" })}: ${label}` : t("message.openImage", { defaultValue: "Open image" })}
            >
              <img
                src={source}
                alt={label}
                loading="lazy"
                decoding="async"
                draggable={false}
                className={cn(
                  "block h-auto max-h-[34rem] max-w-full bg-background object-contain",
                  imgClassName,
                )}
                {...props}
              />
            </a>
            {label ? (
              <span className="block max-w-full truncate px-3 py-2 text-xs text-muted-foreground">
                {label}
              </span>
            ) : null}
          </span>
        );
      },
    }),
    [highlightCode, t],
  );

  return (
    <div
      className={cn(
        "markdown-content prose max-w-none dark:prose-invert",
        "prose-headings:mt-4 prose-headings:mb-2 prose-headings:font-semibold prose-headings:tracking-tight",
        "prose-h1:text-lg prose-h2:text-base prose-h3:text-sm prose-h4:text-[13px]",
        "prose-p:my-2",
        "prose-ul:my-2 prose-ol:my-2 prose-li:my-0.5",
        "prose-blockquote:my-3 prose-blockquote:border-l-2 prose-blockquote:font-normal",
        "prose-blockquote:not-italic prose-blockquote:text-foreground/80",
        "prose-a:text-primary prose-a:underline-offset-2 hover:prose-a:opacity-80",
        "prose-hr:my-6",
        "prose-pre:my-0 prose-pre:bg-transparent prose-pre:p-0",
        "prose-code:before:content-none prose-code:after:content-none prose-code:font-normal",
        "prose-table:my-3 prose-th:text-left prose-th:font-medium",
        className,
      )}
      style={{ lineHeight: "var(--cjk-line-height)" }}
    >
      <ReactMarkdown
        remarkPlugins={remarkPlugins}
        rehypePlugins={rehypePlugins}
        components={components}
      >
        {children}
      </ReactMarkdown>
    </div>
  );
}
