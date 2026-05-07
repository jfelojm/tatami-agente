"""
Lista XML en la carpeta de facturas (Drive) y muestra los primeros caracteres del primero.
"""

import os

from dotenv import load_dotenv
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

load_dotenv(override=True)

creds = Credentials.from_service_account_file(
    os.getenv("GOOGLE_CREDENTIALS_PATH"),
    scopes=["https://www.googleapis.com/auth/drive"],
)
service = build("drive", "v3", credentials=creds)
folder_id = os.getenv("GOOGLE_DRIVE_FACTURAS_FOLDER_ID")

if not folder_id:
    raise SystemExit("Falta GOOGLE_DRIVE_FACTURAS_FOLDER_ID en .env")

# Drive suele usar text/xml o application/xml
q = (
    f"'{folder_id}' in parents and trashed=false "
    "and (mimeType='text/xml' or mimeType='application/xml')"
)
results = service.files().list(q=q, fields="files(id,name)").execute()

xmls = results.get("files", [])
print(f"XMLs encontrados: {len(xmls)}")
for i, f in enumerate(xmls):
    print(f"  [{i}] {f['name']} | {f['id']}")

if xmls:
    primer_xml = xmls[0]
    print(f"\nDescargando: {primer_xml['name']}...")
    content = service.files().get_media(fileId=primer_xml["id"]).execute()
    texto = content.decode("utf-8", errors="replace")
    print("\n--- PRIMEROS 2000 CARACTERES ---")
    print(texto[:2000])
