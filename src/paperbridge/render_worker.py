"""Render one PDF page without host credentials; communicate only over pipes."""
from __future__ import annotations

import base64
import io
import json
import math
import sys


def main():
    if sys.platform == "linux":
        import resource
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        resource.setrlimit(resource.RLIMIT_CPU, (15, 15))
        resource.setrlimit(resource.RLIMIT_AS, (768 * 1024**2, 768 * 1024**2))
        resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
    import pypdfium2 as pdfium

    number, edge = int(sys.argv[1]), int(sys.argv[2])
    if not 1 <= number <= 200 or not 800 <= edge <= 2400:
        raise ValueError()
    data = sys.stdin.buffer.read(20_000_001)
    if len(data) > 20_000_000 or not data.startswith(b"%PDF-"):
        raise ValueError()
    document = pdfium.PdfDocument(data)
    try:
        if not 1 <= len(document) <= 200 or number > len(document):
            raise ValueError()
        page = document[number - 1]
        try:
            width, height = page.get_size()
            if not all(math.isfinite(x) and x > 0 for x in (width, height)):
                raise ValueError()
            bitmap = page.render(scale=edge / max(width, height), may_draw_forms=False)
            try:
                image = bitmap.to_pil()
                output = io.BytesIO()
                image.save(output, format="PNG")
                png = output.getvalue()
                if len(png) > 8_000_000 or image.width * image.height > 6_000_000:
                    raise ValueError()
                print(json.dumps({"png": base64.b64encode(png).decode("ascii"),
                                  "width": image.width, "height": image.height}))
            finally:
                bitmap.close()
        finally:
            page.close()
    finally:
        document.close()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Do not expose parser diagnostics or document content.
        raise SystemExit(1) from None
