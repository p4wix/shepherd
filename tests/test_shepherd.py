"""Tests for bin/shepherd. Never calls herdr: in-process tests replace the herdr
helpers with a function that fails the test, and CLI tests use --dry-run or `list`
with HOME pointed at a temp dir so the prompt cache lands there.

Run: python3 -m unittest discover -s tests -v
"""

import importlib.machinery
import importlib.util
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import tomllib
import unittest
import unittest.mock
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "bin" / "shepherd"

loader = importlib.machinery.SourceFileLoader("shepherd", str(SCRIPT))
spec = importlib.util.spec_from_loader("shepherd", loader)
sh = importlib.util.module_from_spec(spec)
loader.exec_module(sh)


def _no_herdr(*args):
    raise AssertionError(f"herdr must not be called in tests: {args}")


sh.herdr = _no_herdr
sh.herdr_ok = _no_herdr

PLACEHOLDER = re.compile(r"\{\{[^}]*\}\}")
REPO_PLAYBOOKS = sorted(p.stem for p in (REPO / "playbooks").glob("*.md"))


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text).lstrip(), encoding="utf-8")


class TempProject(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.project = self.tmp / "proj"
        self.project.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def run_cli(self, *args, cwd=None):
        env = {**os.environ, "HOME": str(self.home)}
        # A fake herdr first on PATH: if the script ever calls it, the test fails loudly.
        fake_bin = self.tmp / "fakebin"
        write(fake_bin / "herdr", "#!/bin/sh\necho 'herdr called' >&2\nexit 99\n")
        (fake_bin / "herdr").chmod(0o755)
        env["PATH"] = f"{fake_bin}{os.pathsep}{env.get('PATH', '')}"
        r = subprocess.run([sys.executable, str(SCRIPT), *args], cwd=cwd or self.project,
                           env=env, capture_output=True, text=True, timeout=60)
        self.assertNotIn("herdr called", r.stderr)
        return r

    def dry_run(self, *args, label="proj"):
        r = self.run_cli("--dry-run", str(self.project), label, *args)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r

    def prompt(self, prefix, suffix):
        p = self.home / ".cache" / "shepherd" / prefix / f"{suffix}.md"
        self.assertTrue(p.is_file(), f"missing prompt {p}")
        return p.read_text(encoding="utf-8")


class BuildAgentsTest(TempProject):
    def test_crew_numbers_workers_across_entries(self):
        team = sh.load_team(self.project, "crew")
        prefix, agents = sh.build_agents(self.project, team, "proj")
        self.assertEqual(prefix, "proj")
        self.assertEqual([a["name"] for a in agents],
                         ["proj-lead", "proj-worker-1", "proj-worker-2", "proj-worker-3", "proj-worker-4"])
        self.assertEqual([a["kind"] for a in agents], ["claude", "claude", "claude", "codex", "codex"])

    def test_single_worker_has_no_number(self):
        team = {"agents": [{"role": "lead"}, {"role": "worker", "kind": "codex"}]}
        _, agents = sh.build_agents(self.project, team, "proj")
        self.assertEqual([a["name"] for a in agents], ["proj-lead", "proj-worker"])

    def test_explicit_name_is_the_base(self):
        team = {"agents": [{"role": "worker", "name": "rev", "kind": "codex"},
                           {"role": "worker", "name": "rev", "kind": "claude"},
                           {"role": "worker"}]}
        _, agents = sh.build_agents(self.project, team, "proj")
        self.assertEqual([a["name"] for a in agents], ["proj-rev-1", "proj-rev-2", "proj-worker"])

    def test_same_name_collision_dies(self):
        # worker-1 (explicit name) and worker-1 (numbered) clash.
        team = {"agents": [{"role": "worker", "name": "worker-1"}, {"role": "worker", "count": 2}]}
        with self.assertRaises(SystemExit):
            sh.build_agents(self.project, team, "proj")

    def test_role_args_come_before_team_args_and_root_expands(self):
        team = {"agents": [{"role": "lead", "args": ["--model", "opus"]}, {"role": "worker"}]}
        _, agents = sh.build_agents(self.project, team, "proj")
        self.assertEqual(agents[0]["args"], ["--plugin-dir", f"{sh.ROOT}/plugin", "--model", "opus"])
        self.assertEqual(agents[1]["args"], [])
        self.assertTrue((sh.ROOT / "plugin").is_dir())

    def test_root_expands_in_flag_value_and_team_args(self):
        write(self.project / ".shepherd/roles/solo.md", """
            +++
            args = ["--settings={{root}}/settings/x.json"]
            +++
            body
        """)
        team = {"agents": [{"role": "solo", "args": ["{{root}}/a", "~/b"]}]}
        _, agents = sh.build_agents(self.project, team, "proj")
        self.assertEqual(agents[0]["args"], [f"--settings={sh.ROOT}/settings/x.json",
                                             f"{sh.ROOT}/a", str(Path("~/b").expanduser())])

    def test_team_skills_merge_over_role_skills(self):
        team = {"agents": [{"role": "lead", "skills": {"extra": "team skill", "herdr": "overridden"}}]}
        _, agents = sh.build_agents(self.project, team, "proj")
        self.assertEqual(agents[0]["skills"]["extra"], "team skill")
        self.assertEqual(agents[0]["skills"]["herdr"], "overridden")
        self.assertIn("shepherd:dispatch", agents[0]["skills"])


class KindsTest(unittest.TestCase):
    def test_codex_gets_a_short_command_and_a_profile_with_the_prompt(self):
        with tempfile.TemporaryDirectory() as home, \
                unittest.mock.patch.dict(os.environ, {"CODEX_HOME": home}):
            text = 'line "one"\nline two \u0105 \\ end\n'
            args = sh.KINDS["codex"]({"name": "proj-worker-3", "text": text})
            self.assertEqual(args, ["--no-daemon", "--approve-for-me", "-p", "shepherd-proj-worker-3"])
            profile = Path(home) / "shepherd-proj-worker-3.config.toml"
            data = tomllib.loads(profile.read_text(encoding="utf-8"))
            self.assertEqual(data, {"developer_instructions": text.strip()})


class LayoutTest(unittest.TestCase):
    def layout(self, count):
        splits = []

        def fake_herdr_ok(*args):
            pane, direction = args[2], args[args.index("--direction") + 1]
            new = f"p{len(splits) + 1}"
            splits.append((pane, direction, new))
            return {"pane": {"pane_id": new}}

        with unittest.mock.patch.object(sh, "herdr_ok", fake_herdr_ok):
            return sh.layout("p0", count, Path("/tmp")), splits

    def test_five_agents_give_two_left_and_three_right(self):
        panes, splits = self.layout(5)
        # p0 lead, p2 worker-1 under it; p1 right column top, p3 and p4 under it.
        self.assertEqual(panes, ["p0", "p2", "p1", "p3", "p4"])
        self.assertEqual(splits, [("p0", "right", "p1"), ("p0", "down", "p2"),
                                  ("p1", "down", "p3"), ("p3", "down", "p4")])

    def test_small_teams(self):
        self.assertEqual(self.layout(1), (["p0"], []))
        self.assertEqual(self.layout(2)[0], ["p0", "p1"])
        self.assertEqual(self.layout(3)[0], ["p0", "p1", "p2"])  # lead alone on the left


class PlaybookTableTest(TempProject):
    def test_repo_playbooks_listed_with_absolute_paths(self):
        table = sh.playbook_table(self.project)
        for name in REPO_PLAYBOOKS:
            path = REPO / "playbooks" / f"{name}.md"
            self.assertTrue(path.is_absolute())
            self.assertIn(f"| {name} |", table)
            self.assertIn(str(path), table)
        self.assertIn("| reviewer | codex, claude |", table)

    def test_project_playbook_overrides_and_adds(self):
        write(self.project / ".shepherd/playbooks/tester.md", """
            +++
            description = "project tester"
            kinds = ["codex"]
            +++
            body
        """)
        write(self.project / ".shepherd/playbooks/auditor.md", """
            +++
            description = "project auditor"
            +++
            body
        """)
        table = sh.playbook_table(self.project)
        own = self.project / ".shepherd/playbooks"
        self.assertIn(f"| tester | codex | project tester | {own / 'tester.md'} |", table)
        self.assertNotIn(str(REPO / "playbooks/tester.md"), table)
        self.assertIn(f"| auditor | any | project auditor | {own / 'auditor.md'} |", table)
        self.assertIn(str(REPO / "playbooks/reviewer.md"), table)


class DryRunTest(TempProject):
    def test_default_team_is_crew(self):
        r = self.dry_run()
        self.assertIn("Team 'crew'", r.stdout)
        for suffix in ["lead", "worker-1", "worker-2", "worker-3", "worker-4"]:
            self.assertIn(f"proj-{suffix}", r.stdout)
        self.assertRegex(r.stdout, r"proj-worker-3\s+codex")
        self.assertRegex(r.stdout, r"proj-worker-2\s+claude")

    def test_worker_count_flag_follows_kind_order(self):
        r = self.dry_run("--5")
        kinds = re.findall(r"proj-(\S+)\s+(\w+)", r.stdout)
        self.assertEqual(kinds, [("lead", "claude"), ("worker-1", "claude"), ("worker-2", "codex"),
                                 ("worker-3", "codex"), ("worker-4", "claude"), ("worker-5", "codex")])
        r = self.dry_run("--workers", "1")
        self.assertEqual(re.findall(r"proj-(\S+)\s+(\w+)", r.stdout), [("lead", "claude"), ("worker", "claude")])

    def test_worker_count_flag_refuses_team_without_workers(self):
        write(self.project / ".shepherd" / "teams" / "solo.toml", '[[agents]]\nrole = "lead"\n')
        r = self.run_cli("--dry-run", "--team", "solo", "--2", str(self.project))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("needs a team with workers", r.stderr)

    def test_lead_prompt_has_every_playbook_and_no_placeholders(self):
        self.dry_run()
        text = self.prompt("proj", "lead")
        for name in REPO_PLAYBOOKS:
            self.assertIn(str(REPO / "playbooks" / f"{name}.md"), text)
        self.assertEqual(PLACEHOLDER.findall(text), [])
        self.assertIn("proj-worker-1, proj-worker-2, proj-worker-3, proj-worker-4", text)
        cache = self.home / ".cache" / "shepherd" / "proj"
        self.assertIn(f"{cache}/board.md", text)
        self.assertNotIn(sh.NONE_IN_TEAM, text)

    def test_worker_prompts_render_lead_and_root(self):
        self.dry_run()
        for suffix in ["worker-1", "worker-4"]:
            text = self.prompt("proj", suffix)
            self.assertEqual(PLACEHOLDER.findall(text), [])
            self.assertIn(f"{REPO}/playbooks/tester.md", text)
            self.assertIn("proj-lead sends you tasks", text)

    def test_unknown_placeholder_renders_none_in_team(self):
        write(self.project / ".shepherd/stacks/s.md", "Ask {{reviewer}} or {{no-such}}.\n")
        self.dry_run("--stack", "s")
        text = self.prompt("proj", "lead")
        self.assertIn(f"Ask {sh.NONE_IN_TEAM} or {sh.NONE_IN_TEAM}.", text)

    def test_project_context_keeps_literal_braces(self):
        write(self.project / ".shepherd.toml", 'context = "Pass {{customer_id}} and {{lead}}."\n')
        self.dry_run()
        text = self.prompt("proj", "lead")
        self.assertIn("Pass {{customer_id}} and {{lead}}.", text)

    def test_skills_from_role_team_and_project_config(self):
        write(self.project / ".shepherd.toml", '[skills]\n"proj-skill" = "for everyone"\n')
        write(self.project / ".shepherd/teams/t.toml", """
            [[agents]]
            role = "lead"
            [agents.skills]
            "team-skill" = "only the lead"

            [[agents]]
            role = "worker"
        """)
        self.dry_run("--team", "t")
        lead = self.prompt("proj", "lead")
        worker = self.prompt("proj", "worker")
        skills = lead.split("## Your skills", 1)[1]
        self.assertIn("- `herdr`:", skills)
        self.assertIn("- `team-skill`: only the lead", skills)
        self.assertIn("- `proj-skill`: for everyone", skills)
        self.assertIn("## Your skills", worker)
        worker_skills = worker.split("## Your skills", 1)[1]
        self.assertIn("- `proj-skill`: for everyone", worker_skills)
        self.assertNotIn("team-skill", worker)

    def test_project_playbook_in_lead_prompt(self):
        write(self.project / ".shepherd/playbooks/auditor.md", """
            +++
            description = "project auditor"
            kinds = ["codex"]
            +++
            body
        """)
        self.dry_run()
        text = self.prompt("proj", "lead")
        self.assertIn(str(self.project / ".shepherd/playbooks/auditor.md"), text)

    def test_cache_placeholder_uses_label_prefix(self):
        write(self.project / ".shepherd/stacks/s.md", "Cache: {{cache}}\n")
        self.dry_run("--stack", "s", label="My App")
        text = self.prompt("my-app", "worker-1")
        self.assertIn(f"Cache: {self.home / '.cache' / 'shepherd' / 'my-app'}", text)

    def test_list_shows_playbooks(self):
        write(self.project / ".shepherd/playbooks/auditor.md", """
            +++
            description = "project auditor"
            kinds = ["codex"]
            +++
            body
        """)
        r = self.run_cli("list")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("\nPlaybooks:\n", r.stdout)
        section = r.stdout.split("\nPlaybooks:\n", 1)[1].split("\n\n", 1)[0]
        for name in REPO_PLAYBOOKS + ["auditor"]:
            self.assertRegex(section, rf"(?m)^  {name}\s")
        self.assertIn("project auditor (codex)", section)
        self.assertIn("crew", r.stdout.split("\nRoles:\n", 1)[0])
        self.assertIn("worker x2 (claude)", r.stdout)


if __name__ == "__main__":
    unittest.main()
