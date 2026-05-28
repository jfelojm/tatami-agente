"""
setup_form_proveedor.py
───────────────────────
Script de ejecución ÚNICA.
Crea el Google Form "Tatami — Proveedor Nuevo" y la hoja STAGING_PROVEEDORES
en el Master Sheets con fórmula de cod_proveedor automático.

Uso:
    python setup_form_proveedor.py

Al terminar imprime el link del Form para compartir con Jacky, Edu y Mary.
"""

import os
import logging
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/forms.body",
]

MASTER_ID  = os.environ["SPREADSHEET_ID"]
CREDS_PATH = os.environ["GOOGLE_CREDENTIALS_PATH"]


def get_creds() -> Credentials:
    return Credentials.from_service_account_file(CREDS_PATH, scopes=SCOPES)


# ─── CREAR GOOGLE FORM ───────────────────────────────────────────────────────

def crear_form(creds: Credentials) -> dict:
    """Crea el Form con todos los campos y retorna {formId, responderUri}."""
    forms = build("forms", "v1", credentials=creds)

    # 1. Crear form vacío con título
    form_body = {
        "info": {
            "title": "Tatami — Proveedor Nuevo",
            "documentTitle": "Tatami — Proveedor Nuevo"
        }
    }
    form = forms.forms().create(body=form_body).execute()
    form_id = form["formId"]
    log.info(f"Form creado — ID: {form_id}")

    # 2. Agregar descripción y preguntas via batchUpdate
    requests = []

    # Descripción del formulario
    requests.append({
        "updateFormInfo": {
            "info": {
                "description": (
                    "Completa este formulario para registrar un proveedor nuevo. "
                    "Mary revisará la información antes de ingresarla al sistema."
                )
            },
            "updateMask": "description"
        }
    })

    # Definición de preguntas
    preguntas = [
        {
            "title": "Razón Social",
            "required": True,
            "type": "TEXT",
        },
        {
            "title": "RUC",
            "description": "13 dígitos sin espacios ni guiones",
            "required": True,
            "type": "TEXT",
        },
        {
            "title": "¿Es proveedor de inventario?",
            "description": "SI = aparece en pedidos automáticos del sistema",
            "required": True,
            "type": "RADIO",
            "options": ["SI", "NO"],
        },
        {
            "title": "Correo electrónico",
            "required": False,
            "type": "TEXT",
        },
        {
            "title": "¿Activo?",
            "required": True,
            "type": "RADIO",
            "options": ["SI", "NO"],
            "default": "SI",
        },
        {
            "title": "WhatsApp de contacto",
            "description": "Número con código de país. Ej: 593998765432",
            "required": True,
            "type": "TEXT",
        },
        {
            "title": "Dirección",
            "required": False,
            "type": "TEXT",
        },
        {
            "title": "Nombre del contacto",
            "description": "Persona con quien se coordinan los pedidos",
            "required": True,
            "type": "TEXT",
        },
        {
            "title": "Condición de pago",
            "required": True,
            "type": "RADIO",
            "options": ["CONTADO", "CRÉDITO"],
        },
        {
            "title": "Días de crédito",
            "description": "Escribir 0 si es contado",
            "required": True,
            "type": "TEXT",
        },
        {
            "title": "Lead time (días)",
            "description": "Días entre que se hace el pedido y llega la mercadería",
            "required": True,
            "type": "TEXT",
        },
        {
            "title": "Frecuencia de compra (días)",
            "description": "Cada cuántos días se hace normalmente un pedido",
            "required": True,
            "type": "TEXT",
        },
        {
            "title": "Días de pedido",
            "description": "Días válidos para hacer el pedido a este proveedor",
            "required": True,
            "type": "CHECKBOX",
            "options": ["LUN", "MAR", "MIE", "JUE", "VIE", "SAB", "DOM"],
        },
        {
            "title": "Observaciones",
            "description": "Notas operativas relevantes (opcional)",
            "required": False,
            "type": "PARAGRAPH",
        },
    ]

    # Construir items para batchUpdate
    for idx, p in enumerate(preguntas):
        item = {
            "title": p["title"],
            "questionItem": {}
        }
        if "description" in p:
            item["description"] = p["description"]

        q = {"required": p["required"]}

        if p["type"] == "TEXT":
            q["textQuestion"] = {"paragraph": False}
        elif p["type"] == "PARAGRAPH":
            q["textQuestion"] = {"paragraph": True}
        elif p["type"] == "RADIO":
            q["choiceQuestion"] = {
                "type": "RADIO",
                "options": [{"value": o} for o in p["options"]],
                "shuffle": False
            }
        elif p["type"] == "CHECKBOX":
            q["choiceQuestion"] = {
                "type": "CHECKBOX",
                "options": [{"value": o} for o in p["options"]],
                "shuffle": False
            }

        item["questionItem"]["question"] = q

        requests.append({
            "createItem": {
                "item": item,
                "location": {"index": idx}
            }
        })

    forms.forms().batchUpdate(
        formId=form_id,
        body={"requests": requests}
    ).execute()

    log.info(f"Preguntas agregadas al form")

    # 3. Hacer el form accesible para cualquiera con el link
    drive = build("drive", "v3", credentials=creds)
    drive.permissions().create(
        fileId=form_id,
        body={"role": "reader", "type": "anyone"},
        fields="id"
    ).execute()

    responder_uri = f"https://docs.google.com/forms/d/{form_id}/viewform"
    return {"formId": form_id, "responderUri": responder_uri}


# ─── CREAR HOJA STAGING EN MASTER SHEETS ─────────────────────────────────────

def crear_staging_proveedores(creds: Credentials) -> None:
    """Crea la pestaña STAGING_PROVEEDORES en el Master Sheets."""
    sheets = build("sheets", "v4", credentials=creds)

    # Verificar si ya existe
    spreadsheet = sheets.spreadsheets().get(
        spreadsheetId=MASTER_ID
    ).execute()

    hojas_existentes = [s["properties"]["title"] for s in spreadsheet["sheets"]]
    if "STAGING_PROVEEDORES" in hojas_existentes:
        log.info("STAGING_PROVEEDORES ya existe en el Master — se omite creación")
        return

    # Agregar hoja
    sheets.spreadsheets().batchUpdate(
        spreadsheetId=MASTER_ID,
        body={"requests": [{"addSheet": {"properties": {"title": "STAGING_PROVEEDORES"}}}]}
    ).execute()
    log.info("Hoja STAGING_PROVEEDORES creada")

    # Encabezados — cod_proveedor primero, luego el resto en orden de BD_PROV
    encabezados = [[
        "cod_proveedor",      # A — fórmula automática
        "estado",             # B — PENDIENTE / APROBADO / RECHAZADO
        "razon_social",       # C
        "RUC",                # D
        "proveedor_inventario", # E
        "correo",             # F
        "activo",             # G
        "contacto_whatsapp",  # H
        "direccion",          # I
        "contacto_nombre",    # J
        "condicion_pago",     # K
        "dias_credito",       # L
        "lead_time_dias",     # M
        "frecuencia_compra_dias", # N
        "ventana_pedido",     # O
        "observaciones",      # P
        "fecha_envio",        # Q — timestamp del Forms
    ]]

    sheets.spreadsheets().values().update(
        spreadsheetId=MASTER_ID,
        range="STAGING_PROVEEDORES!A1:Q1",
        valueInputOption="USER_ENTERED",
        body={"values": encabezados}
    ).execute()

    # Fórmula cod_proveedor en A2 con ARRAYFORMULA
    # Genera PROV-001, PROV-002, etc. solo si hay razon_social
    formula_cod = [[
        '=ARRAYFORMULA(SI(C2:C="";"";'
        '"PROV-"&TEXTO(FILA(C2:C)-1;"000")))'
    ]]
    sheets.spreadsheets().values().update(
        spreadsheetId=MASTER_ID,
        range="STAGING_PROVEEDORES!A2",
        valueInputOption="USER_ENTERED",
        body={"values": formula_cod}
    ).execute()

    # Formato: encabezado oscuro + columna estado con formato condicional
    sheet_id = next(
        s["properties"]["sheetId"]
        for s in sheets.spreadsheets().get(spreadsheetId=MASTER_ID).execute()["sheets"]
        if s["properties"]["title"] == "STAGING_PROVEEDORES"
    )

    requests_fmt = []

    # Encabezado fila 1
    requests_fmt.append({"repeatCell": {
        "range": {
            "sheetId": sheet_id,
            "startRowIndex": 0, "endRowIndex": 1,
            "startColumnIndex": 0, "endColumnIndex": 17
        },
        "cell": {"userEnteredFormat": {
            "backgroundColor": {"red": 0.11, "green": 0.11, "blue": 0.10},
            "textFormat": {
                "foregroundColor": {"red": 0.95, "green": 0.88, "blue": 0.75},
                "bold": True, "fontSize": 10
            },
            "horizontalAlignment": "CENTER",
        }},
        "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment)"
    }})

    # Freeze fila 1
    requests_fmt.append({"updateSheetProperties": {
        "properties": {
            "sheetId": sheet_id,
            "gridProperties": {"frozenRowCount": 1}
        },
        "fields": "gridProperties.frozenRowCount"
    }})

    # Formato condicional columna B (estado)
    def rango_estado_staging():
        return {
            "sheetId": sheet_id,
            "startRowIndex": 1, "endRowIndex": 500,
            "startColumnIndex": 1, "endColumnIndex": 2
        }

    reglas = [
        ("PENDIENTE", {"red": 0.90, "green": 0.65, "blue": 0.10},
                      {"red": 0.10, "green": 0.07, "blue": 0.0}),
        ("APROBADO",  {"red": 0.20, "green": 0.56, "blue": 0.31},
                      {"red": 1.0,  "green": 1.0,  "blue": 1.0}),
        ("RECHAZADO", {"red": 0.78, "green": 0.18, "blue": 0.18},
                      {"red": 1.0,  "green": 1.0,  "blue": 1.0}),
    ]
    for i, (texto, bg, fg) in enumerate(reglas):
        requests_fmt.append({"addConditionalFormatRule": {
            "rule": {
                "ranges": [rango_estado_staging()],
                "booleanRule": {
                    "condition": {
                        "type": "TEXT_CONTAINS",
                        "values": [{"userEnteredValue": texto}]
                    },
                    "format": {
                        "backgroundColor": bg,
                        "textFormat": {"foregroundColor": fg, "bold": True}
                    }
                }
            },
            "index": i
        }})

    # Validación dropdown en columna B (estado)
    requests_fmt.append({"setDataValidation": {
        "range": {
            "sheetId": sheet_id,
            "startRowIndex": 1, "endRowIndex": 500,
            "startColumnIndex": 1, "endColumnIndex": 2
        },
        "rule": {
            "condition": {
                "type": "ONE_OF_LIST",
                "values": [
                    {"userEnteredValue": "PENDIENTE"},
                    {"userEnteredValue": "APROBADO"},
                    {"userEnteredValue": "RECHAZADO"},
                ]
            },
            "showCustomUi": True,
            "strict": True
        }
    }})

    sheets.spreadsheets().batchUpdate(
        spreadsheetId=MASTER_ID,
        body={"requests": requests_fmt}
    ).execute()

    log.info("STAGING_PROVEEDORES configurado con formato y validaciones")


# ─── CONECTAR FORM A STAGING ──────────────────────────────────────────────────

def conectar_form_a_sheets(creds: Credentials, form_id: str) -> None:
    """Vincula el Form para que las respuestas vayan a STAGING_PROVEEDORES."""
    forms = build("forms", "v1", credentials=creds)

    forms.forms().batchUpdate(
        formId=form_id,
        body={"requests": [{
            "updateSettings": {
                "settings": {
                    "quizSettings": {"isQuiz": False}
                },
                "updateMask": "quizSettings.isQuiz"
            }
        }]}
    ).execute()

    # Nota: la vinculación automática Form→Sheets específico requiere
    # Apps Script o hacerlo manualmente desde el Form (Respuestas → Sheets).
    # El script crea ambos correctamente — el paso de vincularlos es manual (30 seg).
    log.info("Form configurado — vinculación a Sheets: ver instrucciones al final")


# ─── GUARDAR FORM ID EN .ENV ──────────────────────────────────────────────────

def guardar_en_env(form_id: str) -> None:
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(env_path):
        log.warning(f"Agrega manualmente a .env: FORM_PROVEEDOR_ID={form_id}")
        return
    with open(env_path, "r", encoding="utf-8") as f:
        contenido = f.read()
    if "FORM_PROVEEDOR_ID" in contenido:
        log.info("FORM_PROVEEDOR_ID ya existe en .env")
        return
    with open(env_path, "a", encoding="utf-8") as f:
        f.write(f"\nFORM_PROVEEDOR_ID={form_id}\n")
    log.info("FORM_PROVEEDOR_ID guardado en .env")


# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main() -> None:
    creds = get_creds()

    # 1. Crear Form
    result = crear_form(creds)
    form_id       = result["formId"]
    responder_uri = result["responderUri"]

    # 2. Crear staging en Master Sheets
    crear_staging_proveedores(creds)

    # 3. Guardar en .env
    guardar_en_env(form_id)

    print("\n" + "=" * 65)
    print("  OK  Form Proveedor Nuevo creado")
    print(f"  Form ID: {form_id}")
    print(f"  Link para compartir con Jacky / Edu / Mary:")
    print(f"  {responder_uri}")
    print()
    print("  PASO MANUAL — vincular respuestas a STAGING_PROVEEDORES:")
    print("  1. Abre el Form con tu cuenta Google:")
    print(f"     https://docs.google.com/forms/d/{form_id}/edit")
    print("  2. Pestaña 'Respuestas' → ícono Sheets (verde)")
    print("  3. Selecciona 'Seleccionar hoja de cálculo existente'")
    print(f"     → elige el Master Sheets ({MASTER_ID})")
    print("  4. Selecciona la hoja: STAGING_PROVEEDORES")
    print("  Listo — cada respuesta del Form llega directo a staging.")
    print("=" * 65 + "\n")


if __name__ == "__main__":
    main()
