"""Administración de la plataforma SaaS (usuarios `tipo='S'`).

Este paquete usa EXCLUSIVAMENTE el motor `platform_session_factory`, que se
conecta con un rol de base de datos con BYPASSRLS. Las rutas HTTP de empresa
siguen usando el motor del rol de aplicación (NOBYPASSRLS) y nunca importan
nada de aquí.
"""
