"""Respaldos del esquema público en archivos privados, sin tablas adicionales."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
from uuid import uuid4

from sqlalchemy.engine import make_url

from app.core.errors import DomainError
from app.core.security import utcnow
from app.modules.seguridad_accesos.repositories.bitacora import add
from app.modules.saas_suscripciones.suscripciones.services.plataforma import _peer


def audit_backup(db, usuario, accion, job_id, peer):
    add(db, action=accion, user_id=usuario, ip=_peer(peer),
        details={'resultado': 'exito', 'respaldoId': job_id, 'esquema': 'public'})


_manager_lock = threading.Lock()


def backup_manager(request):
    with _manager_lock:
        if not hasattr(request.app.state, 'backups'):
            request.app.state.backups = BackupManager(request.app.state.settings)
        return request.app.state.backups


class BackupManager:
    def __init__(self, settings):
        self.settings = settings
        self.directory = Path(settings.backups_directory).expanduser().resolve()
        configured = settings.backups_pg_dump or shutil.which('pg_dump')
        if not configured and os.name == 'nt':
            versions = Path(os.environ.get('ProgramFiles', r'C:\Program Files')) / 'PostgreSQL'
            candidates = list(versions.glob('*/bin/pg_dump.exe'))
            if candidates:
                configured = str(max(candidates, key=lambda p: int(p.parents[1].name) if p.parents[1].name.isdigit() else 0))
        self.dump = Path(configured).resolve() if configured else None
        self.restore = self.dump.with_name('pg_restore.exe' if os.name == 'nt' else 'pg_restore') if self.dump else None

    def _path(self, job_id, suffix):
        if not re.fullmatch(r'[0-9a-f]{32}', job_id):
            raise DomainError(404, 'respaldo_no_encontrado', 'El respaldo no existe')
        path = self.directory / (job_id + suffix)
        if path.is_symlink() or path.resolve().parent != self.directory:
            raise DomainError(404, 'respaldo_no_encontrado', 'El respaldo no existe')
        return path

    def _save(self, job):
        temp = self._path(job['id'], '.json.tmp')
        temp.write_text(json.dumps(job, ensure_ascii=False), encoding='utf-8')
        temp.replace(self._path(job['id'], '.json'))

    def _load(self, job_id):
        try:
            job = json.loads(self._path(job_id, '.json').read_text(encoding='utf-8'))
            if job['id'] != job_id:
                raise ValueError
            return job
        except (OSError, ValueError, KeyError):
            raise DomainError(404, 'respaldo_no_encontrado', 'El respaldo no existe') from None

    def _lease(self):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        handle = (self.directory / '.lock').open('a+b')
        try:
            if os.name == 'nt':
                import msvcrt
                if handle.seek(0, 2) == 0:
                    handle.write(b'0'); handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return handle
        except OSError:
            handle.close()
            raise DomainError(409, 'respaldo_en_proceso', 'Ya hay un respaldo en proceso. Espera a que termine.') from None

    def _recover(self):
        # Only called under the interprocess lock. No running dump owns it.
        for path in self.directory.glob('*.json'):
            try:
                job = self._load(path.stem)
                if job['estado'] in ('solicitado', 'en_proceso'):
                    job.update(estado='fallido', completado=utcnow().isoformat(), error='El proceso fue interrumpido. Puedes solicitar otro respaldo.')
                    self._save(job)
                    self._path(job['id'], '.part').unlink(missing_ok=True)
            except (DomainError, KeyError):
                continue

    def list(self):
        try:
            lease = self._lease()
        except DomainError:
            lease = None
        if lease:
            try:
                self._recover()
            finally:
                lease.close()
        jobs = []
        for path in self.directory.glob('*.json'):
            try:
                job = self._load(path.stem)
                jobs.append({key: job.get(key) for key in ('id','estado','solicitado','completado','tamanoBytes','error')})
            except DomainError:
                continue
        ready = bool(self.dump and self.dump.is_file() and self.restore and self.restore.is_file())
        return {'configuracion': {'configurado': ready, 'estado': 'disponible' if ready else 'pendiente',
                'mensaje': None if ready else 'Instala las herramientas PostgreSQL o configura BACKUPS_PG_DUMP.',
                'destino': 'Almacenamiento privado del servidor · esquema public', 'retencionDias': 0},
                'respaldos': sorted(jobs, key=lambda j: j['solicitado'], reverse=True)}

    def prepare(self):
        if not self.dump or not self.dump.is_file() or not self.restore or not self.restore.is_file():
            raise DomainError(503, 'respaldos_no_configurados', 'No están disponibles pg_dump y pg_restore en el servidor.')
        lease = self._lease()
        try:
            self._recover()
            job = {'id': uuid4().hex, 'estado': 'solicitado', 'solicitado': utcnow().isoformat(),
                   'completado': None, 'tamanoBytes': None, 'error': None}
            self._save(job)
            return job, lease
        except Exception:
            lease.close()
            raise

    def abort(self, job, lease):
        try:
            job.update(estado='fallido', completado=utcnow().isoformat(), error='No se pudo registrar la solicitud.')
            self._save(job)
        finally:
            lease.close()

    def _environment(self):
        value = self.settings.platform_database_url
        if value is None and self.settings.platform_use_database_url:
            value = self.settings.database_url
        if value is None:
            raise ValueError('Conexión de plataforma no configurada')
        url = make_url(value.get_secret_value())
        env = {k: v for k, v in os.environ.items() if not k.startswith('PG')}
        env.update(PGHOST=url.host or '', PGPORT=str(url.port or 5432), PGUSER=url.username or '',
                   PGPASSWORD=url.password or '', PGDATABASE=url.database or '',
                   PGSSLMODE=str(url.query.get('sslmode', 'require')), PGCONNECT_TIMEOUT='15',
                   PGOPTIONS='-c default_transaction_read_only=on', PGAPPNAME='FashionStore-backup')
        return env

    def _run_command(self, args, env):
        result = subprocess.run(args, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE, timeout=self.settings.backups_timeout_seconds,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        if result.returncode:
            # Never persist or return stderr: it may contain infrastructure details.
            raise RuntimeError('Herramienta PostgreSQL rechazada')

    @staticmethod
    def _digest(path):
        with path.open('rb') as stream:
            return hashlib.file_digest(stream, 'sha256').hexdigest()

    def run(self, job_id, lease):
        job = self._load(job_id)
        part = self._path(job_id, '.part')
        try:
            job['estado'] = 'en_proceso'; self._save(job)
            env = self._environment()
            self._run_command([str(self.dump), '--format=custom', '--no-owner', '--no-acl',
                               '--schema=public', '--file='+str(part)], env)
            with part.open('rb') as stream:
                if stream.read(5) != b'PGDMP':
                    raise ValueError('Formato inválido')
            self._run_command([str(self.restore), '--list', str(part)], env)
            checksum = self._digest(part)
            path = self._path(job_id, '.dump'); part.replace(path)
            job.update(estado='completado', completado=utcnow().isoformat(), tamanoBytes=path.stat().st_size,
                       sha256=checksum, error=None)
        except Exception:
            part.unlink(missing_ok=True)
            job.update(estado='fallido', completado=utcnow().isoformat(), error='No se pudo generar o validar el respaldo. Revisa la conexión, los permisos y la versión de PostgreSQL del servidor.')
        finally:
            try:
                self._save(job)
            finally:
                lease.close()

    def download(self, job_id):
        job = self._load(job_id)
        path = self._path(job_id, '.dump')
        if job['estado'] != 'completado' or not path.is_file():
            raise DomainError(409, 'respaldo_no_disponible', 'El respaldo no está disponible para descargar.')
        if path.stat().st_size != job['tamanoBytes'] or self._digest(path) != job.get('sha256'):
            raise DomainError(409, 'respaldo_alterado', 'El archivo no superó la verificación de integridad.')
        return path
