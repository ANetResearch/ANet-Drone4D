// /lab/ 公开只读构建的顶栏（AWR-14 §21.2.4；AWR-15 §4.2 "顶栏锁定组合"落点：头像标 24 px；ADR-129）。Owner: M15.
// 品牌资产只在 AWR-15 §4.2 的落点引用（check-brand BRAND-02）：/lab/ 构建的落点为本文件（顶栏）与 index.html（启动遮罩、
// favicon）。/lab/ 构建以 base = /lab/ 部署，头像标按 BASE_URL 解析（/lab/brand/，LAB_BRAND_FILES），不用 ui/brand 的根路径常量。
import { DEMO_DOCS_URL } from '@/lib/demo'
import { Badge } from '@/ui/components/ui/badge'
import { Button } from '@/ui/components/ui/button'
import { Icon } from '@/ui/icons/Icon'
import { getLabLocale, setLabLocale, useLabT } from './i18n'
import { LAB_PREFIX, onLabLinkClick } from './router'

const BRAND_AVATAR = `${import.meta.env.BASE_URL}brand/avatar-96.png`

export function LabHeader() {
  const t = useLabT()
  const other = getLabLocale() === 'zh-CN' ? 'en' : 'zh-CN'
  return (
    <header className="sticky top-0 z-(--z-header) border-b border-border bg-background">
      <div className="mx-auto flex h-11 max-w-6xl items-center gap-3 px-4 sm:px-6">
        <a href={`${LAB_PREFIX}/`} onClick={(e) => onLabLinkClick(e, `${LAB_PREFIX}/`)} className="flex min-h-11 items-center gap-2.5 rounded-md">
          <img src={BRAND_AVATAR} width={24} height={24} alt="" aria-hidden="true" data-brand-avatar="" className="size-6 shrink-0" />
          <span className="text-sm font-semibold tracking-tight whitespace-nowrap">{t('lab.public.brand')}</span>
        </a>
        <Badge variant="outline" className="max-sm:hidden" data-lab-public-badge="">{t('lab.public.site')}</Badge>
        <nav className="ml-2 hidden items-center gap-0.5 md:flex" aria-label={t('lab.public.brand')}>
          <Button variant="ghost" size="sm" className="h-8 px-3 text-sm" nativeButton={false}
            render={<a href={`${LAB_PREFIX}/`} onClick={(e) => onLabLinkClick(e, `${LAB_PREFIX}/`)} />}>
            {t('lab.public.nav.samples')}
          </Button>
        </nav>
        <div className="ml-auto flex items-center gap-1">
          <Button variant="ghost" size="sm" className="h-8 px-3 text-sm" lang={other} onClick={() => setLabLocale(other, true)} data-lab-lang="">
            {t('lab.public.lang')}
          </Button>
          <Button variant="ghost" size="sm" className="h-8 px-3 text-sm max-sm:hidden" nativeButton={false} render={<a href="/" />}>
            {t('lab.public.nav.sandbox')}
          </Button>
          <Button variant="ghost" size="sm" className="h-8 px-3 text-sm max-sm:hidden" nativeButton={false}
            render={<a href={DEMO_DOCS_URL} target="_blank" rel="noreferrer noopener" />}>
            {t('lab.public.nav.docs')}
            <Icon icon="external" data-icon="inline-end" />
          </Button>
        </div>
      </div>
    </header>
  )
}
