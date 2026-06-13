"""Incrusta dashboard_filters.js y dashboard_app.js en dashboard.html."""
from pathlib import Path

base = Path(__file__).resolve().parent.parent
html_path = base / "dashboard.html"
html = html_path.read_text(encoding="utf-8")

for fname, tag in (
    ("dashboard_filters.js", "/dashboard-filters.js"),
    ("dashboard_app.js", "/dashboard-app.js"),
):
    src = f'<script src="{tag}"></script>'
    if src not in html:
        print(f"SKIP (ya incrustado): {fname}")
        continue
    js_path = base / fname
    if not js_path.is_file():
        raise SystemExit(f"Falta {js_path}")
    js = js_path.read_text(encoding="utf-8").replace("</script>", "<\\/script>")
    html = html.replace(src, f"<script>\n{js}\n</script>")
    print(f"Incrustado: {fname}")

html_path.write_text(html, encoding="utf-8")
assert "getCheckedValues" in html
assert "/dashboard-filters.js" not in html
print("OK — dashboard.html autónomo")
