import i18n from "i18next"
import { initReactI18next } from "react-i18next"
import resourcesToBackend from "i18next-resources-to-backend"

export const SUPPORTED_LANGUAGES = ["zh", "en"] as const
export type Language = (typeof SUPPORTED_LANGUAGES)[number]

/** Desktop UI translations are kept in one always-loaded namespace. */
export const NAMESPACES = ["common"] as const

void i18n
  // Turns a dynamic import() into the locale backend used by the desktop shell.
  .use(
    resourcesToBackend(
      (language: string, namespace: string) => import(`../locales/${language}/${namespace}.json`),
    ),
  )
  .use(initReactI18next)
  .init({
    fallbackLng: "zh",
    supportedLngs: [...SUPPORTED_LANGUAGES],
    ns: ["common"], // preload only the always-visible chrome; rest are lazy
    defaultNS: "common",
    load: "languageOnly", // "en-US" → "en"
    interpolation: { escapeValue: false }, // React already escapes
    react: { useSuspense: false }, // no Suspense boundaries in the tree; avoid a hard suspend
  })

export default i18n
