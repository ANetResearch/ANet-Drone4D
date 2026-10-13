// The landing page's trimmed i18n dictionary (vite.config.ts landingI18nPlugin; D2.2-FX-landing).
declare module 'virtual:awr-landing-i18n' {
  const dict: { 'zh-CN': Readonly<Record<string, string>>; en: Readonly<Record<string, string>> }
  export default dict
}
