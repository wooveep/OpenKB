import { StrictMode } from "react"
import { createRoot } from "react-dom/client"
import DesktopWorkbenchRoot from "./desktop/DesktopWorkbenchRoot"
import { ThemeProvider } from "@/lib/theme"
import { LanguageProvider } from "@/lib/language"
import "./lib/i18n"
import "./index.css"

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <LanguageProvider>
      <ThemeProvider>
        <DesktopWorkbenchRoot />
      </ThemeProvider>
    </LanguageProvider>
  </StrictMode>,
)
