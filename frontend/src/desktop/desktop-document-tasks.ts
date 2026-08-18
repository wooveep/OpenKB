import type { DesktopImportTask } from "./contracts"

/** Keep one current projection per document while retaining pre-publication jobs. */
export function currentDocumentTasks(tasks: DesktopImportTask[]): DesktopImportTask[] {
  const seen = new Set<string>()
  return tasks.filter((task) => {
    const key = task.document
      ? `document:${task.document.documentId}`
      : `job:${task.job.jobId}`
    if (seen.has(key)) return false
    seen.add(key)
    return true
  })
}

export function taskIsFailed(task: DesktopImportTask): boolean {
  return task.document?.availability === "failed"
    || ["failed", "quarantined", "cancelled"].includes(task.job.status)
}
