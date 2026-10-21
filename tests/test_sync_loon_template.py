import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import sync_loon_template as sync


HOME = """proxy-groups:
  - {name: AI, type: select, proxies: [DIRECT, REJECT], icon: 'https://example.com/icon.png'}
  - {name: '🌏.Route-[Asia]', type: fallback, proxies: [DIRECT]}
rule-providers:
  R-one: {behavior: domain}
  # R-two: {behavior: ipcidr}
rules:
  - RULE-SET,R-one,AI
  #- RULE-SET,R-two,REJECT-DROP,no-resolve
  - MATCH,Final
"""

TEMPLATE = """[General]
ip-mode = v4-only

[Proxy]
# @generate:proxy

[Proxy Chain]
# @generate:proxy-chain

[Proxy Group]
# @sync-home:groups:start
Old = select,DIRECT
# @sync-home:groups:end
# @generate:route
# @generate:line
# @generate:chain
# @generate:direct-exit

[Rule]
FINAL,Final

[Remote Rule]
# @sync-home:rules:start
https://example.com/old.list, policy=AI
# @sync-home:rules:end

[Mitm]
hostname = example.com
"""


class SyncLoonTemplateTests(unittest.TestCase):
    def test_sync_includes_commented_rule_and_preserves_static_sections(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home.yaml"
            template = root / "template.lcf"
            home.write_text(HOME, encoding="utf-8")
            template.write_text(TEMPLATE, encoding="utf-8")
            result, groups, rules = sync.render(home, template)
            self.assertEqual((groups, rules), (1, 2))
            self.assertIn("AI = select,DIRECT,REJECT,img-url=https://example.com/icon.png", result)
            self.assertIn("/R-one.list, policy=AI, tag=R-one, enabled=true", result)
            self.assertIn("/R-two.list, policy=REJECT-DROP, tag=R-two, enabled=true", result)
            self.assertIn("[General]\nip-mode = v4-only", result)
            self.assertIn("[Mitm]\nhostname = example.com", result)
            self.assertIn("# @generate:route", result)
            self.assertNotIn("Old = select,DIRECT", result)

    def test_bad_source_and_missing_marker_leave_template_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home.yaml"
            template = root / "template.lcf"
            template.write_text(TEMPLATE, encoding="utf-8")
            for source in (HOME.replace("R-one: {behavior: domain}", "R-one: {behavior: domain}\n  R-one: {behavior: domain}"),
                           HOME.replace("R-one: {behavior: domain}", "R-absent: {behavior: domain}")):
                home.write_text(source, encoding="utf-8")
                with self.assertRaises(ValueError):
                    sync.render(home, template)
                self.assertEqual(template.read_text(encoding="utf-8"), TEMPLATE)
            home.write_text(HOME, encoding="utf-8")
            template.write_text(TEMPLATE.replace(sync.END_RULES, ""), encoding="utf-8")
            with self.assertRaises(ValueError):
                sync.render(home, template)

    def test_waits_for_matching_published_lists(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home.yaml"
            template = root / "template.lcf"
            lists = root / "lists"
            lists.mkdir()
            home.write_text(HOME, encoding="utf-8")
            template.write_text(TEMPLATE, encoding="utf-8")
            (lists / "R-one.list").write_text("DOMAIN,example.com\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "1 missing"):
                sync.render(home, template, lists)
            (lists / "R-two.list").write_text("IP-CIDR,192.0.2.0/24\n", encoding="utf-8")
            _, groups, rules = sync.render(home, template, lists)
            self.assertEqual((groups, rules), (1, 2))

    def test_cli_waits_without_changing_template_then_syncs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home.yaml"
            template = root / "template.lcf"
            lists = root / "lists"
            lists.mkdir()
            home.write_text(HOME, encoding="utf-8")
            template.write_text(TEMPLATE, encoding="utf-8")
            (lists / "R-one.list").write_text("DOMAIN,example.com\n", encoding="utf-8")
            command = [sys.executable, str(Path(sync.__file__)), "--home", str(home),
                       "--template", str(template), "--lists-dir", str(lists),
                       "--wait-for-lists"]
            waiting = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(waiting.returncode, 0, waiting.stderr)
            self.assertIn("Waiting for published Loon lists", waiting.stdout)
            self.assertEqual(template.read_text(encoding="utf-8"), TEMPLATE)
            (lists / "R-two.list").write_text("IP-CIDR,192.0.2.0/24\n", encoding="utf-8")
            synced = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(synced.returncode, 0, synced.stderr)
            self.assertIn("Updated template", synced.stdout)
            self.assertIn("/R-two.list", template.read_text(encoding="utf-8"))
            home.write_text(HOME.replace("R-one: {behavior: domain}",
                                         "R-one: {behavior: domain}\n  R-one: {behavior: domain}"),
                            encoding="utf-8")
            invalid = subprocess.run(command, capture_output=True, text=True)
            self.assertNotEqual(invalid.returncode, 0)

    def test_rejects_private_content_in_public_template(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home.yaml"
            template = root / "template.lcf"
            home.write_text(HOME, encoding="utf-8")
            for injected in (TEMPLATE.replace("[Mitm]\n", "[Mitm]\nca-p12 = fixture\n"),
                             TEMPLATE.replace("# @generate:proxy\n", "# @generate:proxy\nNode = VLESS,example.com,443,fixture\n")):
                template.write_text(injected, encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "private content"):
                    sync.render(home, template)


if __name__ == "__main__":
    unittest.main()
