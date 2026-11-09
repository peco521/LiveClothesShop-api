from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy.engine import make_url

from app.core.errors import DomainError


_LOG = logging.getLogger(__name__)
_PENDING_MESSAGE = (
    "Configuración pendiente: se requiere pg_dump y una identidad de respaldo "
    "de solo lectura válida para la base local de pruebas."
)


class PlatformBackupManager:
    def __init__(self, settings):
        self._settings = settings
        self._database_url = settings.saas_backup_database_url
        self._dump_path = self._find_pg_dump(settings.saas_backup_pg_dump_path)
        storage_root = os.getenv("LOCALAPPDATA") if os.name == "nt" else os.getenv("XDG_STATE_HOME")
        if not storage_root:
            storage_root = str(
                Path.home() / "AppData" / "Local" if os.name == "nt"
                else Path.home() / ".local" / "state"
            )
        self._directory = Path(storage_root) / "FashionStore" / "saas_backups"
        self._guard = threading.RLock()
        self._locks: dict[str, object] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._process: subprocess.Popen | None = None
        self._active_id: str | None = None
        self._closed = False

    @staticmethod
    def _find_pg_dump(configured: str) -> str | None:
        if configured:
            path = Path(configured).expanduser().resolve()
            return str(path) if path.is_file() and path.name.lower() in {"pg_dump", "pg_dump.exe"} else None
        return shutil.which("pg_dump")

    def configuration(self) -> dict:
        ready = (
            self._database_url is not None
            and self._dump_path is not None
            and self._verify_backup_identity()
        )
        return {
            "configurado": ready,
            "estado": "listo" if ready else "pendiente_configuracion",
            "mensaje": None if ready else _PENDING_MESSAGE,
            "destino": "127.0.0.1:55432/fashionstore_test",
            "retencionDias": self._settings.saas_backup_retention_days,
        }

    def _verify_backup_identity(self) -> bool:
        if self._database_url is None:
            return False
        parsed = make_url(self._database_url.get_secret_value())
        try:
            import psycopg
        except ImportError:
            return False
        try:
            with psycopg.connect(
                host=parsed.host,
                port=parsed.port,
                dbname=parsed.database,
                user=parsed.username,
                password=parsed.password,
                connect_timeout=3,
            ) as connection:
                row = connection.execute("""
                    SELECT current_database(), current_user, inet_server_port(),
                           r.rolcanlogin AND NOT r.rolsuper AND NOT r.rolcreatedb
                             AND NOT r.rolcreaterole AND r.rolbypassrls AND NOT r.rolinherit,
                           NOT EXISTS (
                             SELECT 1 FROM pg_auth_members m WHERE m.member = r.oid
                           ),
                           NOT EXISTS (
                             SELECT 1 FROM pg_class c WHERE c.relowner = r.oid
                           ),
                           NOT EXISTS (
                             SELECT 1 FROM pg_namespace n WHERE n.nspowner = r.oid
                           ),
                           NOT EXISTS (
                             SELECT 1 FROM pg_database d WHERE d.datdba = r.oid
                           ),
                           NOT has_database_privilege(current_user, current_database(), 'CREATE'),
                           NOT has_schema_privilege(current_user, 'public', 'CREATE'),
                           NOT EXISTS (
                             SELECT 1
                             FROM pg_class c
                             JOIN pg_namespace n ON n.oid = c.relnamespace
                             WHERE n.nspname = 'public'
                               AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
                               AND (
                                 NOT has_table_privilege(current_user, c.oid, 'SELECT')
                                 OR has_table_privilege(current_user, c.oid, 'INSERT')
                                 OR has_table_privilege(current_user, c.oid, 'UPDATE')
                                 OR has_table_privilege(current_user, c.oid, 'DELETE')
                                 OR has_table_privilege(current_user, c.oid, 'TRUNCATE')
                                 OR has_table_privilege(current_user, c.oid, 'REFERENCES')
                                 OR has_table_privilege(current_user, c.oid, 'TRIGGER')
                               )
                           ),
                           NOT EXISTS (
                             SELECT 1
                             FROM pg_class c
                             JOIN pg_namespace n ON n.oid = c.relnamespace
                             WHERE n.nspname = 'public' AND c.relkind = 'S'
                               AND (
                                 NOT has_sequence_privilege(current_user, c.oid, 'SELECT')
                                 OR has_sequence_privilege(current_user, c.oid, 'USAGE')
                                 OR has_sequence_privilege(current_user, c.oid, 'UPDATE')
                               )
                           )
                           ,
                           NOT EXISTS (
                             SELECT 1
                             FROM pg_proc p
                             JOIN pg_namespace n ON n.oid = p.pronamespace
                             WHERE n.nspname = 'public' AND p.prosecdef
                               AND has_function_privilege(current_user, p.oid, 'EXECUTE')
                           )
                    FROM pg_roles r
                    WHERE r.rolname = current_user
                """).fetchone()
        except psycopg.Error:
            _LOG.info("No se pudo validar la identidad local de respaldos")
            return False
        return bool(
            row
            and row[0] == "fashionstore_test"
            and row[1] == "fashionstore_backup"
            and row[2] == 55432
            and all(row[3:])
        )

    def _ensure_storage(self) -> None:
        try:
            self._directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            if self._directory.is_symlink() or not self._directory.is_dir():
                raise OSError("invalid_storage_directory")
            if os.name != "nt":
                os.chmod(self._directory, 0o700)
        except OSError:
            raise DomainError(
                503, "almacenamiento_no_disponible",
                "No se pudo asegurar el almacenamiento local de respaldos",
            ) from None

    @staticmethod
    def _validate_id(backup_id: str) -> str:
        try:
            parsed = uuid.UUID(backup_id)
        except (ValueError, AttributeError):
            raise DomainError(404, "respaldo_no_encontrado", "El respaldo no existe") from None
        if str(parsed) != backup_id:
            raise DomainError(404, "respaldo_no_encontrado", "El respaldo no existe")
        return str(parsed)

    def _job_path(self, backup_id: str) -> Path:
        return self._directory / f"{self._validate_id(backup_id)}.json"

    def _dump_file(self, backup_id: str) -> Path:
        return self._directory / f"{self._validate_id(backup_id)}.dump"

    def _read_job(self, backup_id: str) -> dict:
        path = self._job_path(backup_id)
        try:
            if path.is_symlink() or path.resolve(strict=True).parent != self._directory.resolve():
                raise OSError("invalid_job_path")
            value = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise DomainError(404, "respaldo_no_encontrado", "El respaldo no existe") from None
        except (OSError, ValueError):
            raise DomainError(503, "historial_no_disponible",
                              "No se pudo leer el historial de respaldos") from None
        if not isinstance(value, dict) or value.get("id") != backup_id:
            raise DomainError(503, "historial_no_disponible",
                              "No se pudo leer el historial de respaldos")
        return value

    def _write_job(self, value: dict) -> None:
        self._directory.mkdir(parents=True, exist_ok=True)
        destination = self._job_path(value["id"])
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self._directory,
                prefix=f"{value['id']}-", suffix=".tmp", delete=False,
            ) as file:
                temporary = Path(file.name)
                json.dump(value, file, ensure_ascii=False)
            os.replace(temporary, destination)
        except OSError:
            if temporary is not None:
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass
            raise DomainError(503, "historial_no_disponible",
                              "No se pudo guardar el historial de respaldos") from None

    def list_jobs(self) -> list[dict]:
        if not self._directory.exists():
            if self._directory.is_symlink():
                raise DomainError(503, "almacenamiento_no_disponible",
                                  "No se pudo leer el almacenamiento local de respaldos")
            return []
        self._ensure_storage()
        try:
            paths = sorted(self._directory.glob("*.json"), reverse=True)
        except OSError:
            raise DomainError(503, "historial_no_disponible",
                              "No se pudo leer el historial de respaldos") from None
        jobs = []
        for path in paths:
            if not self._is_uuid(path.stem):
                continue
            jobs.append(self._read_job(path.stem))
        return sorted(jobs, key=lambda item: item.get("solicitado", ""), reverse=True)

    @staticmethod
    def _is_uuid(value: str) -> bool:
        try:
            return str(uuid.UUID(value)) == value
        except ValueError:
            return False

    def _acquire_lock(self):
        self._directory.mkdir(parents=True, exist_ok=True)
        handle = (self._directory / "backup.lock").open("a+b")
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return None
        return handle

    @staticmethod
    def _release_lock(handle) -> None:
        if handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def _cleanup_expired(self) -> None:
        cutoff = datetime.now(timezone.utc) - timedelta(
            days=self._settings.saas_backup_retention_days)
        for job in self.list_jobs():
            if job.get("estado") in {"solicitado", "en_proceso"}:
                continue
            try:
                requested = datetime.fromisoformat(job["solicitado"])
            except (KeyError, TypeError, ValueError):
                continue
            if requested.tzinfo is None:
                requested = requested.replace(tzinfo=timezone.utc)
            if requested >= cutoff:
                continue
            dump = self._dump_file(job["id"])
            metadata = self._job_path(job["id"])
            for path in (dump, metadata):
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass

    def _reconcile_interrupted_jobs(self) -> None:
        for job in self.list_jobs():
            if job.get("estado") not in {"solicitado", "en_proceso"}:
                continue
            try:
                self._dump_file(job["id"]).unlink()
            except FileNotFoundError:
                pass
            self._update(
                job["id"],
                estado="fallido",
                completado=datetime.now(timezone.utc).isoformat(),
                tamanoBytes=None,
                error="El servicio se reinició antes de completar el respaldo.",
            )

    def _existing_dump_bytes(self) -> int:
        return sum(
            self._dump_file(job["id"]).stat().st_size
            for job in self.list_jobs()
            if job.get("estado") == "completado" and self._dump_file(job["id"]).is_file()
        )

    def prepare_job(self) -> dict:
        if not self.configuration()["configurado"]:
            raise DomainError(503, "respaldo_no_configurado", _PENDING_MESSAGE)
        with self._guard:
            if self._closed:
                raise DomainError(503, "respaldo_no_disponible",
                                  "El servicio de respaldos no está disponible")
            self._ensure_storage()
            lock_handle = self._acquire_lock()
            if lock_handle is None:
                raise DomainError(409, "respaldo_en_proceso",
                                  "Ya hay un respaldo de plataforma en proceso")
            try:
                self._reconcile_interrupted_jobs()
                self._cleanup_expired()
                now = datetime.now(timezone.utc)
                jobs = self.list_jobs()
                minimum = timedelta(
                    minutes=self._settings.saas_backup_min_interval_minutes)
                if jobs and minimum.total_seconds() and now - datetime.fromisoformat(
                    jobs[0]["solicitado"]) < minimum:
                    raise DomainError(429, "limite_de_frecuencia",
                                      "Espera antes de solicitar otro respaldo")
                limit = self._settings.saas_backup_max_storage_mb * 1024 * 1024
                if self._existing_dump_bytes() >= limit:
                    raise DomainError(507, "limite_de_almacenamiento",
                                      "Se alcanzó el límite local de almacenamiento de respaldos")
                backup_id = str(uuid.uuid4())
                job = {
                    "id": backup_id,
                    "estado": "solicitado",
                    "solicitado": now.isoformat(),
                    "iniciado": None,
                    "completado": None,
                    "tamanoBytes": None,
                    "error": None,
                }
                self._write_job(job)
                self._locks[backup_id] = lock_handle
                self._active_id = backup_id
                return job
            except Exception:
                if lock_handle is not self._locks.get(self._active_id):
                    self._release_lock(lock_handle)
                raise

    def start_job(self, backup_id: str) -> dict:
        backup_id = self._validate_id(backup_id)
        with self._guard:
            if self._active_id != backup_id or backup_id not in self._locks:
                raise DomainError(409, "respaldo_no_solicitado",
                                  "No se pudo iniciar el respaldo solicitado")
            thread = threading.Thread(
                target=self._run, args=(backup_id,), name=f"saas-backup-{backup_id[:8]}",
                daemon=True,
            )
            self._threads[backup_id] = thread
            thread.start()
            return self._read_job(backup_id)

    def abort_job(self, backup_id: str) -> None:
        backup_id = self._validate_id(backup_id)
        with self._guard:
            job = self._read_job(backup_id)
            job["estado"] = "fallido"
            job["completado"] = datetime.now(timezone.utc).isoformat()
            job["error"] = "La solicitud no pudo registrarse en auditoría."
            self._write_job(job)
            self._release_lock(self._locks.pop(backup_id, None))
            self._active_id = None

    def _update(self, backup_id: str, **changes) -> dict:
        job = self._read_job(backup_id)
        job.update(changes)
        self._write_job(job)
        return job

    def _run(self, backup_id: str) -> None:
        output = self._dump_file(backup_id)
        try:
            self._update(
                backup_id, estado="en_proceso",
                iniciado=datetime.now(timezone.utc).isoformat(),
            )
            if self._closed:
                raise RuntimeError("service_shutdown")
            parsed = make_url(self._database_url.get_secret_value())
            if (parsed.host, parsed.port, parsed.database, parsed.username) != (
                "127.0.0.1", 55432, "fashionstore_test", "fashionstore_backup"
            ):
                raise RuntimeError("invalid_backup_target")
            if not self._verify_backup_identity():
                raise RuntimeError("backup_identity_unverified")
            if output.exists() or output.is_symlink():
                raise RuntimeError("backup_output_already_exists")
            if shutil.disk_usage(self._directory).free < 256 * 1024 * 1024:
                raise RuntimeError("insufficient_disk_space")
            environment = os.environ.copy()
            environment.update({
                "PGHOST": parsed.host,
                "PGPORT": str(parsed.port),
                "PGDATABASE": parsed.database,
                "PGUSER": parsed.username,
                "PGPASSWORD": parsed.password or "",
                "PGCONNECT_TIMEOUT": "10",
            })
            command = [
                self._dump_path, "--format=custom", "--no-owner", "--no-privileges",
                "--no-password", "--file", str(output),
            ]
            process = subprocess.Popen(
                command,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                shell=False,
                **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}),
            )
            with self._guard:
                self._process = process
            limit = self._settings.saas_backup_max_storage_mb * 1024 * 1024
            timeout = self._settings.saas_backup_timeout_minutes * 60
            started = time.monotonic()
            while process.poll() is None:
                if output.exists() and self._existing_dump_bytes() + output.stat().st_size > limit:
                    process.terminate()
                    process.wait()
                    raise RuntimeError("storage_limit")
                if time.monotonic() - started > timeout:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                    raise RuntimeError("backup_timeout")
                time.sleep(0.25)
            if process.returncode != 0 or not output.is_file() or output.stat().st_size == 0:
                raise RuntimeError("pg_dump_failed")
            if os.name != "nt":
                os.chmod(output, 0o600)
            self._update(
                backup_id, estado="completado",
                completado=datetime.now(timezone.utc).isoformat(),
                tamanoBytes=output.stat().st_size, error=None,
            )
        except Exception:
            try:
                output.unlink()
            except FileNotFoundError:
                pass
            try:
                self._update(
                    backup_id, estado="fallido",
                    completado=datetime.now(timezone.utc).isoformat(),
                    tamanoBytes=None,
                    error="No se pudo generar el respaldo. Verifica pg_dump, el rol de respaldo y el espacio disponible.",
                )
            except DomainError:
                _LOG.warning("No se pudo persistir el estado fallido del respaldo %s", backup_id)
            _LOG.warning("Falló la tarea local de respaldo %s", backup_id)
        finally:
            with self._guard:
                self._process = None
                self._active_id = None
                self._release_lock(self._locks.pop(backup_id, None))

    def get_job(self, backup_id: str) -> dict:
        self._ensure_storage()
        return self._read_job(backup_id)

    def download_path(self, backup_id: str) -> Path:
        self._ensure_storage()
        job = self._read_job(backup_id)
        if job.get("estado") != "completado":
            raise DomainError(409, "respaldo_no_disponible",
                              "El respaldo todavía no está disponible para descargar")
        path = self._dump_file(backup_id)
        try:
            resolved = path.resolve(strict=True)
        except FileNotFoundError:
            raise DomainError(404, "respaldo_no_encontrado", "El archivo de respaldo no existe") from None
        if path.is_symlink() or resolved.parent != self._directory.resolve() or not resolved.is_file():
            raise DomainError(404, "respaldo_no_encontrado", "El archivo de respaldo no existe")
        return resolved

    def close(self) -> None:
        with self._guard:
            self._closed = True
            process = self._process
            threads = list(self._threads.values())
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        for thread in threads:
            thread.join(timeout=10)
