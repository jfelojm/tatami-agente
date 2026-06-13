"""Quita JS incrustado y deja tags src (el servidor los inline al servir)."""
import re
from pathlib import Path

html_path = Path(__file__).resolve().parent.parent / "dashboard.html"
html = html_path.read_text(encoding="utf-8")

replacement = (
    '<script src="/dashboard-filters.js"></script>\n'
    '<script src="/dashboard-app.js"></script>\n'
)

pattern = r'<script>\s*/\* Filtros compartidos:.*?</script>\s*<script>\s*/\* Portal multi-dashboard.*?</script>\s*'
new_html, n = re.subn(pattern, replacement, html, count=1, flags=re.DOTALL)
if n != 1:
    raise SystemExit(f"Expected 1 replacement, got {n}")

html_path.write_text(new_html, encoding="utf-8")
print(f"OK: removed inline JS, {len(new_html)} chars")
