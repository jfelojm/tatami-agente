import os

import gspread
from dotenv import load_dotenv
from supabase import create_client
from google_credentials import google_credentials

load_dotenv()


# ── 1. GOOGLE SHEETS ──────────────────────────────────────────
def test_sheets():
    print("\n[1] Probando Google Sheets...")
    try:
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ]
        creds = google_credentials(scopes)
        gc = gspread.authorize(creds)
        sh = gc.open_by_key(os.getenv("SPREADSHEET_ID"))
        hojas = [ws.title for ws in sh.worksheets()]
        print(f"    OK: Conectado. Hojas encontradas: {len(hojas)}")
        for h in hojas:
            print(f"       - {h}")
    except Exception as e:
        print(f"    ERROR: {e}")


# ── 2. SUPABASE ───────────────────────────────────────────────
def test_supabase():
    print("\n[2] Probando Supabase...")
    try:
        client = create_client(
            os.getenv("SUPABASE_URL"),
            os.getenv("SUPABASE_KEY"),
        )
        # Verifica que las 3 tablas existen
        for tabla in ["hist_ventas", "mov_inventario", "hist_precios"]:
            client.table(tabla).select("*").limit(1).execute()
            print(f"    OK: Tabla '{tabla}' accesible")
    except Exception as e:
        print(f"    ERROR: {e}")


# ── MAIN ──────────────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 50)
    print("TATAMI AGENTE — TEST DE CONEXIONES")
    print("=" * 50)
    test_sheets()
    test_supabase()
    print("\n" + "=" * 50)

