import os

from dotenv import load_dotenv
from googleapiclient.discovery import build
from google_credentials import google_credentials

load_dotenv(override=True)

creds = google_credentials(["https://www.googleapis.com/auth/drive"])
service = build("drive", "v3", credentials=creds)
folder_id = os.getenv("GOOGLE_DRIVE_FACTURAS_FOLDER_ID")

if not folder_id:
    raise SystemExit("Falta GOOGLE_DRIVE_FACTURAS_FOLDER_ID en .env")

archivos: list[dict] = []
page_token = None
while True:
    kwargs: dict = {
        "q": f"'{folder_id}' in parents and trashed=false",
        "fields": "nextPageToken, files(id,name,mimeType,createdTime)",
        "pageSize": 1000,
    }
    if page_token:
        kwargs["pageToken"] = page_token
    results = service.files().list(**kwargs).execute()
    archivos.extend(results.get("files", []))
    page_token = results.get("nextPageToken")
    if not page_token:
        break

print(f"Archivos en carpeta: {len(archivos)}")
for f in archivos:
    tipo = f["mimeType"].split("/")[-1].ljust(15)
    print(f"  {tipo} | {f['id']} | {f['name']}")
