from __future__ import annotations

import unittest

from scripts import local_env


class LocalEnvironmentTests(unittest.TestCase):
    def test_server_cannot_bind_outside_loopback(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "localhost"):
            local_env.serve("0.0.0.0", 8000)

    def test_mysql_password_is_not_put_on_process_command_line(self) -> None:
        args = local_env._client_args(root=True)
        self.assertTrue(any(item.startswith("--defaults-extra-file=") for item in args))
        self.assertFalse(any(item.startswith("--password=") for item in args))


if __name__ == "__main__":
    unittest.main()
