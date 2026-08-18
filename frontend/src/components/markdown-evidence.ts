import remarkGfm from "remark-gfm"
import remarkMath from "remark-math"
import remarkParse from "remark-parse"
import { unified } from "unified"

type MarkdownNode = {
  type: string
  value?: string
  children?: MarkdownNode[]
}

/** Return evidence markers from the same AST locations that become clickable references. */
export function openKBEvidenceOrdinals(source: string): number[] {
  const tree = unified().use(remarkParse).use(remarkGfm).use(remarkMath).parse(source) as MarkdownNode
  const ordinals: number[] = []
  const visit = (node: MarkdownNode) => {
    if (["code", "inlineCode", "link", "linkReference"].includes(node.type)) return
    if (node.type === "text" && node.value) {
      for (const match of node.value.matchAll(/\[(\d+)\]/g)) ordinals.push(Number(match[1]))
    }
    for (const child of node.children ?? []) visit(child)
  }
  visit(tree)
  return ordinals
}
