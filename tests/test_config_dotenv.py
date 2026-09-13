"""Configuration-only tests: fictitious credentials, no database connections."""
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from pydantic import ValidationError
from pydantic_settings import SettingsConfigDict

from app.core.config import Settings


DOTENV_URL = "postgresql+psycopg://fake_user:fake_password@localhost/fake_db"
ENV_URL = "postgresql+psycopg://env_user:fake_password@localhost/env_db"


class DotenvConfigurationTests(TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_dotenv_and_environment_precedence(self):
        with TemporaryDirectory() as directory:
            dotenv = Path(directory) / ".env"
            dotenv.write_text(
                f"DATABASE_URL={DOTENV_URL}\nCLIENTE_ROL_ID=fake_cliente\n"
                "ENVIRONMENT=test\nUNUSED_SETTING=ignored\n", encoding="utf-8"
            )
            settings = Settings(_env_file=dotenv)
            self.assertEqual(settings.database_url.get_secret_value(), DOTENV_URL)
            self.assertEqual(settings.cliente_rol_id, "fake_cliente")
            self.assertNotIn("fake_password", repr(settings))
            os.environ["DATABASE_URL"] = ENV_URL
            self.assertEqual(Settings(_env_file=dotenv).database_url.get_secret_value(), ENV_URL)

    def test_absolute_dotenv_path_from_different_cwds(self):
        configured_path = Settings.model_config["env_file"]
        self.assertEqual(configured_path, Path(__file__).resolve().parents[1] / ".env")
        self.assertTrue(configured_path.is_absolute())
        # Use a fictitious file, never the developer's real .env.
        with TemporaryDirectory() as directory:
            dotenv = Path(directory) / ".env"
            dotenv.write_text(
                f"DATABASE_URL={DOTENV_URL}\nCLIENTE_ROL_ID=fake_cliente\n", encoding="utf-8"
            )

            class LocalSettings(Settings):
                model_config = SettingsConfigDict(env_file=dotenv)

            original_cwd = Path.cwd()
            try:
                for cwd in (Path(directory), Path(directory).parent):
                    os.chdir(cwd)
                    self.assertEqual(LocalSettings().database_url.get_secret_value(), DOTENV_URL)
            finally:
                os.chdir(original_cwd)

    def test_dotenv_can_be_disabled(self):
        os.environ.update(DATABASE_URL=ENV_URL, CLIENTE_ROL_ID="fake_cliente")
        self.assertEqual(Settings(_env_file=None).database_url.get_secret_value(), ENV_URL)

    def test_role_is_required_and_error_hides_input(self):
        with self.assertRaises(ValidationError) as error:
            Settings(_env_file=None, database_url=DOTENV_URL)
        self.assertIn("cliente_rol_id", str(error.exception))
        self.assertNotIn("fake_password", str(error.exception))
