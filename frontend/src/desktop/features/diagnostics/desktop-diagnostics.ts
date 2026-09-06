import { invoke } from "@tauri-apps/api/core"

export type DesktopDiagnosticLevel = "TRACE" | "DEBUG" | "INFO" | "WARN" | "ERROR"

export interface DesktopDiagnosticStatus {
  configuredLevel: DesktopDiagnosticLevel
  effectiveLevel: DesktopDiagnosticLevel
  configuredComponents: Record<string, DesktopDiagnosticLevel>
  effectiveComponents: Record<string, DesktopDiagnosticLevel>
  warnings: string[]
  configurationFile: string
  sensitiveTraceActive: boolean
  sensitiveTraceCaptureId: string | null
  sensitiveTraceExpiresAt: string | null
  sensitiveTraceSizeBytes: number
  traceComponents: string[]
}

export function desktopDiagnosticStatus(): Promise<DesktopDiagnosticStatus> {
  return invoke<DesktopDiagnosticStatus>("desktop_diagnostic_status")
}

export function stopDesktopSensitiveTrace(): Promise<DesktopDiagnosticStatus> {
  return invoke<DesktopDiagnosticStatus>("desktop_stop_sensitive_trace")
}

export function revealDesktopSensitiveTraceDirectory(): Promise<void> {
  return invoke<void>("desktop_reveal_sensitive_trace_directory", { confirmed: true })
}
