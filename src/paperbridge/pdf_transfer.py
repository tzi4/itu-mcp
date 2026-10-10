"""Transfer a bounded PDF inside its authenticated browser session."""
from __future__ import annotations

import base64
from .browser import RetrievalUnavailable


def browser_pdf(page, url: str, allowed, *, max_bytes: int = 20_000_000) -> bytes:
    if not allowed(url):
        raise RetrievalUnavailable("publisher_redirect_not_allowed")
    def guard(route):
        request = route.request
        if (request.method == "GET" and request.redirected_from is None
                and allowed(request.url)):
            route.fallback()
        else:
            route.abort()
    page.route("**/*", guard)
    try:
        result = page.evaluate('''async ({url, limit}) => {
            const controller = new AbortController();
            const timer = setTimeout(() => controller.abort(), 25000);
            try {
                const response = await fetch(url, {
                    credentials: 'include', redirect: 'error', signal: controller.signal
                });
                if (response.status !== 200) return {reason: 'publisher_access_unavailable'};
                const reader = response.body.getReader();
                const chunks = [];
                let length = 0;
                while (true) {
                    const {done, value} = await reader.read();
                    if (done) break;
                    length += value.length;
                    if (length > limit) return {reason: 'pdf_size_limit_exceeded'};
                    chunks.push(value);
                }
                const pieces = [];
                for (const chunk of chunks) {
                    for (let i = 0; i < chunk.length; i += 16384)
                        pieces.push(String.fromCharCode(...chunk.subarray(i, i + 16384)));
                }
                return {data: btoa(pieces.join(''))};
            } catch (_) {
                return {reason: 'publisher_download_failed'};
            } finally {
                clearTimeout(timer);
                controller.abort();
            }
        }''', {"url": url, "limit": max_bytes})
        if result.get("reason"):
            raise RetrievalUnavailable(result["reason"])
        if len(result["data"]) > 4 * ((max_bytes + 2) // 3):
            raise RetrievalUnavailable("pdf_size_limit_exceeded")
        data = base64.b64decode(result["data"], validate=True)
        if len(data) > max_bytes:
            raise RetrievalUnavailable("pdf_size_limit_exceeded")
        if not data.startswith(b"%PDF-"):
            raise RetrievalUnavailable("publisher_did_not_return_pdf")
        return data
    finally:
        page.unroute("**/*", guard)
