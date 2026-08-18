import React, { useEffect, useId, useRef, useState } from "react"
import Markdown, { defaultUrlTransform, type Components } from "react-markdown"
import rehypeKatex from "rehype-katex"
import remarkGfm from "remark-gfm"
import remarkMath from "remark-math"
import { Check, Copy } from "lucide-react"
import "katex/dist/katex.min.css"
import { useTheme } from "@/lib/theme"

type MarkdownNode = {
  type: string
  value?: string
  url?: string
  children?: MarkdownNode[]
}

function referenceNodes(value: string): MarkdownNode[] {
  const nodes: MarkdownNode[] = []
  const pattern = /(\[\[[^\]]+\]\]|\[\d+\])/g
  let cursor = 0
  for (const match of value.matchAll(pattern)) {
    const index = match.index ?? 0
    if (index > cursor) nodes.push({ type: "text", value: value.slice(cursor, index) })
    const token = match[0]
    if (token.startsWith("[[")) {
      const inner = token.slice(2, -2)
      const separator = inner.indexOf("|")
      const target = (separator === -1 ? inner : inner.slice(0, separator)).trim()
      const label = (separator === -1 ? "" : inner.slice(separator + 1)).trim() || target
      nodes.push({
        type: "link",
        url: `#openkb-wiki-${encodeURIComponent(target)}`,
        children: [{ type: "text", value: label }],
      })
    } else {
      const ordinal = Number(token.slice(1, -1))
      nodes.push({
        type: "link",
        url: `#openkb-evidence-${ordinal}`,
        children: [{ type: "text", value: token }],
      })
    }
    cursor = index + token.length
  }
  if (cursor < value.length) nodes.push({ type: "text", value: value.slice(cursor) })
  return nodes.length ? nodes : [{ type: "text", value }]
}

/** Remark transform for OpenKB wiki links and evidence ordinals outside code/links. */
function remarkOpenKBReferences() {
  return (tree: MarkdownNode) => {
    const visit = (node: MarkdownNode) => {
      if (!node.children || ["code", "inlineCode", "link", "linkReference"].includes(node.type)) return
      const children: MarkdownNode[] = []
      for (const child of node.children) {
        if (child.type === "text" && child.value) children.push(...referenceNodes(child.value))
        else {
          visit(child)
          children.push(child)
        }
      }
      node.children = children
    }
    visit(tree)
  }
}

function CodeBox({ code, lang }: { code: string; lang?: string }) {
  const [copied, setCopied] = useState(false)
  const copy = async () => {
    await navigator.clipboard.writeText(code)
    setCopied(true)
    window.setTimeout(() => setCopied(false), 1200)
  }
  return (
    <div className="my-3 overflow-hidden rounded-apple-md border border-border/70 bg-muted/50">
      <div className="flex min-h-8 items-center justify-between px-3.5 pt-1.5 text-[10.5px] uppercase tracking-wide text-muted-foreground">
        <span>{lang}</span>
        <button type="button" onClick={() => void copy()} className="flex items-center gap-1 rounded px-1.5 py-1 normal-case hover:bg-accent">
          {copied ? <Check className="size-3" /> : <Copy className="size-3" />}
          {copied ? "Copied" : "Copy"}
        </button>
      </div>
      <pre className="overflow-x-auto px-3.5 py-3 text-[12.5px] leading-relaxed"><code className="font-mono2 whitespace-pre text-foreground">{code}</code></pre>
    </div>
  )
}

function MermaidBlock({ code }: { code: string }) {
  const { resolved } = useTheme()
  const id = useId().replace(/:/g, "")
  const ref = useRef<HTMLDivElement>(null)
  const renderKey = `${resolved}:${code}`
  const [failedKey, setFailedKey] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    void import("mermaid").then(async ({ default: mermaid }) => {
      mermaid.initialize({ startOnLoad: false, securityLevel: "strict", theme: resolved === "dark" ? "dark" : "default" })
      const { svg } = await mermaid.render(`openkb-${id}`, code)
      if (!cancelled && ref.current) ref.current.innerHTML = svg
    }).catch(() => {
      if (!cancelled) setFailedKey(renderKey)
    })
    return () => { cancelled = true }
  }, [code, id, renderKey, resolved])

  if (failedKey === renderKey) return <CodeBox code={code} lang="mermaid" />
  return <div ref={ref} className="my-3 flex justify-center overflow-x-auto [&_svg]:h-auto [&_svg]:max-w-full" />
}

function linkHost(url: string): string {
  try { return /^https?:/i.test(url) ? new URL(url).host : "" } catch { return "" }
}

function openExternal(event: React.MouseEvent<HTMLAnchorElement>, url: string) {
  if (!/^https?:/i.test(url)) return
  event.preventDefault()
  void import("@tauri-apps/api/core")
    .then(({ invoke }) => invoke("desktop_open_external_url", { url }))
    .catch(() => window.open(url, "_blank", "noopener,noreferrer"))
}

export default function MarkdownView({
  source,
  onWikiLink,
  onEvidenceRef,
  evidenceCount = 0,
  finalized = true,
}: {
  source: string
  onWikiLink?: (target: string) => void
  onEvidenceRef?: (ordinal: number) => void
  evidenceCount?: number
  finalized?: boolean
}) {
  const components: Components = {
    h1: ({ children }) => <h1 className="mb-3 text-[22px] font-extrabold tracking-tight text-foreground">{children}</h1>,
    h2: ({ children }) => <h2 className="mb-2 mt-5 text-[16px] font-bold text-foreground">{children}</h2>,
    h3: ({ children }) => <h3 className="mb-1.5 mt-4 text-[14px] font-semibold text-foreground">{children}</h3>,
    h4: ({ children }) => <h4 className="mb-1 mt-3 text-[14px] font-semibold text-foreground">{children}</h4>,
    h5: ({ children }) => <h5 className="mb-1 mt-3 text-[13px] font-semibold text-foreground">{children}</h5>,
    h6: ({ children }) => <h6 className="mb-1 mt-3 text-[12px] font-semibold uppercase tracking-wide text-muted-foreground">{children}</h6>,
    p: ({ children }) => <p className="my-1.5 text-[14px] leading-relaxed text-muted-foreground">{children}</p>,
    ul: ({ children }) => <ul className="my-2.5 list-disc space-y-1.5 pl-6 text-[14px] text-muted-foreground">{children}</ul>,
    ol: ({ children, start }) => <ol start={start} className="my-2.5 list-decimal space-y-1.5 pl-6 text-[14px] text-muted-foreground">{children}</ol>,
    li: ({ children }) => <li className="pl-1 leading-relaxed marker:text-muted-foreground">{children}</li>,
    blockquote: ({ children }) => <blockquote className="my-2.5 rounded-r-lg border-l-2 border-amber-400/70 bg-amber-400/10 px-3 py-2 text-[13px] text-muted-foreground">{children}</blockquote>,
    table: ({ children }) => <div className="my-3 overflow-x-auto"><table className="w-full border-collapse text-[13px]">{children}</table></div>,
    th: ({ children }) => <th className="border border-border/70 bg-muted/50 px-3 py-1.5 text-left font-semibold text-foreground">{children}</th>,
    td: ({ children }) => <td className="border border-border/70 px-3 py-1.5 text-muted-foreground">{children}</td>,
    hr: () => <hr className="my-4 border-0 border-t border-border/70" />,
    img: ({ alt, src }) => <span className="text-muted-foreground">![{alt ?? "image"}]({String(src ?? "")})</span>,
    code: ({ children, className }) => <code className={className ? `${className} font-mono2` : "rounded bg-muted px-1 py-px font-mono2 text-[12px]"}>{children}</code>,
    pre: ({ children }) => {
      const child = React.Children.only(children)
      if (!React.isValidElement(child)) return <pre>{children}</pre>
      const props = child.props as { className?: string; children?: React.ReactNode }
      const language = /language-([^\s]+)/.exec(props.className ?? "")?.[1]
      const code = String(props.children ?? "").replace(/\n$/, "")
      return language === "mermaid" && finalized ? <MermaidBlock code={code} /> : <CodeBox code={code} lang={language} />
    },
    a: ({ href = "", children }) => {
      const evidence = /^#openkb-evidence-(\d+)$/.exec(href)
      if (evidence) {
        const ordinal = Number(evidence[1])
        return onEvidenceRef && ordinal >= 1 && ordinal <= evidenceCount
          ? <button type="button" onClick={() => onEvidenceRef(ordinal)} className="rounded px-0.5 text-primary underline decoration-primary/40 underline-offset-2">{children}</button>
          : <span>{children}</span>
      }
      const wiki = /^#openkb-wiki-(.+)$/.exec(href)
      if (wiki) {
        const target = decodeURIComponent(wiki[1])
        return onWikiLink
          ? <button type="button" onClick={() => onWikiLink(target)} className="rounded bg-primary/10 px-1 py-px text-primary">{children}</button>
          : <span className="rounded bg-primary/10 px-1 py-px text-primary">{children}</span>
      }
      if (!/^https?:/i.test(href)) return <span>{children}</span>
      return <a href={href} title={href} onClick={(event) => openExternal(event, href)} className="text-primary hover:underline">{children}<span className="ml-1 text-[0.85em] text-muted-foreground">({linkHost(href)})</span></a>
    },
  }

  return (
    <Markdown
      remarkPlugins={[remarkGfm, remarkMath, remarkOpenKBReferences]}
      rehypePlugins={[rehypeKatex]}
      components={components}
      skipHtml
      urlTransform={(url) => url.startsWith("#openkb-") ? url : defaultUrlTransform(url)}
    >
      {source}
    </Markdown>
  )
}
