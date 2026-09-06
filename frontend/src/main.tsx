import { StrictMode } from "react"
import { createRoot } from "react-dom/client"
import DesktopWorkbenchRoot from "@/desktop/app/DesktopWorkbenchRoot"
import { ThemeProvider } from "@/lib/theme"
import { LanguageProvider } from "@/lib/language"
import { ZoomProvider } from "@/lib/zoom"
import "./lib/i18n"
import "./index.css"

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <LanguageProvider>
      <ThemeProvider>
        <ZoomProvider><DesktopWorkbenchRoot /></ZoomProvider>
      </ThemeProvider>
    </LanguageProvider>
  </StrictMode>,
)
