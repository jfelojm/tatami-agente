# Fase 1 — Agente WhatsApp 24/7 en la nube (Railway)

Guía paso a paso. **Tú solo creas la cuenta y pegas valores**; el código ya está listo.

---

## Qué logramos al terminar

- WhatsApp responde **aunque apagues tu PC**
- Dashboards en `https://TU-URL.railway.app/dashboard?token=...`
- **Ya no necesitas ngrok** en tu laptop

---

## Paso 1 — Cuenta Railway (5 min)

1. Abre [https://railway.com](https://railway.com)
2. **Sign up** con GitHub o email
3. Si pide tarjeta: plan Hobby ~**$5/mes** (hay crédito inicial; sin sorpresas si pones límite de gasto)

---

## Paso 2 — Subir el código a GitHub (una vez)

Si el proyecto **ya está en GitHub** (`origin`), salta al paso 3.

Si no:

1. Crea repo privado en [https://github.com/new](https://github.com/new) → nombre ej. `tatami-agente`
2. En PowerShell (en la carpeta `tatami-agente`):

```powershell
cd "C:\Users\Usuario\Desktop\Agente Tatami\tatami-agente"
git add Dockerfile requirements-webhook.txt google_credentials.py deploy/
git commit -m "Fase 1: deploy webhook WhatsApp en nube"
git remote add origin https://github.com/jfelojm/tatami-agente.git
git push -u origin master
```

(Si ya tienes `origin`, solo `git push`.)

---

## Paso 3 — Crear servicio en Railway (10 min)

1. Railway → **New Project** → **Deploy from GitHub repo**
2. Elige el repo `tatami-agente`
3. Railway detecta el **Dockerfile** y empieza a construir
4. Entra al servicio → **Settings** → **Networking** → **Generate Domain**  
   Obtienes algo como: `https://tatami-agente-production-xxxx.up.railway.app`
5. Anota esa URL — la llamaremos **URL_NUBE**

---

## Paso 4 — Variables de entorno (15 min)

En Railway → tu servicio → **Variables** → **Raw Editor**

Copia los valores desde tu `.env` local. Lista mínima:

| Variable | Dónde está hoy |
|----------|----------------|
| `SUPABASE_URL` | .env |
| `SUPABASE_KEY` | .env |
| `SPREADSHEET_ID` | .env |
| `ANTHROPIC_API_KEY` | .env |
| `WHATSAPP_VERIFY_TOKEN` | .env |
| `WHATSAPP_ACCESS_TOKEN` | .env |
| `WHATSAPP_PHONE_NUMBER_ID` | .env |
| `WHATSAPP_APP_SECRET` | .env |
| `DASHBOARD_TOKEN` | .env |
| `GOOGLE_DRIVE_FACTURAS_FOLDER_ID` | .env |
| `ALLOWLIST_SOCIO` | .env |
| `ALLOWLIST_CONSULTA` | .env |
| `ALLOWLIST_OPERATIVO` | .env |
| `ALERTA_WA_FELIPE` | .env |
| `ALERTA_WA_MOISES` | .env |
| `FACTURA_SESSION_BACKEND` | `supabase` |
| `CONTEO_SHEETS_INGEST_SECRET` | .env |
| `RECONCILIAR_TOL_ABS` | `0.05` |

### Google credentials (importante)

En la nube **no** subimos el archivo `.json`. En su lugar:

**Opción fácil (PowerShell):**

```powershell
cd "C:\Users\Usuario\Desktop\Agente Tatami\tatami-agente"
.\deploy\copiar_google_json_railway.ps1
```

Eso copia el JSON al portapapeles. En Railway → Variables → **`GOOGLE_CREDENTIALS_JSON`** → pegar → Save.

**Opción manual:** abrir `credentials/google_service_account.json`, copiar todo, pegar en Railway.

---

## Paso 5 — Probar que la nube responde (2 min)

En el navegador:

```
https://URL_NUBE/
```

Debe mostrar JSON: `{"status":"ok","agente":"Tatami Bao Bar v4",...}`

Dashboard:

```
https://URL_NUBE/dashboard?token=TU_DASHBOARD_TOKEN
```

---

## Paso 6 — Cambiar webhook en Meta (5 min)

1. [Meta for Developers](https://developers.facebook.com/) → tu app WhatsApp
2. **WhatsApp** → **Configuration** → **Webhook**
3. **Callback URL:** `https://URL_NUBE/webhook`
4. **Verify token:** el mismo `WHATSAPP_VERIFY_TOKEN` de tu .env
5. **Verify and save**
6. Suscríbete al campo **messages** (si no está)

---

## Paso 7 — Apagar ngrok en tu PC (cuando confirmes)

1. Envía un WhatsApp de prueba al agente
2. Si responde bien desde la nube, **deja de correr** uvicorn + ngrok en tu laptop
3. Opcional: desactiva la tarea programada que lanza `ejecutar_servidor_webhook.ps1`

**No borres** el `.env` local; lo usaremos en Fase 2 (servidor Tatami).

---

## Problemas frecuentes

| Síntoma | Qué revisar |
|---------|-------------|
| Build falla | Logs en Railway → Deployments |
| `/` no responde | Variables faltantes; ver Logs |
| Meta no verifica webhook | URL exacta `/webhook` + token igual al .env |
| Agente no responde WA | `WHATSAPP_ACCESS_TOKEN` vigente; webhook suscrito a messages |
| Error Google Sheets | `GOOGLE_CREDENTIALS_JSON` completo; `SPREADSHEET_ID` correcto |

---

## Siguiente: Fase 2

Cuando Fase 1 esté estable, movemos al **servidor Windows Tatami** (AnyDesk):

- Pipeline diario  
- Facturas SRI  
- Ventas Smart Menu  

El WhatsApp **se queda en Railway**; el servidor solo alimenta datos.

---

## Checklist

- [ ] Cuenta Railway creada  
- [ ] Repo en GitHub con Dockerfile  
- [ ] Servicio desplegado + dominio generado  
- [ ] Variables pegadas (incl. `GOOGLE_CREDENTIALS_JSON`)  
- [ ] `https://URL_NUBE/` responde ok  
- [ ] Meta webhook apunta a URL_NUBE  
- [ ] Prueba WhatsApp OK  
- [ ] ngrok apagado en PC personal  

Cuando tengas la **URL_NUBE**, compártela y validamos juntos el webhook Meta.
