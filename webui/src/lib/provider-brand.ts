export interface ProviderBrand {
  logoUrl: string;
  logoUrls: string[];
  color: string;
  initials: string;
}

function officialFaviconUrl(domain: string): string {
  return `https://${domain}/favicon.ico`;
}

function duckDuckGoFaviconUrl(domain: string): string {
  return `https://icons.duckduckgo.com/ip3/${encodeURIComponent(domain)}.ico`;
}

function googleFaviconUrl(domain: string): string {
  return `https://www.google.com/s2/favicons?domain=${encodeURIComponent(domain)}&sz=64`;
}

export function faviconUrls(domain: string): string[] {
  const faviconDomain = faviconDomainFromValue(domain);
  return [
    officialFaviconUrl(faviconDomain),
    duckDuckGoFaviconUrl(faviconDomain),
    googleFaviconUrl(domain),
  ];
}

function brand(
  domain: string,
  color: string,
  initials: string,
  logoOverrides: string[] = [],
): ProviderBrand {
  const logoUrls = [...logoOverrides];
  faviconUrls(domain).forEach((url) => addUniqueLogoUrl(logoUrls, url));
  return {
    logoUrl: logoUrls[0],
    logoUrls,
    color,
    initials,
  };
}

function addUniqueLogoUrl(urls: string[], url: string | null | undefined): void {
  const value = url?.trim();
  if (value && !urls.includes(value)) urls.push(value);
}

function domainFromLogoUrl(url: string): string | null {
  if (url.startsWith("/")) return null;
  try {
    const parsed = new URL(url);
    if (!/^https?:$/.test(parsed.protocol)) return null;
    const host = parsed.hostname.toLowerCase();
    if (host === "www.google.com" || host === "google.com") {
      return parsed.searchParams.get("domain");
    }
    if (host === "icons.duckduckgo.com") {
      const match = parsed.pathname.match(/^\/ip3\/(.+)\.ico$/);
      return match ? decodeURIComponent(match[1]) : null;
    }
    return host.replace(/^www\./, "");
  } catch {
    return null;
  }
}

function faviconDomainFromValue(value: string): string {
  const host = value.split("/")[0]?.trim();
  return host || value;
}

export function logoFallbackUrls(logoUrl: string | null | undefined): string[] {
  const value = logoUrl?.trim();
  if (!value) return [];
  if (value.startsWith("/")) return [value];

  const urls: string[] = [];
  const domain = domainFromLogoUrl(value);
  const isFaviconProxy = /^(https?:\/\/)?(www\.google\.com|google\.com|icons\.duckduckgo\.com)\//i.test(value);
  if (domain && isFaviconProxy) {
    addUniqueLogoUrl(urls, value);
    faviconUrls(domain).forEach((url) => addUniqueLogoUrl(urls, url));
    return urls;
  }
  addUniqueLogoUrl(urls, value);
  if (domain) faviconUrls(domain).forEach((url) => addUniqueLogoUrl(urls, url));
  return urls;
}

export const PROVIDER_BRAND_ALIASES: Record<string, string> = {
  brave_search: "brave",
};

export const PROVIDER_LABEL_ALIASES: Record<string, string> = {
  brave_search: "Brave Search",
};

const PROVIDER_BRANDS: Record<string, ProviderBrand> = {
  agnes: brand("agnes-ai.com", "#FF6B35", "A"),
  brave: brand("brave.com", "#FB542B", "B"),
  custom: brand("localhost", "#6B7280", "C"),
  deepseek: brand("deepseek.com", "#4D6BFE", "DS"),
  duckduckgo: brand("duckduckgo.com", "#DE5833", "DDG"),
  exa: brand("exa.ai", "#5B5BF6", "E"),
  jina: brand("jina.ai", "#7C3AED", "J"),
  kagi: brand("kagi.com", "#FFB319", "K"),
  olostep: brand("olostep.com", "#111827", "O"),
  opencode: brand("opencode.ai", "#7C3AED", "OC"),
  tavily: brand("tavily.com", "#111827", "T"),
};

export function providerBrand(provider: string | null | undefined): ProviderBrand | null {
  if (!provider) return null;
  const key = PROVIDER_BRAND_ALIASES[provider] ?? provider;
  return PROVIDER_BRANDS[key] ?? null;
}

export function providerDisplayLabel(
  providers: Array<{ name: string; label: string }>,
  value: string | null | undefined,
): string {
  if (!value) return "";
  return providers.find((provider) => provider.name === value)?.label
    ?? PROVIDER_LABEL_ALIASES[value]
    ?? value;
}

export function inferProviderFromModelName(modelName: string | null | undefined): string | null {
  const normalized = (modelName ?? "").trim().toLowerCase();
  if (!normalized) return null;
  const prefix = normalized.split(/[/:]/)[0];
  if (providerBrand(prefix)) return prefix;
  if (/deepseek/.test(normalized)) return "deepseek";
  if (/opencode|big-pickle/.test(normalized)) return "opencode";
  return null;
}

// ---------------------------------------------------------------------------
// Custom provider 动态 brand 工具(基于 api_base 生成 favicon + 首字母 + 稳定颜色)
// 共享给 ProviderIcon / ComposerModelBadge / ThreadHeader 等组件。
// ---------------------------------------------------------------------------

/** 从 api_base URL 提取 host(去掉通用前缀 www/api/apihub/api-gateway/gateway)。
 *  如 https://apihub.agnes-ai.com/v1 → agnes-ai.com */
export function hostFromApiBase(apiBase: string | null | undefined): string | null {
  if (!apiBase) return null;
  try {
    const parsed = new URL(apiBase);
    let host = parsed.hostname.toLowerCase();
    host = host.replace(/^(www|api|apihub|api-gateway|gateway)\./, "");
    return host || null;
  } catch {
    return null;
  }
}

/** 从 host 生成首字母(取第一段非通用前缀的首字母,大写;失败回退 "C") */
export function initialsFromHost(host: string | null): string {
  if (!host) return "C";
  return host.split(".")[0].charAt(0).toUpperCase() || "C";
}

/** 从 host 生成稳定颜色(基于域名 hash → HSL);失败回退灰色 #6B7280 */
export function colorFromHost(host: string | null): string {
  if (!host) return "#6B7280";
  let hash = 0;
  for (let i = 0; i < host.length; i++) hash = host.charCodeAt(i) + ((hash << 5) - hash);
  return `hsl(${Math.abs(hash) % 360}, 55%, 50%)`;
}

/** 解析 custom provider 的 brand:有 api_base → 用域名生成 favicon + 颜色 + 首字母;
 *  无 api_base → 回退到内置 custom brand。供所有需要展示 custom 图标的组件复用。 */
export function resolveCustomBrand(
  provider: string | null | undefined,
  apiBase: string | null | undefined,
): ProviderBrand | null {
  const isCustom = provider === "custom" || (provider?.startsWith("custom__") ?? false);
  if (!isCustom) return providerBrand(provider);
  const host = hostFromApiBase(apiBase);
  if (!host) return providerBrand("custom");
  const urls = faviconUrls(host);
  return {
    logoUrl: urls[0] ?? "",
    logoUrls: urls,
    color: colorFromHost(host),
    initials: initialsFromHost(host),
  };
}
