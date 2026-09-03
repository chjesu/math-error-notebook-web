"""Check the browser plugin without importing the server or using a model."""

from pathlib import Path
import shutil
import subprocess
import unittest


@unittest.skipUnless(shutil.which("node"), "Node.js is required for plugin validation")
class HarnessClientRegistrationTests(unittest.TestCase):
    def assert_client_registers(self, source: str) -> None:
        self.assertNotRegex(source, r"(?m)^(<<<<<<<|=======|>>>>>>>)")
        script = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const registrations = [];
vm.runInNewContext(fs.readFileSync(0, 'utf8'), {
  window: {location: {origin: 'http://localhost:8123'}, __ModuleLoader__: {load: definition => registrations.push(definition)}}
}, {timeout: 1000});
assert.equal(registrations.length, 1);
assert.equal(registrations[0].id, '@lizhaolin/dsh-math-notebook-ui');
const plugin = registrations[0].factory(name => {
  assert.equal(name, 'react/jsx-runtime');
  return {jsx() {}};
});
assert.equal(typeof plugin.apply, 'function');
assert.ok(Array.isArray(plugin.inject));
assert.ok(plugin.inject.includes('slots'));
"""
        result = subprocess.run(
            [shutil.which("node"), "-e", script], input=source,
            text=True, encoding="utf-8", capture_output=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_client_bundle_registers_with_harness(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "extensions" /
                  "dsh-math-notebook-ui" / "lib" / "client.js").read_text(encoding="utf-8")
        self.assert_client_registers(source)
