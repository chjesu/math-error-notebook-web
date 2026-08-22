from __future__ import annotations

import base64
import os
from unittest import mock
import unittest

from services.web_auth.bootstrap import _allowed_hosts, _mysql_connection_factory, _pepper


class BootstrapTests(unittest.TestCase):
    def test_parses_pepper_and_allowed_hosts(self) -> None:
        environment = {
            "LZLM_AUTH_PEPPER_B64": base64.b64encode(b"p" * 32).decode("ascii"),
            "LZLM_ALLOWED_HOSTS": "app.example.cn, api.example.cn",
        }
        with mock.patch.dict(os.environ, environment, clear=True):
            self.assertEqual(_pepper(), b"p" * 32)
            self.assertEqual(_allowed_hosts(), {"app.example.cn", "api.example.cn"})

    def test_remote_mysql_fails_closed_without_tls_ca(self) -> None:
        environment = {
            "LZLM_MYSQL_HOST": "mysql.example.cn",
            "LZLM_MYSQL_USER": "app",
            "LZLM_MYSQL_PASSWORD": "runtime-only",
            "LZLM_MYSQL_DATABASE": "notebook",
        }
        with mock.patch.dict(os.environ, environment, clear=True):
            with self.assertRaisesRegex(RuntimeError, "SSL_CA"):
                _mysql_connection_factory()


if __name__ == "__main__":
    unittest.main()
