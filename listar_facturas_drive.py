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

results = (
    service.files()
    .list(
        q=f"'{folder_id}' in parents and trashed=false",
        fields="files(id,name,mimeType,createdTime)",
    )
    .execute()
)

archivos = results.get("files", [])
print(f"Archivos en carpeta: {len(archivos)}")
for f in archivos:
    tipo = f["mimeType"].split("/")[-1].ljust(15)
    print(f"  {tipo} | {f['id']} | {f['name']}")
