"""Gmail OAuth delivery. No credentials or recovery links reach logs/disk."""
import base64
import json
import logging
from datetime import datetime, timezone
from email.message import EmailMessage
from queue import Empty, Full, Queue
from threading import Event, Lock, Thread
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class GmailRecoveryDelivery:
    """Bounded, volatile queue: dispatch confirms acceptance, not delivery.

    Real and unknown accounts enqueue identically; slow Google I/O never leaks
    account existence through request latency. A provider failure makes the
    adapter globally unavailable until API restart. Pending jobs are not durable.
    """

    def __init__(self, client_id, client_secret, refresh_token, sender, frontend_url):
        self._credentials = dict(client_id=client_id, client_secret=client_secret,
                                 refresh_token=refresh_token, grant_type="refresh_token")
        self._sender = sender
        self._frontend_url = frontend_url.rstrip("/")
        self._queue = Queue(maxsize=100)
        self._closed = Event()
        self._failed = Event()
        self._lock = Lock()
        self._thread = Thread(target=self._run, name="gmail-recovery", daemon=True)
        self._thread.start()

    def dispatch(self, recipient: str | None, token: str, expires: datetime) -> None:
        with self._lock:
            if self._closed.is_set() or self._failed.is_set():
                raise RuntimeError("Servicio de recuperación no disponible")
            try:
                self._queue.put_nowait((recipient, token, expires))
            except Full:
                raise RuntimeError("Servicio de recuperación no disponible") from None

    def close(self):
        with self._lock:
            self._closed.set()
        self._thread.join(timeout=1)

    def _run(self):
        while not self._closed.is_set():
            try:
                job = self._queue.get(timeout=0.2)
            except Empty:
                continue
            try:
                recipient, token, expires = job
                if (not self._failed.is_set() and not self._closed.is_set()
                        and recipient is not None and expires > datetime.now(timezone.utc)):
                    self._send(recipient, token, expires)
            except Exception:
                self._failed.set()
                # Never log the exception: provider responses can carry secrets.
                logging.getLogger(__name__).error(
                    "Gmail no disponible. Revise credenciales/permisos y reinicie la API.")
            finally:
                self._queue.task_done()
                del job
                recipient = token = expires = None
        # Discard plaintext jobs on shutdown; never persist or retry them.
        while True:
            try:
                self._queue.get_nowait()
                self._queue.task_done()
            except Empty:
                break

    @staticmethod
    def _post(url, body, content_type, authorization=None):
        headers = {"Content-Type": content_type}
        if authorization:
            headers["Authorization"] = "Bearer " + authorization
        request = Request(url, data=body, headers=headers, method="POST")
        with build_opener(_NoRedirect()).open(request, timeout=10) as response:
            return json.loads(response.read(1_000_000))

    def _send(self, recipient, token, expires):
        access = self._post("https://oauth2.googleapis.com/token",
                            urlencode(self._credentials).encode(),
                            "application/x-www-form-urlencoded")["access_token"]
        message = EmailMessage()
        message["From"] = self._sender
        message["To"] = recipient
        message["Subject"] = "LiveClothesShop — Recuperar contraseña"
        link = self._frontend_url + "/restablecer-contrasena#token=" + token
        message.set_content(
            "Solicitaste recuperar tu acceso a LiveClothesShop.\n\n"
            "Abre este enlace para elegir una nueva contraseña:\n" + link +
            "\n\nEl enlace es de un solo uso y vence a las " +
            expires.astimezone(timezone.utc).strftime("%H:%M UTC") +
            ".\nSi no lo solicitaste, ignora este correo. No compartas el enlace.\n")
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
        if not self._closed.is_set():
            self._post("https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
                       json.dumps({"raw": raw}).encode(), "application/json", access)


def configured_gmail_delivery(settings):
    if settings.recovery_delivery != "gmail":
        return None
    secrets = [settings.gmail_client_id.get_secret_value(),
               settings.gmail_client_secret.get_secret_value(),
               settings.gmail_refresh_token.get_secret_value()]
    if not all(secrets) or not settings.gmail_sender:
        return None
    return GmailRecoveryDelivery(*secrets, settings.gmail_sender, settings.recovery_frontend_url)
