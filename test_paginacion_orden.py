"""Toda paginación Supabase con .range(offset, ...) debe llevar .order() por columna única."""
import re
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parent
VENTANA = 15
_RANGE_OFFSET = re.compile(r"\.range\(\s*offset")
_DIRS_CODIGO = (".", "dashboard_services")


def _faltantes() -> list[str]:
    out = []
    for d in _DIRS_CODIGO:
        paths = sorted((BASE / d).glob("*.py"))
        out.extend(_faltantes_en(paths))
    return out


def _faltantes_en(paths: list[Path]) -> list[str]:
    out = []
    for path in paths:
        rel = path.relative_to(BASE)
        if path.name.startswith(("_", "test_")):
            continue
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        for i, line in enumerate(lines):
            if not _RANGE_OFFSET.search(line):
                continue
            ctx = "\n".join(lines[max(0, i - VENTANA) : i + 1])
            if ".order(" not in ctx:
                out.append(f"{rel}:{i + 1}")
    return out


class TestPaginacionOrden(unittest.TestCase):
    def test_range_con_order(self):
        self.assertEqual(_faltantes(), [], "Paginación sin .order(): filas repetidas u omitidas")


if __name__ == "__main__":
    unittest.main()
