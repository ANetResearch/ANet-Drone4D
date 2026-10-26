// Public addresses of ANet4D (v0.3.2 rename; the product was formerly named ANet Drone4D). The single place for the
// repository, public site and documentation URLs used by the app, the landing page, the /lab/ static build and the demo
// build's HTML meta tags (vite.config.ts). No imports, so the Vite config can read it as well.
// - The GitHub repository is ANetResearch/ANet4D (renamed from ANet-Drone4D; GitHub redirects the old URL).
// - The public site moved to anet4d.agentnetwork.org.cn; drone4d.agentnetwork.org.cn is a permanent (301) redirect.
// - The documentation moved to docs.agentnetwork.org.cn/anet4d/; the old /drone4d/ path serves redirect pages.

/** product name (exact casing; also brand.product in the dictionaries) */
export const PRODUCT_NAME = 'ANet4D'
/** GitHub repository (legacy repository name, renamed later; GitHub redirects) */
export const REPO_URL = 'https://github.com/ANetResearch/ANet4D'
/** the repository URL without the scheme, as shown in About */
export const REPO_LABEL = REPO_URL.replace(/^https:\/\//, '')
/** origin of the public site (no trailing slash) */
export const SITE_ORIGIN = 'https://anet4d.agentnetwork.org.cn'
/** documentation (Sphinx, Read the Docs layout), Chinese edition, latest version */
export const DOCS_URL = 'https://docs.agentnetwork.org.cn/anet4d/zh-cn/latest/'
