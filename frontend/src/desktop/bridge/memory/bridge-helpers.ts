import type { DesktopBridgeEvent, DesktopConversation, DesktopImportTask } from "@/desktop/bridge/contracts"

export function requireConversation(
  conversations: DesktopConversation[],
  conversationId: string,
): DesktopConversation {
  const conversation = conversations.find((item) => item.conversationId === conversationId)
  if (!conversation) throw new Error("The conversation was not found.")
  return conversation
}

export function emitBridgeEvent(
  listeners: Set<(event: DesktopBridgeEvent) => void>,
  event: DesktopBridgeEvent,
): void {
  for (const listener of listeners) listener(event)
}

export function isSupportedImportSource(sourcePath: string): boolean {
  return /\.(txt|md|markdown|doc|docx|xls|xlsx|ppt|pptx|pdf)$/i.test(sourcePath)
}

export function sourceFormat(sourcePath: string): "txt" | "markdown" | "doc" | "docx" | "xls" | "xlsx" | "ppt" | "pptx" | "pdf" {
  if (/\.(md|markdown)$/i.test(sourcePath)) return "markdown"
  if (/\.doc$/i.test(sourcePath)) return "doc"
  if (/\.docx$/i.test(sourcePath)) return "docx"
  if (/\.xlsx$/i.test(sourcePath)) return "xlsx"
  if (/\.xls$/i.test(sourcePath)) return "xls"
  if (/\.ppt$/i.test(sourcePath)) return "ppt"
  if (/\.pptx$/i.test(sourcePath)) return "pptx"
  if (/\.pdf$/i.test(sourcePath)) return "pdf"
  return "txt"
}

export function sourceName(sourcePath: string): string {
  return sourcePath.split(/[\\/]/).filter(Boolean).at(-1) || ""
}

export function updateImportTasks(
  tasks: DesktopImportTask[],
  jobId: string,
  status: "paused" | "cancelled",
): DesktopImportTask[] {
  return tasks.map((task) => {
    if (task.job.jobId !== jobId) return task
    const activeStage = task.stages.find((stage) => stage.status === "running")
      ?? task.stages.find((stage) => stage.status === "pending")
    return {
      ...task,
      job: { ...task.job, status },
      stages: task.stages.map((stage) => (
        stage.stageRunId === activeStage?.stageRunId
          ? { ...stage, status, errorCode: `import_${status}` }
          : stage
      )),
    }
  })
}
