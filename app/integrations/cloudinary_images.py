"""Signed server-side image uploads. Credentials never leave the backend."""
import hashlib
import json
import time
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import uuid4

from app.core.errors import DomainError

MAX_IMAGE_BYTES = 5 * 1024 * 1024


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def image_type(data: bytes) -> str:
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'image/png'
    if data.startswith(b'\xff\xd8\xff'):
        return 'image/jpeg'
    if len(data) >= 12 and data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return 'image/webp'
    raise DomainError(415, 'imagen_invalida', 'Selecciona una imagen PNG, JPG o WebP.')


def upload_image(settings, data: bytes, content_type: str | None) -> dict[str, str]:
    if not data:
        raise DomainError(422, 'imagen_vacia', 'La imagen está vacía.')
    if len(data) > MAX_IMAGE_BYTES:
        raise DomainError(413, 'imagen_grande', 'La imagen no debe superar los 5 MB.')
    mime = image_type(data)
    if content_type != mime:
        raise DomainError(415, 'imagen_invalida', 'El contenido no corresponde al tipo de imagen indicado.')
    cloud = settings.cloudinary_cloud_name
    key = settings.cloudinary_api_key.get_secret_value()
    secret = settings.cloudinary_api_secret.get_secret_value()
    if not all((cloud, key, secret)):
        raise DomainError(503, 'cloudinary_no_configurado', 'Configura las credenciales de Cloudinary en el backend.')
    public_id = 'liveclothes/prendas/' + uuid4().hex
    fields = {'overwrite': 'false', 'public_id': public_id, 'timestamp': str(int(time.time()))}
    signed = '&'.join(f'{k}={v}' for k, v in sorted(fields.items()))
    fields['signature'] = hashlib.sha256((signed + secret).encode()).hexdigest()
    fields['api_key'] = key
    boundary = uuid4().hex
    chunks = []
    for name, value in fields.items():
        chunks.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    chunks.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="image"\r\nContent-Type: {mime}\r\n\r\n'.encode())
    chunks.extend((data, f'\r\n--{boundary}--\r\n'.encode()))
    request = Request(f'https://api.cloudinary.com/v1_1/{cloud}/image/upload', data=b''.join(chunks),
                      headers={'Content-Type': f'multipart/form-data; boundary={boundary}'}, method='POST')
    try:
        with build_opener(NoRedirect()).open(request, timeout=30) as response:
            raw = response.read(65537)
        if len(raw) > 65536:
            raise ValueError
        result = json.loads(raw)
        url = result['secure_url']
        parsed = urlsplit(url)
        if (parsed.scheme != 'https' or parsed.netloc != 'res.cloudinary.com'
                or not parsed.path.startswith(f'/{cloud}/image/upload/') or len(url) > 255
                or result.get('public_id') != public_id or result.get('format') not in {'jpg', 'jpeg', 'png', 'webp'}):
            raise ValueError
        return {'url': url, 'publicId': public_id}
    except Exception:
        raise DomainError(502, 'cloudinary_no_disponible', 'No se pudo subir la imagen a Cloudinary. Revisa las credenciales o inténtalo nuevamente.') from None
