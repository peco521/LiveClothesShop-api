"""Alta interactiva propuesta; requiere aprobación explícita antes de ejecutarse.

La contraseña se introduce en la terminal, no se guarda en archivos ni se imprime.
El archivo no se ejecuta al iniciar la aplicación.
"""
import argparse
import getpass
import json
from datetime import date
from uuid import uuid4

from sqlalchemy import create_engine, text

from app.core.config import Settings
from app.core.security import Passwords

EMAIL = 'adminsaas@fashionstore.com'
PROJECT = 'vleqtsrmaqvslyugurzj'


def required(label, max_length):
    value = input(f'{label}: ').strip()
    if not value or len(value) > max_length:
        raise ValueError(f'{label} es obligatorio y admite hasta {max_length} caracteres')
    return value


def main():
    parser = argparse.ArgumentParser(description='Alta propuesta de administrador SaaS')
    parser.add_argument('--confirmar-crear', action='store_true')
    args = parser.parse_args()
    if not args.confirmar_crear:
        print('Sin cambios. Con aprobación previa, --confirmar-crear solicita los datos y registra la cuenta y su auditoría.')
        return

    settings = Settings()
    url = settings.database_url.get_secret_value()
    if PROJECT not in url or settings.password_storage != 'argon2id':
        raise SystemExit('Proyecto distinto o almacenamiento de contraseñas inseguro; no se hicieron cambios')

    data = {'id': str(uuid4()), 'correo': EMAIL}
    for key, label, limit in [('ci', 'CI', 100), ('nombre', 'Nombres', 100),
                              ('apellido_pat', 'Apellido paterno', 50), ('apellido_mat', 'Apellido materno', 50),
                              ('telefono', 'Teléfono', 20), ('direccion', 'Dirección', 150)]:
        data[key] = required(label, limit)
    data['sexo'] = required('Sexo (M/F)', 1).upper()
    if data['sexo'] not in {'M', 'F'}:
        raise ValueError('Sexo debe ser M o F')
    data['fecha_nac'] = date.fromisoformat(required('Fecha de nacimiento (AAAA-MM-DD)', 10))
    if data['fecha_nac'] > date.today():
        raise ValueError('La fecha de nacimiento no puede ser futura')
    password = getpass.getpass('Contraseña (12 a 128 caracteres): ')
    confirm = getpass.getpass('Repite la contraseña: ')
    if password != confirm or not password.strip() or not 12 <= len(password) <= 128:
        raise ValueError('Las contraseñas no coinciden o no cumplen la longitud requerida')
    data['password_hash'] = Passwords('argon2id').hash(password)
    del password, confirm

    engine = create_engine(url, hide_parameters=True)
    try:
        with engine.begin() as db:
            db.execute(text("SELECT pg_advisory_xact_lock(hashtext('crear_administrador_saas'))"))
            exists = db.scalar(text('SELECT count(*) FROM usuario WHERE lower(trim(correo)) = :correo'), {'correo': EMAIL})
            if exists:
                raise ValueError('Ya existe una cuenta con ese correo')
            db.execute(text("""
                INSERT INTO usuario (idusuario, ci, nombre, apellidopat, apellidomat, sexo, correo,
                                     telefono, direccion, contrasena, fechanac, tipo, nrorol, idemp)
                VALUES (:id, :ci, :nombre, :apellido_pat, :apellido_mat, :sexo, :correo,
                        :telefono, :direccion, :password_hash, :fecha_nac, 'S', 'SAAS', NULL)
            """), data)
            db.execute(text("""
                INSERT INTO bitacora (usuario_id, accion, detalles, idemp)
                VALUES (:id, 'administrador_saas_creado', CAST(:details AS jsonb), NULL)
            """), {'id': data['id'], 'details': json.dumps({'resultado': 'exito'})})
    finally:
        engine.dispose()
    print('Cuenta creada y registrada en la bitácora.')


if __name__ == '__main__':
    main()
