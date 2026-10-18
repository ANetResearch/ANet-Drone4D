// D3 公开只读构建的开关与路径（扩展点 15，AWR-05 §12.3 第 15 处、§7.9、ADR-129；AWR-14 §21.2.3、§21.14；D3-AC-21）。Owner: M15.
// 两个开关都是构建期常量（vite.config.ts 校验取值只能是 0 或 1，缺省 0）：
// - VITE_AWR_LAB_ONLY = 1：`/lab/` 独立静态构建（base = /lab/），入口 app/lab-public/entry.tsx；界面只读，数据只来自 /lab-static/。
// - VITE_AWR_LAB_PUBLIC = 1：D2 演示构建显示"事件沙盘"入口，链接到 LAB_PUBLIC_ENTRY（整页跳转到静态构建，不经应用路由）。
// 本模块无导入：演示构建的落地页分块可直接引用 LAB_PUBLIC 与 LAB_PUBLIC_ENTRY，不带入 lab-public 的其他模块。

export type LabBuild = 'internal' | 'public'

/** 本构建是否为 /lab/ 公开只读构建 */
export const LAB_ONLY: boolean = import.meta.env.VITE_AWR_LAB_ONLY === '1'
/** 演示构建是否显示"事件沙盘"入口 */
export const LAB_PUBLIC: boolean = import.meta.env.VITE_AWR_LAB_PUBLIC === '1'
/** AWR-14 §21.2.3 的构建种类 */
export const LAB_BUILD: LabBuild = LAB_ONLY ? 'public' : 'internal'

/** 静态构建的挂载前缀（vite base） */
export const LAB_BASE = '/lab/'
/** D2 演示构建的"事件沙盘"入口链接 */
export const LAB_PUBLIC_ENTRY = LAB_BASE
/** 静态制品根（AWR-17 §18.8.5）：index.json 在版本目录之外（no-cache），其余文件只经 v/<version>/ 访问（immutable） */
export const LAB_STATIC_ROOT = '/lab-static/'
export const LAB_STATIC_INDEX = `${LAB_STATIC_ROOT}index.json`
/** 公开只读页允许请求的前缀（AWR-14 §21.14.2 规则 1：只发起 /lab/ 与 /lab-static/ 的 GET） */
export const LAB_ALLOWED_PREFIXES: readonly string[] = [LAB_BASE, LAB_STATIC_ROOT]
/** 单文件上限（AWR-17 §18.8.5 规则 3，M27-FR-136）：超过的制品不放 emax，下载指向分发渠道 */
export const LAB_STATIC_MAX_FILE_BYTES = 64 * 1024 * 1024
