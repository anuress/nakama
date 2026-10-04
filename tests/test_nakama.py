import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
NAKAMA = REPO / "bin" / "nakama"

FAKE_HERDR = r'''#!/usr/bin/env python3
import json, os, sys, time
st_path = os.environ["FAKE_STATE"]
st = json.load(open(st_path))
args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write(json.dumps(["herdr"] + args) + "\n")
def save():
    json.dump(st, open(st_path, "w"))
cmd = args[:2]
if args == ["--version"]:
    print("herdr " + st.get("version", "0.9.3"))
elif cmd == ["pane", "split"]:
    print(json.dumps({"result": {"pane": {"pane_id": "w1:p9"}}}))
elif cmd == ["tab", "create"]:
    print(json.dumps({"result": {"tab": {"tab_id": "w1:t5"}, "root_pane": {"pane_id": "w1:p8"}}}))
elif cmd == ["agent", "list"]:
    print(json.dumps({"result": {"agents": st.get("agents", [])}}))
elif cmd == ["agent", "start"]:
    sys.exit(st.get("start_rc", 0))
elif cmd == ["agent", "get"]:
    if st.get("status") is None:
        sys.exit(1)
    print(json.dumps({"result": {"agent": {"agent_status": st["status"]}}}))
elif cmd == ["agent", "prompt"]:
    write = st.get("write_on_prompt")
    if write:
        time.sleep(0.02)
        open(write["path"], "w").write(write["text"])
elif cmd == ["agent", "wait"]:
    if "after_wait_status" in st:
        st["status"] = st["after_wait_status"]
        save()
elif cmd == ["agent", "read"]:
    if st.get("busy_read_fails") and "recent-unwrapped" in args:
        sys.exit(1)
    print(st.get("screen", "SCREEN"))
'''

FAKE_WT = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write(json.dumps(["wt"] + args) + "\n")
if args[0] == "switch":
    path = os.path.join(os.environ["FAKE_ROOT"], "wt-" + args[2].replace("/", "-"))
    os.makedirs(path, exist_ok=True)
    print(json.dumps({"path": path}))
elif args[0] == "list":
    print(json.dumps({"schema": 2, "items": [{"branch": "agent/b1-old"}, {"branch": "develop"}]}))
'''


class NakamaTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        bin_dir = self.bin_dir = self.root / "bin"
        bin_dir.mkdir()
        for name, body in (("herdr", FAKE_HERDR), ("wt", FAKE_WT)):
            path = bin_dir / name
            path.write_text(body)
            path.chmod(0o755)
        roles = self.root / "roles"
        roles.mkdir()
        (roles / "builder.md").write_text(
            "---\nprefix: b\nkind: pi\nmode: write\nbudget: 45\n---\nYou are a builder.\n")
        (roles / "reviewer.md").write_text(
            "---\nprefix: r\nkind: claude\nmodel: opus  # strongest\nmode: read\nbudget: 20\n"
            "names: zoro, sanji\n---\nYou are a reviewer.\n")
        self.state = self.root / "state.json"
        self.log = self.root / "log.jsonl"
        self.log.write_text("")
        self.set_state(status="idle")
        self.env = os.environ.copy()
        self.env.pop("NAKAMA_AGENT", None)
        self.env.update({
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "HERDR_ENV": "1",
            "FAKE_STATE": str(self.state),
            "FAKE_LOG": str(self.log),
            "FAKE_ROOT": str(self.root),
            "NAKAMA_STATE_DIR": str(self.root / "records"),
            "NAKAMA_ROLES_DIR": str(self.root / "roles"),
            "NAKAMA_USER_ROLES_DIR": str(self.root / "userroles"),
            "NAKAMA_GRACE_SEC": "0",
            "NAKAMA_POLL_SEC": "0",
        })

    def set_state(self, **state):
        self.state.write_text(json.dumps(state))

    def nakama(self, *args, env=None):
        p = subprocess.run([sys.executable, str(NAKAMA), *args], capture_output=True, text=True,
                           env=env or self.env, cwd=self.root)
        return p.returncode, json.loads(p.stdout) if p.stdout.strip() else {}

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def launch(self, *extra):
        rc, out = self.nakama("launch", "builder", "Order Tip", "--kind", "pi", *extra)
        self.assertEqual(rc, 0, out)
        return out

    def task(self, out, name="task.md"):
        path = Path(out["dir"], name)
        path.write_text("## Goal\nx\n")
        return path

    def result(self, out):
        return str(Path(out["dir"], "result.md"))

    def help_text(self, *args):
        return subprocess.run([sys.executable, str(NAKAMA), *args, "--help"],
                              capture_output=True, text=True).stdout

    def test_help_describes_every_command(self):
        text = self.help_text()
        self.assertNotIn("{launch,", text)
        for command in ("launch", "prompt", "wait", "extend", "teardown", "list", "sweep", "roles", "doctor", "init"):
            line = next((l for l in text.splitlines() if l.strip().startswith(command + " ")), "")
            self.assertTrue(line.split(command, 1)[1].strip(), f"no description for {command}")

    def test_launch_help_explains_options(self):
        text = self.help_text("launch")
        for phrase in ("roles/<role>.md", "own herdr tab", "builder's worktree", "current checkout", "minutes"):
            self.assertIn(phrase, text)

    def test_name_rule_valid_and_capped(self):
        rc, out = self.nakama("launch", "builder", "Order Detail: Tip Tracker!! with a very long title", "--kind", "pi")
        self.assertEqual(rc, 0, out)
        self.assertRegex(out["name"], r"^[a-z][a-z0-9_-]{0,31}$")
        self.assertTrue(out["name"].startswith("b1-order-detail--tip-tracker"))

    def test_same_slug_gets_next_number(self):
        self.assertEqual(self.launch()["name"], "b1-order-tip")
        self.assertEqual(self.launch()["name"], "b2-order-tip")

    def assert_backstop(self, call):
        envs = [call[i + 1] for i, value in enumerate(call) if value == "--env"]
        for expected in ("GIT_SSH_COMMAND=false", "GIT_TERMINAL_PROMPT=0", "GIT_CONFIG_COUNT=1",
                         "GIT_CONFIG_KEY_0=credential.helper", "GIT_CONFIG_VALUE_0="):
            self.assertIn(expected, envs)
        self.assertTrue(any(e.startswith("GH_CONFIG_DIR=") and len(e) > 14 for e in envs))

    def test_launch_opens_own_tab_with_push_backstop(self):
        out = self.launch()
        tab = next(c for c in self.calls() if c[1:3] == ["tab", "create"])
        self.assertEqual(tab[tab.index("--label") + 1], out["name"])
        self.assertIn("--no-focus", tab)
        self.assert_backstop(tab)
        self.assertEqual(out["pane"], "w1:p8")
        self.assertFalse(any(c[1:3] == ["pane", "split"] for c in self.calls()))

    def test_worker_pane_knows_its_agent_name(self):
        out = self.launch()
        tab = next(c for c in self.calls() if c[1:3] == ["tab", "create"])
        self.assertIn(f"NAKAMA_AGENT={out['name']}", tab)

    def test_worker_cannot_launch_agents_without_spawns(self):
        parent = self.launch()
        rc, out = self.nakama("launch", "builder", "child", env={**self.env, "NAKAMA_AGENT": parent["name"]})
        self.assertEqual(rc, 2)
        self.assertIn("spawns", out["error"])
        self.assertEqual(sum(1 for c in self.calls() if c[1:3] == ["tab", "create"]), 1)

    def test_role_with_spawns_may_launch_agents(self):
        user = self.root / "userroles"
        user.mkdir()
        (user / "lead.md").write_text("---\nprefix: l\nmode: write\nspawns: true\n---\nYou lead.\n")
        lead = self.nakama("launch", "lead", "epic")[1]
        rc, out = self.nakama("launch", "builder", "child", env={**self.env, "NAKAMA_AGENT": lead["name"]})
        self.assertEqual(rc, 0, out)
        roles = {r["role"]: r for r in self.nakama("roles")[1]["roles"]}
        self.assertTrue(roles["lead"]["spawns"])
        self.assertFalse(roles["builder"]["spawns"])

    def test_split_layout_splits_with_push_backstop(self):
        out = self.launch("--layout", "split")
        self.assert_backstop(next(c for c in self.calls() if c[1:3] == ["pane", "split"]))
        self.assertEqual(out["pane"], "w1:p9")

    def test_launch_creates_worktree_on_agent_branch(self):
        out = self.launch()
        self.assertEqual(out["branch"], "agent/b1-order-tip")
        self.assertTrue(Path(out["dir"]).is_dir())
        self.assertIn(["wt", "step", "copy-ignored", "--require-include"], self.calls())

    def test_refuses_outside_herdr(self):
        rc, out = self.nakama("launch", "builder", "x", "--kind", "pi", env={**self.env, "HERDR_ENV": ""})
        self.assertEqual(rc, 2)
        self.assertIn("herdr", out["error"])

    def test_start_failure_reports_blocked_with_screen(self):
        self.set_state(status="idle", start_rc=1)
        out = self.launch()
        self.assertEqual(out["status"], "blocked")
        self.assertIn("SCREEN", out["screen"])

    def test_joined_reviewer_gets_own_dir(self):
        builder = self.launch()
        rc, rev = self.nakama("launch", "reviewer", "order-tip", "--kind", "claude", "--join", builder["name"])
        self.assertEqual(rc, 0, rev)
        self.assertEqual(rev["worktree"], builder["worktree"])
        self.assertNotEqual(rev["dir"], builder["dir"])
        self.assertIsNone(rev["branch"])
        self.assertEqual(sum(1 for c in self.calls() if c[:2] == ["wt", "switch"]), 1)

    def test_wait_returns_fresh_result_status(self):
        out = self.launch()
        self.task(out)
        self.set_state(status="idle",
                       write_on_prompt={"path": self.result(out), "text": "Status: succeeded\n## Summary\nok\n"})
        self.assertEqual(self.nakama("prompt", out["name"], "task.md")[1]["status"], "prompted")
        self.set_state(status="working", after_wait_status="idle")
        self.assertEqual(self.nakama("wait", out["name"], "--timeout", "5")[1]["status"], "succeeded")

    def test_fresh_result_counts_only_once_the_agent_settles(self):
        # An agent may write result.md and keep working (commit, rewrite it); that is not done yet.
        out = self.launch()
        self.task(out)
        self.set_state(status="idle", write_on_prompt={"path": self.result(out), "text": "Status: succeeded\n"})
        self.nakama("prompt", out["name"], "task.md")
        self.set_state(status="working")
        self.assertEqual(self.nakama("wait", out["name"], "--timeout", "0")[1]["status"], "running")
        self.set_state(status="idle")
        self.assertEqual(self.nakama("wait", out["name"], "--timeout", "5")[1]["status"], "succeeded")

    def test_prompt_refused_while_agent_is_working(self):
        out = self.launch()
        self.task(out)
        self.task(out, "fix-1.md")
        self.nakama("prompt", out["name"], "task.md")
        self.set_state(status="working")
        rc, res = self.nakama("prompt", out["name"], "fix-1.md")
        self.assertEqual((rc, res["status"], res["rounds"]), (0, "busy", 0))
        self.assertEqual(sum(1 for c in self.calls() if c[1:3] == ["agent", "prompt"]), 1)

    def test_stale_result_is_ignored(self):
        out = self.launch()
        self.task(out)
        Path(self.result(out)).write_text("Status: succeeded\n")
        old = time.time() - 600
        os.utime(self.result(out), (old, old))
        self.nakama("prompt", out["name"], "task.md")
        self.assertEqual(self.nakama("wait", out["name"], "--timeout", "5")[1]["status"], "failed")

    def test_result_without_status_line_is_malformed(self):
        out = self.launch()
        self.task(out)
        self.set_state(status="idle", write_on_prompt={"path": self.result(out), "text": "done!\n"})
        self.nakama("prompt", out["name"], "task.md")
        self.assertEqual(self.nakama("wait", out["name"], "--timeout", "5")[1]["status"], "malformed")

    def test_nudges_exactly_once_then_fails(self):
        out = self.launch()
        self.task(out)
        self.nakama("prompt", out["name"], "task.md")
        res = self.nakama("wait", out["name"], "--timeout", "5")[1]
        self.assertEqual(res["status"], "failed")
        prompts = [c for c in self.calls() if c[1:3] == ["agent", "prompt"]]
        self.assertEqual(len(prompts), 2)
        self.assertIn("result.md", prompts[1][-1])

    def test_round_limit_and_answers_do_not_count(self):
        out = self.launch()
        for name in ("fix-1.md", "fix-2.md", "fix-3.md", "fix-4.md"):
            self.task(out, name)
        self.assertEqual(self.nakama("prompt", out["name"], "fix-1.md", "--answer")[1]["rounds"], 0)
        self.assertEqual(self.nakama("prompt", out["name"], "fix-2.md")[1]["rounds"], 1)
        self.assertEqual(self.nakama("prompt", out["name"], "fix-3.md")[1]["rounds"], 2)
        res = self.nakama("prompt", out["name"], "fix-4.md")[1]
        self.assertEqual(res["status"], "round_limit")
        self.assertEqual(sum(1 for c in self.calls() if c[1:3] == ["agent", "prompt"]), 3)

    def test_prompt_rejects_missing_file(self):
        out = self.launch()
        self.assertEqual(self.nakama("prompt", out["name"], "task.md")[0], 2)

    def test_blocked_returns_screen(self):
        out = self.launch()
        self.task(out)
        self.set_state(status="blocked")
        self.nakama("prompt", out["name"], "task.md")
        res = self.nakama("wait", out["name"], "--timeout", "5")[1]
        self.assertEqual(res["status"], "blocked")
        self.assertIn("SCREEN", res["screen"])

    def test_extension_dialog_while_working_is_blocked(self):
        # herdr reports pi extension dialogs (e.g. a security scanner) as working, not blocked.
        out = self.launch()
        self.task(out)
        self.nakama("prompt", out["name"], "task.md")
        self.set_state(status="working", screen="Suspicious bash command detected. Allow execution?\n Yes\n No")
        res = self.nakama("wait", out["name"], "--timeout", "5")[1]
        self.assertEqual(res["status"], "blocked")
        self.assertIn("Allow execution?", res["screen"])

    def test_screen_of_a_busy_agent_falls_back_to_visible(self):
        out = self.launch()
        self.task(out)
        self.nakama("prompt", out["name"], "task.md")
        self.set_state(status="working", busy_read_fails=True)
        record = self.root / "records" / f"{out['name']}.json"
        rec = json.loads(record.read_text())
        rec["prompted_at"] -= 46 * 60
        record.write_text(json.dumps(rec))
        res = self.nakama("wait", out["name"], "--timeout", "5")[1]
        self.assertEqual((res["status"], res["screen"].strip()), ("over_budget", "SCREEN"))

    def test_over_budget(self):
        out = self.launch()
        self.task(out)
        self.nakama("prompt", out["name"], "task.md")
        self.set_state(status="working")
        record = self.root / "records" / f"{out['name']}.json"
        rec = json.loads(record.read_text())
        rec["prompted_at"] -= 46 * 60
        record.write_text(json.dumps(rec))
        self.assertEqual(self.nakama("wait", out["name"], "--timeout", "5")[1]["status"], "over_budget")

    def test_extend_refuses_past_ceiling(self):
        out = self.launch()
        self.assertEqual(self.nakama("extend", out["name"], "120")[1]["status"], "extended")
        self.assertEqual(self.nakama("extend", out["name"], "60")[1]["status"], "refused")

    def test_agent_gone_is_failed(self):
        out = self.launch()
        self.task(out)
        self.set_state(status=None)
        self.nakama("prompt", out["name"], "task.md")
        self.assertEqual(self.nakama("wait", out["name"], "--timeout", "5")[1]["status"], "failed")

    def test_running_when_timeout_expires(self):
        out = self.launch()
        self.task(out)
        self.nakama("prompt", out["name"], "task.md")
        self.set_state(status="working")
        self.assertEqual(self.nakama("wait", out["name"], "--timeout", "0")[1]["status"], "running")

    def test_failed_prompt_leaves_the_record_untouched(self):
        out = self.launch()
        self.task(out)
        self.task(out, "fix-1.md")
        self.nakama("prompt", out["name"], "task.md")
        record = self.root / "records" / f"{out['name']}.json"
        before = record.read_text()
        broken = r'''#!/usr/bin/env python3
import json, sys
if sys.argv[1:3] == ["agent", "prompt"]:
    sys.exit(7)
print(json.dumps({"result": {"agent": {"agent_status": "idle"}}}))
'''
        herdr = self.bin_dir / "herdr"
        herdr.write_text(broken)
        herdr.chmod(0o755)
        rc, res = self.nakama("prompt", out["name"], "fix-1.md")
        self.assertEqual(rc, 1, res)
        self.assertIn("error", res)
        self.assertEqual(record.read_text(), before)
        herdr.write_text(FAKE_HERDR)
        herdr.chmod(0o755)
        self.assertEqual(self.nakama("prompt", out["name"], "fix-1.md")[1]["rounds"], 1)

    def test_marker_above_the_screen_tail_is_not_blocked(self):
        # Source code or a log can quote the dialog; only the last 10 non-empty lines are live.
        out = self.launch()
        self.task(out)
        self.nakama("prompt", out["name"], "task.md")
        self.set_state(status="working", screen="grep output: Allow execution?\n"
                       + "\n".join(f"work {i}" for i in range(10)))
        self.assertEqual(self.nakama("wait", out["name"], "--timeout", "0")[1]["status"], "running")

    def test_marker_in_the_screen_tail_still_blocks(self):
        # The window counts non-empty lines: blank scrollback must not push the dialog out of it.
        out = self.launch()
        self.task(out)
        self.nakama("prompt", out["name"], "task.md")
        self.set_state(status="working", screen="Allow execution?\n" + "\n" * 15 + " Yes\n No")
        res = self.nakama("wait", out["name"], "--timeout", "0")[1]
        self.assertEqual(res["status"], "blocked")
        self.assertIn("Allow execution?", res["screen"])

    def test_teardown_removes_only_the_gh_config_dir_it_created(self):
        out = self.launch()
        record = self.root / "records" / f"{out['name']}.json"
        mine = Path(json.loads(record.read_text())["gh_config_dir"])
        self.assertTrue(mine.is_dir())
        self.assertTrue(mine.name.startswith("nakama-gh-"))
        decoy = Path(tempfile.mkdtemp(prefix="nakama-gh-"))
        self.assertEqual(self.nakama("teardown", out["name"])[1]["status"], "closed")
        self.assertFalse(mine.exists())
        self.assertTrue(decoy.is_dir())

    def test_sweep_finds_custom_names_through_their_record(self):
        # A custom --name matches no pattern; its record is the only proof nakama started it.
        out = self.launch("--name", "custom-agent")
        self.set_state(status="idle", agents=[{"name": "custom-agent"}, {"name": "someone-elses"}])
        res = self.nakama("sweep")[1]
        self.assertEqual(res["stray_agents"], [])
        self.assertEqual([r["name"] for r in res["open_records"]], [out["name"]])
        self.nakama("teardown", out["name"])
        before = len(self.calls())
        res = self.nakama("sweep")[1]
        self.assertEqual(res["stray_agents"], ["custom-agent"])
        self.assertFalse(any(c[1:3] in (["agent", "send-keys"], ["tab", "close"], ["pane", "close"])
                             for c in self.calls()[before:]))

    def test_teardown_closes_tab_and_keeps_worktree_by_default(self):
        out = self.launch()
        res = self.nakama("teardown", out["name"])[1]
        self.assertEqual(res, {"name": out["name"], "status": "closed", "worktree_removed": False})
        self.assertIn(["herdr", "tab", "close", "w1:t5"], self.calls())
        self.assertFalse(any(c[:2] == ["wt", "remove"] for c in self.calls()))

    def test_teardown_of_split_layout_closes_pane(self):
        out = self.launch("--layout", "split")
        self.nakama("teardown", out["name"])
        self.assertIn(["herdr", "pane", "close", "w1:p9"], self.calls())

    def test_sweep_lists_orphans_and_kills_nothing(self):
        builder = self.launch()
        self.set_state(status="idle", agents=[{"name": "r7-stray"}, {"name": "pi-main"}])
        res = self.nakama("sweep")[1]
        self.assertEqual(res["stray_agents"], ["r7-stray"])
        self.assertEqual([r["name"] for r in res["open_records"]], [builder["name"]])
        self.assertEqual(res["agent_worktrees"], ["agent/b1-old"])
        self.assertFalse(any(c[1:3] in (["pane", "close"], ["tab", "close"], ["agent", "send-keys"])
                             for c in self.calls()))

    def start_call(self):
        return next(c for c in self.calls() if c[1:3] == ["agent", "start"])

    def test_role_defaults_from_frontmatter(self):
        rc, out = self.nakama("launch", "reviewer", "tip", "--no-worktree")
        self.assertEqual(rc, 0, out)
        start = self.start_call()
        self.assertEqual(start[start.index("--kind") + 1], "claude")
        self.assertEqual(start[start.index("--model") + 1], "opus")
        self.assertEqual(Path(out["dir"], "persona.md").read_text(), "You are a reviewer.\n")
        roles = {r["role"]: r for r in self.nakama("roles")[1]["roles"]}
        self.assertEqual(roles["reviewer"]["names"], ["zoro", "sanji"])
        self.assertEqual(roles["builder"]["mode"], "write")

    def test_claude_always_gets_an_explicit_permission_mode(self):
        # A user default of auto mode would otherwise let a read-only role edit files.
        self.nakama("launch", "reviewer", "tip", "--no-worktree")
        self.nakama("launch", "builder", "tip", "--no-worktree", "--kind", "claude")
        starts = [c for c in self.calls() if c[1:3] == ["agent", "start"]]
        modes = [s[s.index("--permission-mode") + 1] for s in starts]
        self.assertEqual(modes, ["default", "acceptEdits"])
        # Read-only inspection must not stop on a permission prompt.
        for tool in ("Bash(git grep:*)", "Bash(git ls-files:*)"):
            self.assertIn(tool, starts[0])

    def test_flags_override_frontmatter(self):
        rc, out = self.nakama("launch", "reviewer", "tip", "--no-worktree",
                              "--kind", "pi", "--model", "x/y", "--budget", "5")
        self.assertEqual(rc, 0, out)
        start = self.start_call()
        self.assertEqual(start[start.index("--kind") + 1], "pi")
        self.assertEqual(start[start.index("--model") + 1], "x/y")
        self.assertIn("--tools", start)
        rec = json.loads((self.root / "records" / f"{out['name']}.json").read_text())
        self.assertEqual(rec["budget_min"], 5)

    def test_kind_override_drops_the_roles_model(self):
        # A role's model names a model of its own kind; under another CLI it is meaningless.
        self.nakama("launch", "reviewer", "tip", "--no-worktree", "--kind", "pi")
        self.assertNotIn("--model", self.start_call())

    def test_explicit_model_survives_kind_override(self):
        self.nakama("launch", "reviewer", "tip", "--no-worktree", "--kind", "pi", "--model", "x/y")
        start = self.start_call()
        self.assertEqual(start[start.index("--model") + 1], "x/y")

    def test_name_pool_then_numbered(self):
        names = [self.nakama("launch", "reviewer", "tip", "--no-worktree")[1]["name"] for _ in range(3)]
        self.assertEqual(names, ["zoro-tip", "sanji-tip", "zoro2-tip"])

    def test_explicit_name_validated(self):
        self.assertEqual(self.nakama("launch", "builder", "x", "--name", "Bad Name")[0], 2)
        self.assertEqual(self.nakama("launch", "builder", "x", "--name", "kaido")[1]["name"], "kaido")
        self.assertEqual(self.nakama("launch", "builder", "y", "--name", "kaido")[0], 2)

    def test_unknown_role_refused(self):
        rc, out = self.nakama("launch", "wizard", "x")
        self.assertEqual(rc, 2)
        self.assertIn("unknown role", out["error"])

    def install_json_fake(self, command, outputs):
        script = self.bin_dir / command
        script.write_text(f"""#!/usr/bin/env python3
import json, os, sys
with open(os.environ['FAKE_LOG'], 'a') as log:
    log.write(json.dumps(sys.argv[1:]) + '\\n')
outputs = json.loads({json.dumps(json.dumps(outputs))})
value = outputs.get(' '.join(sys.argv[1:3]), {{}})
print(value if isinstance(value, str) else json.dumps(value))
""")
        script.chmod(0o755)

    def test_role_frontmatter_unquotes_values_and_accepts_yaml_name_list(self):
        (self.root / "roles" / "yaml.md").write_text("""---
prefix: y
kind: claude
model: \"opus\"
mode: read
budget: '7'
names: [zoro, sanji]
---
You are a reviewer.
""")
        rc, out = self.nakama("roles")
        self.assertEqual(rc, 0, out)
        role = next(role for role in out["roles"] if role["role"] == "yaml")
        self.assertEqual(role["model"], "opus")
        self.assertEqual(role["budget"], 7)
        self.assertEqual(role["names"], ["zoro", "sanji"])

    def test_non_integer_role_budget_returns_json_error_for_role_commands(self):
        (self.root / "roles" / "broken.md").write_text("""---
prefix: x
budget: abc
---
Broken role.
""")
        expected = {"error": "role broken: budget must be a whole number of minutes"}
        for args in (("launch", "broken", "x"), ("roles",), ("sweep",)):
            with self.subTest(args=args):
                rc, out = self.nakama(*args)
                self.assertEqual(rc, 2)
                self.assertEqual(out, expected)

    def test_malformed_herdr_list_output_returns_json_error(self):
        self.install_json_fake("herdr", {"agent list": "not json"})
        rc, out = self.nakama("launch", "builder", "x")
        self.assertEqual(rc, 1)
        self.assertEqual(out, {"error": "herdr agent list returned unexpected output", "stdout": "not json\n"})

    def test_missing_agent_status_returns_json_error(self):
        records = self.root / "records"
        records.mkdir()
        (records / "b1-order-tip.json").write_text(json.dumps({
            "name": "b1-order-tip", "role": "builder", "kind": "pi", "worktree": str(self.root),
            "rounds": 0, "closed": False,
        }))
        self.install_json_fake("herdr", {
            "agent list": {"result": {"agents": []}},
            "agent get": {"result": {"agent": {}}},
        })
        rc, out = self.nakama("list")
        self.assertEqual(rc, 1)
        self.assertEqual(out, {"error": "herdr agent get returned unexpected output", "stdout": '{"result": {"agent": {}}}\n'})

    def test_wt_switch_missing_path_fails_before_copy_ignored(self):
        self.install_json_fake("wt", {"switch -c": {}})
        rc, out = self.nakama("launch", "builder", "x")
        self.assertEqual(rc, 1)
        self.assertEqual(out, {"error": "wt switch returned unexpected output", "stdout": "{}\n"})
        self.assertNotIn(["step", "copy-ignored", "--require-include"], self.calls())

    def test_tab_create_missing_pane_id_returns_json_error(self):
        self.install_json_fake("herdr", {
            "agent list": {"result": {"agents": []}},
            "tab create": {"result": {"tab": {"tab_id": "w1:t5"}}},
        })
        rc, out = self.nakama("launch", "builder", "x")
        self.assertEqual(rc, 1)
        self.assertEqual(out, {
            "error": "herdr tab create returned unexpected output",
            "stdout": '{"result": {"tab": {"tab_id": "w1:t5"}}}\n',
        })

    def test_pane_split_missing_id_returns_json_error(self):
        self.install_json_fake("herdr", {
            "agent list": {"result": {"agents": []}},
            "pane split": {"result": {"pane": {}}},
        })
        rc, out = self.nakama("launch", "builder", "x", "--layout", "split")
        self.assertEqual(rc, 1)
        self.assertEqual(out, {
            "error": "herdr pane split returned unexpected output",
            "stdout": '{"result": {"pane": {}}}\n',
        })

    def test_sweep_missing_worktree_items_returns_json_error(self):
        self.install_json_fake("herdr", {"agent list": {"result": {"agents": []}}})
        self.install_json_fake("wt", {"list --format": {"schema": 2}})
        rc, out = self.nakama("sweep")
        self.assertEqual(rc, 1)
        self.assertEqual(out, {
            "error": "wt list returned unexpected output", "stdout": '{"schema": 2}\n',
        })

    def test_user_roles_override_bundled(self):
        user = self.root / "userroles"
        user.mkdir()
        (user / "reviewer.md").write_text("---\nprefix: q\nkind: pi\n---\nMine.\n")
        (user / "tester.md").write_text("---\nprefix: t\nmode: write\n---\nYou test.\n")
        roles = {r["role"]: r for r in self.nakama("roles")[1]["roles"]}
        self.assertEqual(roles["reviewer"]["kind"], "pi")
        self.assertEqual(roles["reviewer"]["path"], str(user / "reviewer.md"))
        self.assertEqual(roles["tester"]["mode"], "write")
        self.assertEqual(roles["builder"]["path"], str(self.root / "roles" / "builder.md"))

    def doctor_env(self, version="0.9.3", ignored=True):
        home = self.root / "home"
        (home / ".pi/agent/extensions").mkdir(parents=True)
        (home / ".pi/agent/extensions/herdr-agent-state.ts").write_text("")
        if ignored:
            (home / ".config/git").mkdir(parents=True)
            (home / ".config/git/ignore").write_text(".DS_Store\n.agent/\n")
        pi = self.bin_dir / "pi"
        pi.write_text("#!/bin/sh\nexit 0\n")
        pi.chmod(0o755)
        self.set_state(status="idle", version=version)
        env = {k: v for k, v in self.env.items() if k != "XDG_CONFIG_HOME"}
        return {**env, "HOME": str(home), "PATH": f"{self.bin_dir}:/usr/bin:/bin"}

    def test_doctor_passes_when_everything_is_present(self):
        rc, out = self.nakama("doctor", env=self.doctor_env())
        self.assertEqual(rc, 0)
        self.assertTrue(out["ok"], out)
        self.assertNotIn("herdr claude integration", [c["check"] for c in out["checks"]])

    def test_doctor_reports_fixes(self):
        out = self.nakama("doctor", env=self.doctor_env(version="0.9.2", ignored=False))[1]
        self.assertFalse(out["ok"])
        failed = {c["check"]: c["fix"] for c in out["checks"] if not c["ok"]}
        self.assertIn("herdr", failed["herdr >= 0.9.3"])
        self.assertIn("echo '.agent/'", failed[".agent/ globally git-ignored"])

    def git(self, repo, *args):
        return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True).stdout

    def test_init_writes_include_once_and_hides_it(self):
        repo = self.root / "repo"
        repo.mkdir()
        self.git(repo, "init", "-q")
        env = {**self.env, "PATH": f"{self.bin_dir}:/usr/bin:/bin"}
        for _ in range(2):
            p = subprocess.run([sys.executable, str(NAKAMA), "init", "local.properties", "ktlint"],
                               capture_output=True, text=True, env=env, cwd=repo)
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual((repo / ".worktreeinclude").read_text(), "local.properties\nktlint\n")
        self.assertEqual(self.git(repo, "status", "--porcelain"), "")

    def test_install_links_is_idempotent_and_uninstalls(self):
        home = self.root / "home2"
        env = {**self.env, "HOME": str(home), "PATH": f"{self.bin_dir}:/usr/bin:/bin"}
        sh = [str(REPO / "install.sh")]
        first = subprocess.run(sh, capture_output=True, text=True, env=env)
        self.assertEqual(first.returncode, 0, first.stderr)
        link = home / ".local/bin/nakama"
        self.assertEqual(os.readlink(link), str(NAKAMA))
        self.assertTrue((home / ".claude/skills/nakama-orchestrate").is_symlink())
        second = subprocess.run(sh, capture_output=True, text=True, env=env)
        self.assertIn("ok      " + str(link), second.stdout)
        subprocess.run(sh + ["--uninstall"], capture_output=True, text=True, env=env)
        self.assertFalse(link.exists() or link.is_symlink())

    def test_install_never_overwrites_foreign_files(self):
        home = self.root / "home3"
        (home / ".claude/skills/nakama").mkdir(parents=True)
        env = {**self.env, "HOME": str(home), "PATH": f"{self.bin_dir}:/usr/bin:/bin"}
        p = subprocess.run([str(REPO / "install.sh")], capture_output=True, text=True, env=env)
        self.assertEqual(p.returncode, 1)
        self.assertIn("SKIP", p.stderr)
        self.assertTrue((home / ".claude/skills/nakama").is_dir())

    def clone_alias(self):
        alias = self.root / "clone-alias"
        alias.symlink_to(REPO, target_is_directory=True)
        return alias

    def test_install_via_symlinked_clone_path_stays_ok(self):
        # A link must record the physical clone path: re-running through the real path reports ok.
        home = self.root / "home4"
        env = {**self.env, "HOME": str(home), "PATH": f"{self.bin_dir}:/usr/bin:/bin"}
        first = subprocess.run([str(self.clone_alias() / "install.sh")], capture_output=True, text=True, env=env)
        self.assertEqual(first.returncode, 0, first.stderr)
        link = home / ".local/bin/nakama"
        self.assertEqual(os.readlink(link), str(NAKAMA))
        second = subprocess.run([str(REPO / "install.sh")], capture_output=True, text=True, env=env)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("ok      " + str(link), second.stdout)

    def test_uninstall_removes_links_of_either_spelling(self):
        # A link may name this clone through a symlinked path or the real one; both are ours.
        home = self.root / "home5"
        env = {**self.env, "HOME": str(home), "PATH": f"{self.bin_dir}:/usr/bin:/bin"}
        link = home / ".local/bin/nakama"
        for clone in (self.clone_alias(), REPO):
            link.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(str(clone / "bin/nakama"), str(link))
            p = subprocess.run([str(REPO / "install.sh"), "--uninstall"], capture_output=True, text=True, env=env)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertFalse(link.is_symlink(), f"kept a link spelling {clone}")

    def test_install_leaves_links_into_another_clone_alone(self):
        other = self.root / "other-clone"
        (other / "bin").mkdir(parents=True)
        (other / "bin/nakama").write_text("#!/bin/sh\n")
        home = self.root / "home6"
        link = home / ".local/bin/nakama"
        link.parent.mkdir(parents=True)
        link.symlink_to(other / "bin/nakama")
        env = {**self.env, "HOME": str(home), "PATH": f"{self.bin_dir}:/usr/bin:/bin"}
        p = subprocess.run([str(REPO / "install.sh")], capture_output=True, text=True, env=env)
        self.assertEqual(p.returncode, 1)
        self.assertIn("SKIP", p.stderr)
        subprocess.run([str(REPO / "install.sh"), "--uninstall"], capture_output=True, text=True, env=env)
        self.assertEqual(os.readlink(link), str(other / "bin/nakama"))

    def test_doctor_accepts_any_spelling_of_the_global_ignore(self):
        env = self.doctor_env(ignored=False)
        home = Path(env["HOME"])
        (home / ".config/git").mkdir(parents=True)
        for spelling in (".agent", "/.agent/", "**/.agent/**"):
            (home / ".config/git/ignore").write_text(f"*.log\n{spelling}\n")
            checks = {c["check"]: c for c in self.nakama("doctor", env=env)[1]["checks"]}
            self.assertTrue(checks[".agent/ globally git-ignored"]["ok"], f"spelling {spelling!r} not recognised")

    def test_doctor_honours_core_excludes_file(self):
        env = self.doctor_env(ignored=False)
        home = Path(env["HOME"])
        (home / ".gitconfig").write_text(f"[core]\n\texcludesFile = {home}/my-ignore\n")
        (home / "my-ignore").write_text("# extras\n/.agent/\n")
        checks = {c["check"]: c for c in self.nakama("doctor", env=env)[1]["checks"]}
        self.assertTrue(checks[".agent/ globally git-ignored"]["ok"], checks[".agent/ globally git-ignored"])

    def test_skill_tells_orchestrators_about_timeouts_and_budget(self):
        text = (REPO / "skills" / "nakama" / "SKILL.md").read_text()
        rows = [l for l in text.splitlines() if l.startswith("| `nakama")]
        wait = next(l for l in rows if l.startswith("| `nakama wait"))
        extend = next(l for l in rows if l.startswith("| `nakama extend"))
        self.assertIn("Bash tool", wait)
        self.assertIn("300000 ms", wait)
        self.assertIn("--timeout 100", wait)
        self.assertIn("180-minute ceiling applies per prompt", extend)
        self.assertIn("--answer", extend)


if __name__ == "__main__":
    unittest.main()
