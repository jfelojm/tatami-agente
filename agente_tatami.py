import argparse
import os
import subprocess
import sys

from dotenv import load_dotenv


def _venv_python() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    py = os.path.join(here, "venv", "Scripts", "python.exe")
    if os.path.exists(py):
        return py
    # fallback: maybe user calls from activated venv
    return sys.executable


def _run(module: str, args: list[str] | None = None):
    args = args or []
    py = _venv_python()
    cmd = [py, module, *args]
    print("\n$ " + " ".join(cmd))
    raise SystemExit(subprocess.call(cmd))


def main():
    load_dotenv(override=True)

    p = argparse.ArgumentParser(
        prog="agente_tatami",
        description="Launcher de módulos Tatami Agente.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    # ventas
    p_ventas = sub.add_parser("ventas", help="Carga ventas Smart Menu -> hist_ventas")
    p_ventas.add_argument("--historico", action="store_true", help="Modo rango de fechas")
    p_ventas.add_argument("fecha_inicio", nargs="?", help="YYYY-MM-DD (solo con --historico)")
    p_ventas.add_argument("fecha_fin", nargs="?", help="YYYY-MM-DD (solo con --historico)")

    # descargo
    sub.add_parser("descargo", help="Descargo inventario desde hist_ventas -> mov_inventario")

    # facturas
    p_fact = sub.add_parser("facturas", help="Procesa XML facturas desde Drive")
    p_fact.add_argument("--dry-run", action="store_true", help="No escribe (solo valida)")

    # par levels
    p_par = sub.add_parser("par-levels", help="Calcula par levels y consumo diario en Sheets")
    p_par.add_argument("--dry-run", action="store_true", help="No escribe en Sheets")

    # pedidos
    p_ped = sub.add_parser("pedidos", help="Genera pedidos por proveedor (WhatsApp)")
    p_ped.add_argument("--dry-run", action="store_true", help="Solo imprime (recomendado)")

    # reporte semanal
    p_rep = sub.add_parser("reporte", help="Reporte semanal (ventas/costos/precios/stock)")
    p_rep.add_argument("--dry-run", action="store_true", help="Solo imprime (default)")

    a = p.parse_args()

    if a.cmd == "ventas":
        if a.historico:
            if not a.fecha_inicio or not a.fecha_fin:
                print("ERROR: falta fecha_inicio y fecha_fin con --historico")
                raise SystemExit(2)
            _run("ventas_smartmenu.py", ["--historico", a.fecha_inicio, a.fecha_fin])
        else:
            _run("ventas_smartmenu.py")

    if a.cmd == "descargo":
        _run("descargo_inventario.py")

    if a.cmd == "facturas":
        _run("procesar_facturas_drive.py", ["--dry-run"] if a.dry_run else [])

    if a.cmd == "par-levels":
        _run("calcular_par_levels.py", ["--dry-run"] if a.dry_run else [])

    if a.cmd == "pedidos":
        _run("generar_pedidos.py", ["--dry-run"] if a.dry_run else [])

    if a.cmd == "reporte":
        _run("reporte_semanal.py", ["--dry-run"] if a.dry_run else [])


if __name__ == "__main__":
    main()

