"""Administración de la plataforma SaaS (usuarios `tipo='S'`).

Las rutas de administración usan `platform_session_factory`. En desarrollo se
puede configurar explícitamente la misma credencial que para el resto de la API;
una conexión compartida no proporciona aislamiento por rol de base de datos.
"""
