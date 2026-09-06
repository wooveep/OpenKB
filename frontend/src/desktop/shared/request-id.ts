let requestSequence = 0

/** Generate a locally unique request id for a Desktop Bridge mutation or read. */
export function nextDesktopRequestId(scope: string): string {
  requestSequence += 1
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID()
  }
  return `desktop-${scope}-${Date.now()}-${requestSequence}`
}
