"""
Crea (si no existe) la hoja BD_CONFIG en el Spreadsheet y la inicializa con claves base.
"""

import os

import gspread
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials


load_dotenv(override=True)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]


def main():
    creds = Credentials.from_service_account_file(
        os.getenv("GOOGLE_CREDENTIALS_PATH"), scopes=SCOPES
    )
    sh = gspread.authorize(creds).open_by_key(os.getenv("SPREADSHEET_ID"))

    try:
        ws = sh.worksheet("BD_CONFIG")
        print("BD_CONFIG ya existe - no se recrea.")
        return
    except Exception:
        pass

    ws = sh.add_worksheet(title="BD_CONFIG", rows=200, cols=4)

    headers = [["clave", "valor", "descripcion", "tipo"]]
    seed = [
        ["umbral_alerta_precio", "0.05", "Variacion de precio para alertar (0.05 = 5%)", "float"],
        ["proveedores_piloto_tokens", "ITALDELI,GALABDISTRI,MARAMAR,PACHECO,ELJURI", "Filtro de proveedores piloto (contiene token en razon_social)", "csv"],
        ["par_level_dias_cobertura", "7", "Dias de cobertura para par_level", "int"],
        ["smartmenu_sucursal", "1", "Sucursal Smart Menu (param sucursal)", "int"],
        ["smartmenu_caja", "1", "Caja Smart Menu (param caja)", "int"],
    ]

    ws.update("A1:D1", headers)
    ws.update(f"A2:D{len(seed)+1}", seed)
    print("BD_CONFIG creada e inicializada.")


if __name__ == "__main__":
    main()

