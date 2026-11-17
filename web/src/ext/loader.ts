const cache = new Map<string, Promise<unknown>>()

/** Import an extension's UI module once per URL and version. A failure is cached too, until the version changes. */
export function loadModule(url: string, version = ''): Promise<unknown> {
  const k = `${url}\u0000${version}`
  let p = cache.get(k)
  if (!p) {
    // The version in the URL: browsers cache modules by URL for the page's lifetime.
    p = import(/* @vite-ignore */ version ? `${url}?v=${encodeURIComponent(version)}` : url)
    cache.set(k, p)
  }
  return p
}
