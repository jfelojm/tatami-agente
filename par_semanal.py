"""
PAR semanal (BD_CONFIG par_cron_*): calcular_par_levels + post acción.

Uso:
  python par_semanal.py
"""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent

try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env", override=True)
except ImportError:
    pass

ZONA_EC = ZoneInfo("America/Guayaquil")


def main() -> int:
    from config_sheets import cfg

    script = str(cfg("par_script", "calcular_par_levels.py") or "calcular_par_levels.py")
    post = str(cfg("par_post_accion", "recalcular_stock_sheets") or "").strip()

    print("=" * 60)
    print(f"PAR SEMANAL — {datetime.now(ZONA_EC):%Y-%m-%d %H:%M} EC")
    print("=" * 60)

    rc = subprocess.run(
        [sys.executable, str(ROOT / script)],
        cwd=str(ROOT),
    ).returncode
    if rc != 0:
        try:
            from estrategia_config import telefonos_por_roles
            from alertas_pipeline import enviar_mensaje_wa

            for tel, lab in telefonos_por_roles(["ADMIN"]):
                enviar_mensaje_wa(tel, f"❌ Fallo PAR semanal (exit {rc})", etiqueta=lab)
        except Exception:
            pass
        return rc

    if post == "recalcular_stock_sheets":
        subprocess.run(
            [sys.executable, str(ROOT / "recalcular_stock_sheets.py"), "--produccion"],
            cwd=str(ROOT),
        )

    try:
        from alertas_pipeline import ping_wa_paso_proceso

        ping_wa_paso_proceso("PAR levels (semanal)")
    except Exception as e:
        print(f"  WARN: ping WA PAR: {e}")

    print("PAR semanal OK.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
