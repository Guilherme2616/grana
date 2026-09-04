import io
import os
import re
import unicodedata

import requests
from flask import current_app
from google.auth.transport.requests import Request
from google.oauth2 import service_account


DRIVE_API = "https://www.googleapis.com/drive/v3"
FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"
MONTH_NAMES = (
    "JANEIRO", "FEVEREIRO", "MARCO", "ABRIL", "MAIO", "JUNHO",
    "JULHO", "AGOSTO", "SETEMBRO", "OUTUBRO", "NOVEMBRO", "DEZEMBRO",
)


class DriveConfigurationError(RuntimeError):
    pass


class DriveAccessError(RuntimeError):
    pass


def normalize_folder_name(value):
    normalized = unicodedata.normalize("NFKD", value or "")
    return re.sub(r"[^A-Z0-9]+", " ", normalized.encode("ascii", "ignore").decode().upper()).strip()


def month_folder_matches(folder_name, month_number):
    normalized = normalize_folder_name(folder_name)
    month_name = MONTH_NAMES[month_number - 1]
    parts = normalized.split()
    return normalized == month_name or (
        month_name in parts and (str(month_number) in parts or f"{month_number:02d}" in parts)
    )


def drive_is_configured():
    credential_path = current_app.config.get("GOOGLE_SERVICE_ACCOUNT_FILE", "")
    folder_id = current_app.config.get("GOOGLE_DRIVE_ROOT_FOLDER_ID", "")
    return bool(credential_path and folder_id and os.path.isfile(credential_path))


def _authorized_session():
    credential_path = current_app.config.get("GOOGLE_SERVICE_ACCOUNT_FILE", "")
    if not credential_path or not os.path.isfile(credential_path):
        raise DriveConfigurationError("O arquivo de credenciais do Google Drive não foi configurado.")
    try:
        credentials = service_account.Credentials.from_service_account_file(
            credential_path,
            scopes=["https://www.googleapis.com/auth/drive.readonly"],
        )
        credentials.refresh(Request())
    except Exception as exc:
        raise DriveConfigurationError("Não foi possível autenticar a conta de serviço do Google.") from exc

    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {credentials.token}"})
    return session


def _list_children(session, parent_id, mime_type=None):
    conditions = [f"'{parent_id}' in parents", "trashed = false"]
    if mime_type:
        conditions.append(f"mimeType = '{mime_type}'")
    params = {
        "q": " and ".join(conditions),
        "fields": "nextPageToken,files(id,name,mimeType,modifiedTime,size)",
        "pageSize": 100,
        "orderBy": "name",
    }
    files = []
    while True:
        response = session.get(f"{DRIVE_API}/files", params=params, timeout=30)
        if not response.ok:
            raise DriveAccessError("O Google Drive recusou a consulta à pasta configurada.")
        payload = response.json()
        files.extend(payload.get("files", []))
        token = payload.get("nextPageToken")
        if not token:
            break
        params["pageToken"] = token
    return files


def _find_folder(session, parent_id, predicate, missing_message):
    folders = _list_children(session, parent_id, FOLDER_MIME_TYPE)
    matches = [item for item in folders if predicate(item["name"])]
    if not matches:
        raise DriveAccessError(missing_message)
    if len(matches) > 1:
        raise DriveAccessError("Há mais de uma pasta correspondente. Mantenha apenas uma.")
    return matches[0]


def list_month_pdfs(reference_month):
    year_text, month_text = reference_month.split("-")
    month_number = int(month_text)
    root_id = current_app.config.get("GOOGLE_DRIVE_ROOT_FOLDER_ID", "")
    if not root_id:
        raise DriveConfigurationError("A pasta principal do Google Drive não foi configurada.")

    session = _authorized_session()
    year_folder = _find_folder(
        session,
        root_id,
        lambda name: normalize_folder_name(name) == year_text,
        f"Não encontrei a pasta {year_text} no Google Drive.",
    )
    month_folder = _find_folder(
        session,
        year_folder["id"],
        lambda name: month_folder_matches(name, month_number),
        f"Não encontrei a pasta {MONTH_NAMES[month_number - 1]} dentro de {year_text}.",
    )
    pdfs = _list_children(session, month_folder["id"], "application/pdf")
    return session, pdfs


def list_dividend_pdfs():
    root_id = current_app.config.get("GOOGLE_DRIVE_ROOT_FOLDER_ID", "")
    if not root_id:
        raise DriveConfigurationError("A pasta principal do Google Drive não foi configurada.")

    session = _authorized_session()
    dividend_folder = _find_folder(
        session,
        root_id,
        lambda name: any(part.startswith("PROVENTO") for part in normalize_folder_name(name).split()),
        "Não encontrei a pasta de Proventos dentro de Faturas Grana.",
    )
    pdfs = _list_children(session, dividend_folder["id"], "application/pdf")
    return session, pdfs


def download_pdf(session, file_id):
    response = session.get(f"{DRIVE_API}/files/{file_id}", params={"alt": "media"}, timeout=60)
    if not response.ok:
        raise DriveAccessError("Não foi possível baixar um dos PDFs do Google Drive.")
    maximum = current_app.config.get("MAX_CONTENT_LENGTH", 10 * 1024 * 1024)
    if len(response.content) > maximum:
        raise DriveAccessError("O PDF ultrapassa o limite de 10 MB.")
    return io.BytesIO(response.content)
