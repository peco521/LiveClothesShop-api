"""Create a verified, operator-approved backup before a Supabase migration.

This tool is deliberately inert without --approved. It never reads or prints a
repository .env; pass the connection only through SUPABASE_BACKUP_DATABASE_URL.
The output folder must be outside the repository or an ignored temporary path.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.engine import make_url

PROJECT_HOST = "aws-0-us-west-2.pooler.supabase.com"
PROJECT_REF = "vleqtsrmaqvslyugurzj"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description="Respaldo verificable previo a migración Supabase")
    parser.add_argument("--approved", action="store_true", help="confirma que el operador autorizó crear el dump")
    parser.add_argument("--output-dir", required=True, help="carpeta vacía para el respaldo")
    parser.add_argument("--pg-dump", default="pg_dump")
    parser.add_argument("--pg-restore", default="pg_restore")
    args = parser.parse_args()
    if not args.approved:
        raise SystemExit("No se creó respaldo: falta --approved.")
    raw_url = os.environ.get("SUPABASE_BACKUP_DATABASE_URL", "")
    if not raw_url:
        raise SystemExit("Falta SUPABASE_BACKUP_DATABASE_URL en el entorno privado.")
    url = make_url(raw_url)
    if url.host != PROJECT_HOST or url.database != "postgres" or PROJECT_REF not in (url.username or ""):
        raise SystemExit("Destino rechazado: no coincide con el proyecto Supabase autorizado.")

    output = Path(args.output_dir).expanduser().resolve()
    if output.exists() and any(output.iterdir()):
        raise SystemExit("La carpeta de salida ya contiene archivos; no se sobrescribe un respaldo.")
    output.mkdir(parents=True, exist_ok=True)
    dump = output / "fashionstore_supabase_pre_migration.dump"
    manifest = output / "fashionstore_supabase_pre_migration.manifest.json"
    if dump.exists() or manifest.exists():
        raise SystemExit("Ya existe un artefacto de respaldo en la salida.")

    pg_dump = shutil.which(args.pg_dump) or args.pg_dump
    pg_restore = shutil.which(args.pg_restore) or args.pg_restore
    child_env = os.environ.copy()
    child_env["PGPASSWORD"] = url.password or ""
    child_env["PGSSLMODE"] = "require"
    command = [pg_dump, "--host", url.host, "--port", str(url.port or 5432),
               "--username", url.username or "", "--dbname", url.database or "",
               "--no-password", "--format=custom", "--verbose", "--file", str(dump)]
    try:
        completed = subprocess.run(command, env=child_env, capture_output=True, text=True, timeout=3600)
        if completed.returncode:
            raise RuntimeError("pg_dump falló; el dump parcial se eliminará.")
        listing = subprocess.run([pg_restore, "--list", str(dump)], capture_output=True, text=True, timeout=300)
        if listing.returncode or not listing.stdout.strip():
            raise RuntimeError("pg_restore --list no pudo validar el archivo generado.")
        manifest.write_text(json.dumps({
            "created_at": datetime.now(timezone.utc).isoformat(),
            "project_ref": PROJECT_REF,
            "database": url.database,
            "dump_file": dump.name,
            "bytes": dump.stat().st_size,
            "sha256": sha256(dump),
            "restore_list_entries": len(listing.stdout.splitlines()),
            "restore_instruction": "Restaurar primero en una base aislada y validar; no usar --clean contra producción.",
        }, indent=2), encoding="utf-8")
    except Exception:
        dump.unlink(missing_ok=True)
        manifest.unlink(missing_ok=True)
        raise
    finally:
        child_env.pop("PGPASSWORD", None)
    print(f"Respaldo verificado: {dump.name}; manifiesto: {manifest.name}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
