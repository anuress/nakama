import importlib.machinery
import importlib.util
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

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
    barrier = os.environ.get("FAKE_TAB_BARRIER")
    if barrier:
        while not os.path.exists(barrier):
            time.sleep(0.01)
    print(json.dumps({"result": {"tab": {"tab_id": "w1:t5"}, "root_pane": {"pane_id": "w1:p8"}}}))
elif cmd == ["agent", "list"]:
    print(json.dumps({"result": {"agents": st.get("agents", [])}}))
elif cmd == ["agent", "start"]:
    sys.exit(st.get("start_rc", 0))
elif cmd == ["agent", "get"]:
    barrier = os.environ.get("FAKE_GET_BARRIER")
    if barrier:
        while not os.path.exists(barrier):
            time.sleep(0.01)
    name = args[2]
    status = st["statuses"].get(name) if name in st.get("statuses", {}) else st.get("status")
    if status is None:
        sys.exit(1)
    agent = {"agent_status": status}
    if "terminal_id" in st:
        agent["terminal_id"] = st["terminal_id"]
    if "agent_session" in st:
        agent["agent_session"] = st["agent_session"]
    print(json.dumps({"result": {"agent": agent}}))
elif cmd == ["agent", "prompt"]:
    if args[2] in st.get("prompt_fail", []):
        sys.exit(1)
    barrier = os.environ.get("FAKE_PROMPT_BARRIER")
    if barrier:
        while not os.path.exists(barrier):
            time.sleep(0.01)
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
    branch = args[2] if args[1] == "-c" else args[1]
    path = os.path.join(os.environ["FAKE_ROOT"], "wt-" + branch.replace("/", "-"))
    os.makedirs(path, exist_ok=True)
    print(json.dumps({"path": path}))
elif args[0] == "remove":
    if os.environ.get("FAKE_WT_REMOVE_STDERR"):
        print(os.environ["FAKE_WT_REMOVE_STDERR"], file=sys.stderr)
    if os.environ.get("FAKE_WT_REMOVE_STDOUT"):
        print(os.environ["FAKE_WT_REMOVE_STDOUT"])
    sys.exit(int(os.environ.get("FAKE_WT_REMOVE_RC", "0")))
elif args[0] == "list":
    items = json.load(open(os.environ["FAKE_STATE"])).get("worktrees") or [
        {"branch": "agent/b1-old"}, {"branch": "develop"}]
    print(json.dumps({"schema": 2, "items": items}))
'''

FAKE_HC = r'''#!/usr/bin/env python3
import json, os, sys
st = json.load(open(os.environ["FAKE_STATE"]))
hc = st.get("hc", {})
args = sys.argv[1:]
with open(os.environ["FAKE_HC_LOG"], "a") as log:
    log.write(json.dumps({"argv": ["hc", *args], "terminal_env": "HC_TERMINAL_ID" in os.environ}) + "\n")
cmd = args[0] if args else ""
if args == ["--version"]:
    print("hc " + hc.get("version", "0.1.19"))
elif cmd == "run":
    name = args[args.index("--name") + 1]
    print(json.dumps({"address": "t9", "agent": "pi", "cwd": None, "name": name,
                      "parent": "t1", "state": "live", "turn": 0}))
elif cmd == "get":
    if args[1] in hc.get("missing", []):
        sys.exit(3)
    if hc.get("terminal_state") == "finished" and args[1] != "t1":
        state, status = "finished", None
    else:
        state = "live"
        status = None if hc.get("agent_state", "idle") is None else {"app": "pi", "state": hc.get("agent_state", "idle")}
    print(json.dumps({"address": args[1], "state": state, "status": status,
                      "labels": {"herdr.space": "s7"}, "created_ms": 1794300123118}))
elif cmd == "ls":
    print(json.dumps({"terminals": hc.get("ls", [])}))
elif cmd == "wait":
    print(json.dumps({"children": [], "ended": False, "exit_code": None, "reason": "timeout",
                      "signal": None, "status": None, "terminal": args[1], "turn": 0}))
elif cmd == "read":
    print(hc.get("screen", "SCREEN"))
elif cmd == "send":
    sys.exit(hc.get("send_rc", 0))
elif cmd == "keys":
    pass
elif cmd == "kill":
    print(json.dumps({"address": args[1], "state": "finished", "status": None}))
elif cmd == "rm":
    print(json.dumps({"removed": args[1]}))
else:
    sys.exit(2)
'''

FAKE_HERDR_APP = r'''#!/usr/bin/env python3
import json, os, sys
st_path = os.environ["FAKE_STATE"]
st = json.load(open(st_path))
app = st.setdefault("herdr_app", {})
args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write(json.dumps(["herdr-app"] + args) + "\n")
if args[0] == "ls":
    print(json.dumps({"spaces": app.get("spaces", [])}))
elif args[:2] in (["do", "space.select"], ["do", "space.new"]):
    if app.get("rc"):
        print(app.get("stderr", "herdr-app failed"), file=sys.stderr)
        sys.exit(app["rc"])
    if args[1] == "space.new":
        want = json.loads(args[2])
        app.setdefault("spaces", []).append({"id": "sp-new", "name": want["name"],
                                            "folder": want["folder"], "machine": "local",
                                            "active": False, "tabs": [want["tab"]]})
        json.dump(st, open(st_path, "w"))
else:
    sys.exit(2)
'''

FAKE_GIT = r'''#!/usr/bin/env python3
import json, os, sys
st = json.load(open(os.environ["FAKE_STATE"]))
args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write(json.dumps(["git"] + args) + "\n")
if args[0] == "for-each-ref":
    print("\n".join(st.get("refs", [])))
else:
    sys.exit(2)
'''


class NakamaTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        bin_dir = self.bin_dir = self.root / "bin"
        bin_dir.mkdir()
        for name, body in (("herdr", FAKE_HERDR), ("wt", FAKE_WT), ("hc", FAKE_HC),
                           ("herdr-app", FAKE_HERDR_APP)):
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
        self.env.pop("NAKAMA_SESSION", None)  # a leaked export would make the session tests vacuous
        for name in ("HC_TERMINAL_ID", "HC_SCOPE", "HC_TERMINAL_SOCKET"):
            self.env.pop(name, None)  # a GUI terminal must not switch the suite to hc
        self.env.update({
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "HERDR_ENV": "1",
            "HERDR_WORKSPACE_ID": "w3",
            "HERDR_PANE_ID": "w3:p1",
            "FAKE_STATE": str(self.state),
            "FAKE_LOG": str(self.log),
            "FAKE_ROOT": str(self.root),
            "NAKAMA_STATE_DIR": str(self.root / "records"),
            "NAKAMA_ROLES_DIR": str(self.root / "roles"),
            "NAKAMA_USER_ROLES_DIR": str(self.root / "userroles"),
            "GIT_CONFIG_GLOBAL": os.devnull,  # no global ignores, hooks or signing leak into test repos
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

    def msg(self, text="hi"):
        path = self.root / "msg.md"
        path.write_text(text)
        return str(path)

    def orch_box(self, session="w3:p1"):
        safe_session = "".join(c if c.isalnum() or c in "_-" else "_" for c in session) or "default"
        return Path(self.env["NAKAMA_STATE_DIR"]) / "inbox" / "orchestrator" / safe_session

    def test_orchestrator_inboxes_and_waits_are_session_isolated(self):
        env_a = {**self.env, "HERDR_PANE_ID": "pane:A"}
        env_b = {**self.env, "HERDR_PANE_ID": "pane:B"}
        a = self.nakama("launch", "builder", "Session A", "--kind", "pi", env=env_a)[1]
        b = self.nakama("launch", "builder", "Session B", "--kind", "pi", env=env_b)[1]
        record_a = Path(env_a["NAKAMA_STATE_DIR"]) / f"{a['name']}.json"
        self.assertEqual(json.loads(record_a.read_text())["session"], "pane:A")
        worker_b = {**env_b, "NAKAMA_AGENT": b["name"]}
        self.nakama("send", "orchestrator", "ask", self.msg("B only"), env=worker_b)

        self.assertEqual(self.nakama("inbox", env=env_a)[1]["messages"], [])
        self.task(a)
        self.nakama("prompt", a["name"], "task.md", env=env_a)
        self.assertNotEqual(self.nakama("wait", a["name"], "--timeout", "0", env=env_a)[1]["status"], "message")
        self.assertTrue(self.orch_box("pane:B").is_dir())
        _, inbox_b = self.nakama("inbox", env=env_b)
        self.assertEqual([message["text"] for message in inbox_b["messages"]], ["B only"])

    def test_session_override_nested_inheritance_and_legacy_fallback(self):
        user_roles = self.root / "userroles"
        user_roles.mkdir()
        (user_roles / "lead.md").write_text(
            "---\nprefix: l\nkind: pi\nmode: write\nspawns: true\n---\nLead.\n")
        override = {**self.env, "HERDR_PANE_ID": "pane:original", "NAKAMA_SESSION": "chosen/session"}
        parent = self.nakama("launch", "lead", "Parent", env=override)[1]
        parent_record = Path(override["NAKAMA_STATE_DIR"]) / f"{parent['name']}.json"
        self.assertEqual(json.loads(parent_record.read_text())["session"], "chosen/session")
        child_env = {**self.env, "HERDR_PANE_ID": "pane:child", "NAKAMA_AGENT": parent["name"]}
        child = self.nakama("launch", "builder", "Child", "--kind", "pi", env=child_env)[1]
        child_record = Path(child_env["NAKAMA_STATE_DIR"]) / f"{child['name']}.json"
        self.assertEqual(json.loads(child_record.read_text())["session"], "chosen/session")
        worker = {**self.env, "NAKAMA_AGENT": parent["name"]}
        self.nakama("send", "orchestrator", "info", self.msg("override"), env=worker)
        self.assertEqual([m["text"] for m in self.nakama("inbox", env=override)[1]["messages"]], ["override"])

        legacy = json.loads(parent_record.read_text())
        legacy.pop("session")
        parent_record.write_text(json.dumps(legacy))
        self.nakama("send", "orchestrator", "info", self.msg("legacy"), env=worker)
        legacy_session = {**self.env, "HERDR_PANE_ID": "pane:original"}
        self.assertEqual([m["text"] for m in self.nakama("inbox", env=legacy_session)[1]["messages"]], ["legacy"])

    def test_list_filters_to_session_unless_all_requested(self):
        env_a = {**self.env, "HERDR_PANE_ID": "pane:A"}
        env_b = {**self.env, "HERDR_PANE_ID": "pane:B"}
        a = self.nakama("launch", "builder", "Session A", "--kind", "pi", env=env_a)[1]
        b = self.nakama("launch", "builder", "Session B", "--kind", "pi", env=env_b)[1]
        self.assertEqual([r["name"] for r in self.nakama("list", env=env_a)[1]["agents"]], [a["name"]])
        self.assertEqual({r["name"] for r in self.nakama("list", "--all", env=env_a)[1]["agents"]},
                         {a["name"], b["name"]})

    def test_worker_message_wakes_orchestrator_wait(self):
        out = self.launch()
        self.task(out)
        self.nakama("prompt", out["name"], "task.md")
        worker = {**self.env, "NAKAMA_AGENT": out["name"]}
        rc, sent = self.nakama("send", "orchestrator", "ask", self.msg("pick A or B?"), env=worker)
        self.assertEqual((rc, sent["status"]), (0, "sent"))
        _, waited = self.nakama("wait", out["name"], "--timeout", "1")
        self.assertEqual(waited["status"], "message")
        _, box = self.nakama("inbox")
        self.assertEqual([(m["from"], m["kind"], m["text"]) for m in box["messages"]],
                         [(out["name"], "ask", "pick A or B?")])
        self.assertEqual(self.nakama("inbox")[1]["messages"], [])
        self.assertNotEqual(self.nakama("wait", out["name"], "--timeout", "0")[1]["status"], "message")

    def test_worker_needs_peers_to_message_a_peer(self):
        a, b = self.launch(), self.launch()
        worker = {**self.env, "NAKAMA_AGENT": a["name"]}
        rc, err = self.nakama("send", b["name"], "info", self.msg(), env=worker)
        self.assertEqual(rc, 2)
        self.assertIn("peers", err["error"])
        user = self.root / "userroles"
        user.mkdir()
        (user / "builder.md").write_text("---\nprefix: b\nkind: pi\nmode: write\npeers: true\n---\nB.\n")
        self.assertEqual(self.nakama("send", b["name"], "info", self.msg(), env=worker)[0], 0)
        _, box = self.nakama("inbox", b["name"])
        self.assertEqual(box["messages"][0]["from"], a["name"])

    def test_send_pushes_pointer_into_working_worker(self):
        out = self.launch()
        self.set_state(status="working")
        rc, sent = self.nakama("send", out["name"], "info", self.msg("use B, not A"))
        self.assertEqual((rc, sent["pushed"]), (0, True))
        push = [c for c in self.calls() if c[1:3] == ["agent", "prompt"]][-1]
        self.assertEqual(push[3], out["name"])
        self.assertIn(".agent/" + out["name"] + "/inbox/", push[4])
        self.assertIn("overrides task.md", push[4])
        _, box = self.nakama("inbox", out["name"])
        self.assertEqual(box["messages"][0]["text"], "use B, not A")

    def test_send_does_not_push_when_agent_gone_or_to_orchestrator(self):
        out = self.launch()
        self.set_state(status=None)
        self.assertFalse(self.nakama("send", out["name"], "info", self.msg())[1]["pushed"])
        self.set_state(status="idle")
        worker = {**self.env, "NAKAMA_AGENT": out["name"]}
        self.assertFalse(self.nakama("send", "orchestrator", "info", self.msg(), env=worker)[1]["pushed"])
        self.assertFalse(any(c[1:3] == ["agent", "prompt"] for c in self.calls()))

    def test_send_done_pushes_pointer_into_orchestrator_pane(self):
        out = self.launch()
        worker = {**self.env, "NAKAMA_AGENT": out["name"]}
        rc, sent = self.nakama("send", "orchestrator", "done", self.msg(), env=worker)
        self.assertEqual((rc, sent["pushed"]), (0, True))
        push = [c for c in self.calls() if c[1:3] == ["agent", "prompt"]][-1]
        self.assertEqual(push[3], "w3:p1")
        self.assertIn("nakama inbox", push[4])

    def test_worker_launched_by_worker_pokes_root_orchestrator_pane(self):
        user = self.root / "userroles"
        user.mkdir()
        (user / "lead.md").write_text("---\nprefix: l\nmode: write\nspawns: true\n---\nYou lead.\n")
        parent = self.nakama("launch", "lead", "epic")[1]
        child_env = {**self.env, "NAKAMA_AGENT": parent["name"], "HERDR_PANE_ID": "w3:p77"}
        rc, child = self.nakama("launch", "builder", "Child", "--kind", "pi", env=child_env)
        self.assertEqual(rc, 0, child)
        worker = {**self.env, "NAKAMA_AGENT": child["name"]}
        self.nakama("send", "orchestrator", "done", self.msg(), env=worker)
        push = [c for c in self.calls() if c[1:3] == ["agent", "prompt"]][-1]
        self.assertEqual(push[3], "w3:p1")

    def test_send_done_keeps_mail_when_orchestrator_prompt_rejected(self):
        out = self.launch()
        self.set_state(prompt_fail=["w3:p1"])
        worker = {**self.env, "NAKAMA_AGENT": out["name"]}
        rc, sent = self.nakama("send", "orchestrator", "ask", self.msg("q"), env=worker)
        self.assertEqual((rc, sent["status"], sent["pushed"]), (0, "sent", False))
        _, box = self.nakama("inbox")
        self.assertEqual(box["messages"][0]["text"], "q")

    def test_wait_discards_done_mail_of_the_settled_agent(self):
        out = self.launch()
        worker = {**self.env, "NAKAMA_AGENT": out["name"]}
        self.nakama("send", "orchestrator", "done", self.msg(), env=worker)
        self.set_state(status="idle",
                       write_on_prompt={"path": self.result(out), "text": "Status: succeeded\nNext: review\n"})
        self.task(out)
        self.nakama("prompt", out["name"], "task.md")
        rc, res = self.nakama("wait", out["name"], "--timeout", "5")
        self.assertEqual(res.get("status"), "succeeded", res)
        self.assertEqual(self.nakama("inbox", "--peek")[1]["messages"], [])

    def test_send_same_id_delivers_once(self):
        out = self.launch()
        worker = {**self.env, "NAKAMA_AGENT": out["name"]}
        args = ("send", "orchestrator", "info", self.msg("once"), "--id", "retry_1")
        self.assertEqual(self.nakama(*args, env=worker)[1]["status"], "sent")
        rc, duplicate = self.nakama(*args, env=worker)
        self.assertEqual(rc, 0)
        self.assertEqual(duplicate, {"to": "orchestrator", "from": out["name"],
                                     "status": "duplicate", "id": "retry_1"})
        _, inbox = self.nakama("inbox")
        self.assertEqual([m["text"] for m in inbox["messages"]], ["once"])

    def test_send_different_ids_both_deliver(self):
        out = self.launch()
        worker = {**self.env, "NAKAMA_AGENT": out["name"]}
        for key in ("first", "second"):
            rc, sent = self.nakama("send", "orchestrator", "info", self.msg(key), "--id", key, env=worker)
            self.assertEqual((rc, sent["status"]), (0, "sent"))
        _, inbox = self.nakama("inbox", "--peek")
        self.assertEqual([m["text"] for m in inbox["messages"]], ["first", "second"])

    def test_send_without_id_behaves_as_before(self):
        out = self.launch()
        worker = {**self.env, "NAKAMA_AGENT": out["name"]}
        rc, sent = self.nakama("send", "orchestrator", "info", self.msg("ordinary"), env=worker)
        self.assertEqual((rc, sent["status"]), (0, "sent"))
        self.assertNotIn("id", sent)
        box = self.orch_box()
        self.assertFalse((box / ".ids").exists())
        _, inbox = self.nakama("inbox")
        self.assertEqual([m["text"] for m in inbox["messages"]], ["ordinary"])

    def test_send_rejects_invalid_id(self):
        rc, out = self.nakama("send", "orchestrator", "info", self.msg(), "--id", "bad.key")
        self.assertEqual(rc, 2)
        self.assertIn("--id", out["error"])

    def test_inbox_leaves_id_sidecar_untouched(self):
        out = self.launch()
        worker = {**self.env, "NAKAMA_AGENT": out["name"]}
        self.nakama("send", "orchestrator", "info", self.msg("keep sidecar"), "--id", "stable", env=worker)
        box = self.orch_box()
        id_file = box / ".ids" / f"{out['name']}.stable"
        self.assertTrue(id_file.is_file())
        _, inbox = self.nakama("inbox")
        self.assertEqual([m["text"] for m in inbox["messages"]], ["keep sidecar"])
        self.assertTrue(id_file.is_file())
        self.assertEqual(self.nakama("inbox")[1]["messages"], [])
        self.assertTrue(id_file.is_file())

    def test_inbox_full_does_not_record_id(self):
        out = self.launch()
        worker = {**self.env, "NAKAMA_AGENT": out["name"]}
        box = self.orch_box()
        box.mkdir(parents=True, exist_ok=True)
        for i in range(20):
            (box / f"{i}.{out['name']}.info.md").write_text("full")
        rc, full = self.nakama("send", "orchestrator", "info", self.msg(), "--id", "later", env=worker)
        self.assertEqual((rc, full["status"]), (0, "inbox_full"))
        id_file = box / ".ids" / f"{out['name']}.later"
        self.assertFalse(id_file.exists())
        (box / f"0.{out['name']}.info.md").unlink()
        rc, sent = self.nakama("send", "orchestrator", "info", self.msg("retry"), "--id", "later", env=worker)
        self.assertEqual((rc, sent["status"]), (0, "sent"))
        self.assertTrue(id_file.is_file())

    def test_inbox_full_counts_already_read_messages(self):
        out = self.launch()
        worker = {**self.env, "NAKAMA_AGENT": out["name"]}
        box = self.orch_box()
        box.mkdir(parents=True, exist_ok=True)
        for i in range(20):
            (box / f"{i}.{out['name']}.info.read").write_text("already read")
        rc, full = self.nakama("send", "orchestrator", "info", self.msg(), env=worker)
        self.assertEqual((rc, full["status"]), (0, "inbox_full"))

    def test_broadcast_reaches_live_workers_only(self):
        first, second, gone = self.launch(), self.launch(), self.launch()
        records_dir = Path(self.env["NAKAMA_STATE_DIR"])
        orchestrator = json.loads((records_dir / f"{first['name']}.json").read_text())
        orchestrator["name"] = "orchestrator"
        (records_dir / "orchestrator.json").write_text(json.dumps(orchestrator))
        self.set_state(status=None, statuses={first["name"]: "idle", second["name"]: "working",
                                             gone["name"]: None, "orchestrator": "idle"})
        rc, out = self.nakama("send", "@all", "info", self.msg("rebase"))
        self.assertEqual(rc, 0)
        self.assertEqual(out["to"], "@all")
        self.assertEqual(out["status"], "sent")
        self.assertEqual([r["to"] for r in out["results"]], [first["name"], second["name"]])
        self.assertFalse(any(r["to"] == "orchestrator" for r in out["results"]))
        for worker in (first, second):
            _, box = self.nakama("inbox", worker["name"])
            self.assertEqual([m["text"] for m in box["messages"]], ["rebase"])
        self.assertEqual(self.nakama("inbox", gone["name"])[1]["messages"], [])

    def test_broadcast_reaches_only_workers_in_current_session(self):
        env_a = {**self.env, "HERDR_PANE_ID": "pane:A"}
        env_b = {**self.env, "HERDR_PANE_ID": "pane:B"}
        a = self.nakama("launch", "builder", "Session A", "--kind", "pi", env=env_a)[1]
        b = self.nakama("launch", "builder", "Session B", "--kind", "pi", env=env_b)[1]
        self.set_state(status=None, statuses={a["name"]: "working", b["name"]: "working"})
        rc, out = self.nakama("send", "@all", "info", self.msg("A only"), env=env_a)
        self.assertEqual(rc, 0)
        self.assertEqual([result["to"] for result in out["results"]], [a["name"]])
        self.assertEqual(self.nakama("inbox", a["name"])[1]["messages"][0]["text"], "A only")
        self.assertEqual(self.nakama("inbox", b["name"])[1]["messages"], [])

    def test_worker_cannot_broadcast_even_with_peers(self):
        worker = self.launch()
        worker_env = {**self.env, "NAKAMA_AGENT": worker["name"]}
        rc, out = self.nakama("send", "@all", "info", self.msg(), env=worker_env)
        self.assertEqual(rc, 2)
        self.assertIn("only the orchestrator", out["error"])

    def test_broadcast_with_no_live_workers(self):
        worker = self.launch()
        self.set_state(status=None, statuses={worker["name"]: None})
        rc, out = self.nakama("send", "@all", "info", self.msg())
        self.assertEqual(rc, 0)
        self.assertEqual(out["status"], "no_recipients")
        self.assertEqual(out["results"], [])

    def test_broadcast_continues_after_inbox_full(self):
        full, available = self.launch(), self.launch()
        full_box = Path(full["dir"]) / "inbox"
        full_box.mkdir()
        for i in range(20):
            (full_box / f"{i}.orchestrator.info.md").write_text("full")
        self.set_state(status=None, statuses={full["name"]: "idle", available["name"]: "idle"})
        rc, out = self.nakama("send", "@all", "info", self.msg("continue"))
        self.assertEqual(rc, 0)
        self.assertEqual([r["status"] for r in out["results"]], ["inbox_full", "sent"])
        self.assertEqual(self.nakama("inbox", available["name"])[1]["messages"][0]["text"], "continue")

    def test_broadcast_continues_after_push_failure(self):
        failed_push, successful_push = self.launch(), self.launch()
        self.set_state(status=None, statuses={failed_push["name"]: "working",
                                             successful_push["name"]: "working"},
                       prompt_fail=[failed_push["name"]])
        rc, out = self.nakama("send", "@all", "info", self.msg("continue"))
        self.assertEqual(rc, 0)
        self.assertEqual([r["pushed"] for r in out["results"]], [False, True])
        self.assertEqual(self.nakama("inbox", successful_push["name"])[1]["messages"][0]["text"], "continue")

    def test_broadcast_id_is_idempotent_per_recipient(self):
        first, second = self.launch(), self.launch()
        self.set_state(status=None, statuses={first["name"]: "idle", second["name"]: "idle"})
        args = ("send", "@all", "info", self.msg("once"), "--id", "retry_1")
        rc, sent = self.nakama(*args)
        self.assertEqual((rc, [r["status"] for r in sent["results"]]), (0, ["sent", "sent"]))
        rc, retry = self.nakama(*args)
        self.assertEqual((rc, [r["status"] for r in retry["results"]]), (0, ["duplicate", "duplicate"]))
        for worker in (first, second):
            _, box = self.nakama("inbox", worker["name"])
            self.assertEqual([m["text"] for m in box["messages"]], ["once"])

    def test_send_to_unknown_agent_fails(self):
        self.assertEqual(self.nakama("send", "nobody", "info", self.msg())[0], 2)

    def test_help_describes_every_command(self):
        text = self.help_text()
        self.assertNotIn("{launch,", text)
        for command in ("launch", "resume", "prompt", "wait", "send", "inbox", "extend", "pause", "teardown", "list", "sweep", "roles", "doctor", "init"):
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
        self.assertEqual(tab[tab.index("--workspace") + 1], "w3")
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
        split = next(c for c in self.calls() if c[1:3] == ["pane", "split"])
        self.assertEqual(split[split.index("--pane") + 1], "w3:p1")
        self.assert_backstop(split)
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

    def test_resume_after_teardown_reuses_record_and_reprimes_task(self):
        out = self.launch()
        self.task(out)
        stale_result = Path(self.result(out))
        stale_result.write_text("Status: succeeded" + chr(10) + "Next: review" + chr(10))
        old = time.time() - 600
        os.utime(stale_result, (old, old))
        record = Path(self.env["NAKAMA_STATE_DIR"]) / f"{out['name']}.json"
        rec = json.loads(record.read_text())
        rec.update(rounds=1, budget_min=73, extended_min=15, prompted_at=1, nudged_at=2,
                   screen_sig="old", screen_changed_at=3)
        record.write_text(json.dumps(rec))
        self.nakama("teardown", out["name"])

        rc, resumed = self.nakama("resume", out["name"])
        self.assertEqual(rc, 0, resumed)
        self.assertEqual(resumed["status"], "fresh_primed")
        self.assertEqual(resumed["rounds"], 1)
        rec = json.loads(record.read_text())
        self.assertFalse(rec["closed"])
        self.assertEqual((rec["rounds"], rec["budget_min"]), (1, 73))
        self.assertEqual(rec["extended_min"], 15)  # resume keeps the extensions it was granted
        self.assertIsNotNone(rec["prompted_at"])
        self.assertIsNone(rec["nudged_at"])
        self.assertIsNone(rec["screen_sig"])
        self.assertIsNone(rec["screen_changed_at"])
        prompt = next(c for c in self.calls() if c[1:3] == ["agent", "prompt"])
        self.assertIn("task.md", prompt[-1])
        self.assertIn("fix-*.md", prompt[-1])
        self.assertIn("git log", prompt[-1])
        self.assertIn("git status", prompt[-1])
        self.assertEqual(self.nakama("wait", out["name"], "--timeout", "0")[1]["status"], "failed")

    def test_resume_retires_pause_file(self):
        out = self.launch()
        self.task(out)
        self.nakama("prompt", out["name"], "task.md")
        self.set_state(status="working")
        rc, paused = self.nakama("pause", out["name"], "--timeout", "0")
        self.assertEqual(rc, 0, paused)
        self.assertEqual(paused["status"], "paused_forced")
        pause_file = Path(out["dir"], "pause.md")
        self.assertTrue(pause_file.is_file())
        self.nakama("teardown", out["name"])

        rc, resumed = self.nakama("resume", out["name"])
        self.assertEqual((rc, resumed["status"]), (0, "fresh_primed"))
        self.assertFalse(pause_file.exists())
        self.assertEqual(len(list(pause_file.parent.glob("pause-*.done.md"))), 1)
        prompts = [c for c in self.calls() if c[1:3] == ["agent", "prompt"]]
        self.assertIn("The earlier pause is over", prompts[-1][-1])

    def test_resume_marks_pause_mail_read(self):
        out = self.launch()
        self.task(out)
        self.nakama("prompt", out["name"], "task.md")
        self.set_state(status="working")
        rc, paused = self.nakama("pause", out["name"], "--timeout", "0")
        self.assertEqual((rc, paused["status"]), (0, "paused_forced"))
        inbox = Path(out["dir"], "inbox")
        pause_mail = next(mail for mail in inbox.glob("*.md") if mail.read_text().startswith("Pause now."))

        self.set_state(status=None)
        rc, unrelated = self.nakama("send", out["name"], "info", self.msg("Unrelated notification"))
        self.assertEqual((rc, unrelated["status"]), (0, "sent"))
        unrelated_mail = Path(unrelated["file"])

        rc, resumed = self.nakama("resume", out["name"])
        self.assertEqual((rc, resumed["status"]), (0, "fresh_primed"))
        read_pause_mail = pause_mail.with_suffix(".read")
        self.assertFalse(pause_mail.exists())
        self.assertTrue(read_pause_mail.is_file())
        self.assertTrue(read_pause_mail.read_text().startswith("Pause now."))
        self.assertTrue(unrelated_mail.is_file())
        self.assertFalse(unrelated_mail.with_suffix(".read").exists())
        self.assertFalse(any(mail.read_text().startswith("Pause now.") for mail in inbox.glob("*.md")))

    def test_resume_without_pause_has_no_pause_note(self):
        out = self.launch()
        self.task(out)
        self.nakama("teardown", out["name"])

        rc, resumed = self.nakama("resume", out["name"])
        self.assertEqual((rc, resumed["status"]), (0, "fresh_primed"))
        prompts = [c for c in self.calls() if c[1:3] == ["agent", "prompt"]]
        self.assertNotIn("The earlier pause is over", prompts[-1][-1])

    def test_resume_cleans_up_old_surface_and_gh_config_for_dead_workers(self):
        for layout in ("tab", "split"):
            out = self.launch("--layout", layout)
            self.task(out)
            record = Path(self.env["NAKAMA_STATE_DIR"]) / f"{out['name']}.json"
            old = json.loads(record.read_text())
            old_gh = Path(old["gh_config_dir"])
            self.assertTrue(old_gh.is_dir())
            self.set_state(status=None)

            rc, resumed = self.nakama("resume", out["name"])
            self.assertEqual((rc, resumed["status"]), (0, "fresh_primed"))
            close_call = ["herdr", "tab", "close", old["tab"]] if layout == "tab" else [
                "herdr", "pane", "close", old["pane"]]
            self.assertIn(close_call, self.calls())
            self.assertFalse(old_gh.exists())
            new = json.loads(record.read_text())
            self.assertNotEqual(new["gh_config_dir"], old["gh_config_dir"])
            self.assertTrue(Path(new["gh_config_dir"]).is_dir())

    def test_resume_live_worker_does_nothing(self):
        out = self.launch()
        self.task(out)
        self.nakama("prompt", out["name"], "task.md")
        before = self.calls()
        record = Path(self.env["NAKAMA_STATE_DIR"]) / f"{out['name']}.json"
        before_record = record.read_text()
        rc, result = self.nakama("resume", out["name"])
        self.assertEqual((rc, result["status"]), (0, "already_live"))
        self.assertFalse(any(c[1:3] in (["tab", "create"], ["pane", "split"], ["agent", "start"], ["agent", "prompt"])
                             for c in self.calls()[len(before):]))
        self.assertEqual(record.read_text(), before_record)

    def test_resume_requires_existing_task_and_worktree(self):
        out = self.launch()
        task = self.task(out)
        task.unlink()
        rc, missing_task = self.nakama("resume", out["name"])
        self.assertEqual(rc, 2)
        self.assertIn("task.md", missing_task["error"])

        task.write_text("## Goal" + chr(10) + "x" + chr(10))
        record = Path(self.env["NAKAMA_STATE_DIR"]) / f"{out['name']}.json"
        rec = json.loads(record.read_text())
        rec["worktree"] = str(self.root / "gone-worktree")
        record.write_text(json.dumps(rec))
        rc, missing_tree = self.nakama("resume", out["name"])
        self.assertEqual(rc, 2)
        self.assertIn("worktree", missing_tree["error"])

    def test_resume_agent_start_failure_is_blocked(self):
        out = self.launch()
        self.task(out)
        self.nakama("teardown", out["name"])
        self.set_state(status="idle", start_rc=1)
        rc, result = self.nakama("resume", out["name"])
        self.assertEqual(rc, 0)
        self.assertEqual(result["status"], "blocked")
        self.assertIn("SCREEN", result["screen"])
        self.assertFalse(json.loads((Path(self.env["NAKAMA_STATE_DIR"]) / f"{out['name']}.json").read_text())["closed"])

    def test_resume_prompt_failure_can_be_retried_without_restarting_live_worker(self):
        out = self.launch()
        self.task(out)
        self.nakama("teardown", out["name"])
        self.set_state(status="idle", prompt_fail=[out["name"]])

        rc, failed = self.nakama("resume", out["name"])
        self.assertEqual((rc, failed["status"]), (1, "prompt_failed"))
        record = Path(self.env["NAKAMA_STATE_DIR"]) / f"{out['name']}.json"
        rec = json.loads(record.read_text())
        self.assertFalse(rec["closed"])
        self.assertIsNone(rec["prompted_at"])
        starts = sum(c[1:3] == ["agent", "start"] for c in self.calls())
        surfaces = sum(c[1:3] in (["tab", "create"], ["pane", "split"]) for c in self.calls())

        self.set_state(status="idle", prompt_fail=[])
        rc, retried = self.nakama("resume", out["name"])
        self.assertEqual((rc, retried["status"]), (0, "fresh_primed"))
        self.assertEqual(sum(c[1:3] == ["agent", "start"] for c in self.calls()), starts)
        self.assertEqual(sum(c[1:3] in (["tab", "create"], ["pane", "split"]) for c in self.calls()), surfaces)
        self.assertIsNotNone(json.loads(record.read_text())["prompted_at"])

    def test_resume_obeys_parent_spawn_permission(self):
        out = self.launch()
        self.task(out)
        self.nakama("teardown", out["name"])
        parent = self.launch()
        before = sum(c[1:3] == ["tab", "create"] for c in self.calls())
        rc, result = self.nakama("resume", out["name"], env={**self.env, "NAKAMA_AGENT": parent["name"]})
        self.assertEqual(rc, 2)
        self.assertIn("spawns", result["error"])
        self.assertEqual(sum(c[1:3] == ["tab", "create"] for c in self.calls()), before)

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

    def test_join_refuses_same_kind_and_model_as_builder(self):
        builder = self.launch("--model", "m1")
        rc, err = self.nakama("launch", "reviewer", "r", "--kind", "pi", "--model", "m1", "--join", builder["name"])
        self.assertEqual(rc, 2)
        self.assertIn("--model", err["error"])
        rc, _ = self.nakama("launch", "reviewer", "r", "--kind", "pi", "--model", "m2", "--join", builder["name"])
        self.assertEqual(rc, 0)

    def test_join_refuses_when_both_models_are_the_cli_default(self):
        builder = self.launch()
        rc, err = self.nakama("launch", "reviewer", "r", "--kind", "pi", "--join", builder["name"])
        self.assertEqual(rc, 2)
        self.assertIn("joined worker", err["error"])

    def wgit(self, worktree, *args):
        return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=worktree, check=True,
                              capture_output=True, text=True, env=self.env).stdout.strip()

    def stepped(self, trailers, base_trailers=()):
        self.wgit(self.root, "init", "-q", "-b", "main")
        self.wgit(self.root, "commit", "-q", "--allow-empty", "-m", "root")
        out = self.launch()
        worktree = out["worktree"]
        self.wgit(worktree, "init", "-q", "-b", "main")
        self.wgit(worktree, "commit", "-q", "--allow-empty", "-m", "root")
        for i, trailer in enumerate(base_trailers):
            self.wgit(worktree, "commit", "-q", "--allow-empty", "-m", f"base {i}",
                      "--trailer", f"Nakama-Step: {trailer}")
        self.wgit(worktree, "checkout", "-q", "-b", "work")
        for i, trailer in enumerate(trailers):
            self.wgit(worktree, "commit", "-q", "--allow-empty", "-m", f"step {i}",
                      "--trailer", f"Nakama-Step: {trailer}")
        Path(out["dir"], "task.md").write_text(
            "## Goal\nx\n## Steps\n1. a — check: true\n2. b — check: true\n3. c — check: true\n## Acceptance\n- true\n")
        return out

    def read_review(self, prepare=None):
        """A read-only reviewer joined to a builder whose worktree is a real git repo with one tracked file."""
        builder = self.launch()
        wt = builder["worktree"]
        self.wgit(wt, "init", "-q")
        Path(wt, "tracked.txt").write_text("v1")
        self.wgit(wt, "add", "tracked.txt")
        self.wgit(wt, "commit", "-q", "-m", "a")
        if prepare:
            prepare(wt)
        rev = self.nakama("launch", "reviewer", "r", "--join", builder["name"])[1]
        self.task(rev)
        self.set_state(status="idle", write_on_prompt=self.write_result(rev))
        self.nakama("prompt", rev["name"], "task.md")
        return builder, rev

    def write_result(self, rev):
        return {"path": self.result(rev), "text": "Status: succeeded\nNext: merge\n"}

    def settle(self, rev):
        return self.nakama("wait", rev["name"], "--timeout", "5")[1]

    def record(self, name):
        return json.loads((self.root / "records" / f"{name}.json").read_text())

    def test_wait_receipt_is_fixed_at_first_settle(self):
        builder, rev = self.read_review()
        wt = builder["worktree"]
        head = self.wgit(wt, "rev-parse", "HEAD")
        Path(rev["dir"], "notes.md").write_text("ok")  # inside .agent/: not tampering
        first = self.settle(rev)
        self.assertEqual((first["status"], first["reviewed_head"]), ("succeeded", head))
        self.assertNotIn("stale", first)
        # The builder moves on (fix round, commit, stray file): the stored receipt is not re-judged.
        Path(wt, "oops.txt").write_text("x")
        self.wgit(wt, "add", "-A")
        self.wgit(wt, "commit", "-q", "-m", "b")
        again = self.settle(rev)
        self.assertEqual((again["status"], again["reviewed_head"]), ("succeeded", head))
        self.assertEqual(self.record(rev["name"])["receipt"]["head_at_result"], head)

    def assert_tampered(self, rev):
        out = self.settle(rev)
        self.assertEqual(out["status"], "tampered")
        self.assertIn("outside .agent/", out["reason"])
        self.assertEqual(self.settle(rev)["status"], "tampered")

    def test_wait_tampered_when_read_worker_adds_file_outside_agent_dir(self):
        builder, rev = self.read_review()
        Path(builder["worktree"], "oops.txt").write_text("x")
        self.assert_tampered(rev)

    def test_wait_tampered_when_read_worker_edits_already_modified_file(self):
        builder, rev = self.read_review(lambda wt: Path(wt, "tracked.txt").write_text("v2"))
        Path(builder["worktree"], "tracked.txt").write_text("v3")
        self.assert_tampered(rev)

    def test_wait_tampered_when_read_worker_adds_file_in_untracked_dir(self):
        def prepare(wt):
            Path(wt, "d").mkdir()
            Path(wt, "d", "a.txt").write_text("1")
        builder, rev = self.read_review(prepare)
        Path(builder["worktree"], "d", "b.txt").write_text("2")
        self.assert_tampered(rev)

    def test_wait_ignores_stash_created_on_another_branch(self):
        builder, rev = self.read_review()
        other = Path(builder["worktree"]).parent / "other-branch"
        self.wgit(builder["worktree"], "worktree", "add", "-b", "other-branch", str(other), "HEAD")
        Path(other, "tracked.txt").write_text("other branch change")
        self.wgit(other, "stash", "push", "-m", "nakama-test-stash")
        self.assertEqual(self.settle(rev)["status"], "succeeded")

    def test_wait_ignores_other_branch_stash_reordering_own_stash(self):
        def prepare(wt):
            Path(wt, "tracked.txt").write_text("reviewer branch change")
            self.wgit(wt, "stash", "push", "-m", "reviewer-branch-stash")

        builder, rev = self.read_review(prepare)
        other = Path(builder["worktree"]).parent / "other-branch"
        self.wgit(builder["worktree"], "worktree", "add", "-b", "other-branch", str(other), "HEAD")
        Path(other, "tracked.txt").write_text("other branch change")
        self.wgit(other, "stash", "push", "-m", "other-branch-stash")
        self.assertEqual(self.settle(rev)["status"], "succeeded")

    def test_wait_tampered_when_read_worker_changes_stash(self):
        builder, rev = self.read_review()
        Path(builder["worktree"], "tracked.txt").write_text("v2")
        self.wgit(builder["worktree"], "stash", "push", "-m", "nakama-test-stash")
        self.assert_tampered(rev)

    def test_wait_tampered_when_read_worker_commits(self):
        builder, rev = self.read_review()
        self.wgit(builder["worktree"], "commit", "-q", "--allow-empty", "-m", "by reviewer")
        self.assert_tampered(rev)

    def test_wait_skips_check_without_git_baseline(self):
        builder, rev = self.read_review()
        rec = self.record(rev["name"])
        del rec["git_before"]
        (self.root / "records" / f"{rev['name']}.json").write_text(json.dumps(rec))
        Path(builder["worktree"], "oops.txt").write_text("x")
        out = self.settle(rev)
        self.assertEqual(out["status"], "succeeded")
        self.assertNotIn("reviewed_head", out)

    def test_resume_keeps_read_worker_baseline(self):
        builder, rev = self.read_review()
        Path(builder["worktree"], "oops.txt").write_text("x")
        self.set_state(status=None, write_on_prompt=self.write_result(rev))
        self.assertEqual(self.nakama("resume", rev["name"])[1]["status"], "fresh_primed")
        self.set_state(status="idle", write_on_prompt=self.write_result(rev))
        self.assertEqual(self.settle(rev)["status"], "tampered")

    def test_wait_returns_fresh_result_status(self):
        out = self.launch()
        self.task(out)
        self.set_state(status="idle",
                       write_on_prompt={"path": self.result(out), "text": "Status: succeeded\n## Summary\nok\n"})
        self.assertEqual(self.nakama("prompt", out["name"], "task.md")[1]["status"], "prompted")
        self.set_state(status="working", after_wait_status="idle")
        self.assertEqual(self.nakama("wait", out["name"], "--timeout", "5")[1]["status"], "succeeded")

    def wait_with_result(self, text):
        out = self.launch()
        self.task(out)
        self.set_state(status="idle", write_on_prompt={"path": self.result(out), "text": text})
        self.nakama("prompt", out["name"], "task.md")
        return self.nakama("wait", out["name"], "--timeout", "5")[1]

    def test_launch_records_current_branch_as_base_when_base_is_head(self):
        self.wgit(self.root, "init", "-q", "-b", "main")
        self.wgit(self.root, "commit", "-q", "--allow-empty", "-m", "root")
        out = self.launch()
        self.assertEqual(self.record(out["name"])["base"], "main")

    def test_launch_records_explicit_base_and_none_for_join(self):
        out = self.launch("--base", "develop")
        self.assertEqual(self.record(out["name"])["base"], "develop")
        rev = self.nakama("launch", "reviewer", "r", "--join", out["name"])[1]
        self.assertIsNone(self.record(rev["name"]).get("base"))

    def test_wait_reports_steps_from_trailers_and_result(self):
        out = self.stepped(["1/3", "2/3"])
        text = "Status: partial\nSteps: 2/3\n"
        self.set_state(status="idle", write_on_prompt={"path": self.result(out), "text": text})
        self.nakama("prompt", out["name"], "task.md")
        result = self.nakama("wait", out["name"], "--timeout", "5")[1]
        self.assertEqual(result["steps"], {"done": 2, "total": 3, "reported": "2/3"})
        self.assertNotIn("steps_mismatch", result.get("warnings", []))

    def test_wait_warns_on_steps_mismatch(self):
        out = self.stepped(["1/3"])
        self.set_state(status="idle", write_on_prompt={
            "path": self.result(out), "text": "Status: partial\nSteps: 2/3\n"})
        self.nakama("prompt", out["name"], "task.md")
        result = self.nakama("wait", out["name"], "--timeout", "5")[1]
        self.assertIn("steps_mismatch", result["warnings"])

    def test_steps_count_ignores_base_commits_with_trailers(self):
        out = self.stepped(["2/3"], base_trailers=["1/3"])
        self.set_state(status="idle", write_on_prompt={"path": self.result(out), "text": "Status: partial\n"})
        self.nakama("prompt", out["name"], "task.md")
        result = self.nakama("wait", out["name"], "--timeout", "5")[1]
        self.assertEqual(result["steps"]["done"], 1)

    def test_wait_without_steps_section_has_no_steps_key(self):
        result = self.wait_with_result("Status: succeeded\nNext: review\n")
        self.assertNotIn("steps", result)

    def test_prompt_task_adds_step_progress(self):
        out = self.stepped(["1/3"])
        self.nakama("prompt", out["name"], "task.md")
        prompt = [c for c in self.calls() if c[1:3] == ["agent", "prompt"]][-1]
        self.assertIn("Steps 1..1 are committed", prompt[-1])
        self.assertIn("continue at step 2 of ## Steps", prompt[-1])

    def test_prompt_task_reports_all_steps_complete(self):
        out = self.stepped(["1/3", "2/3", "3/3"])
        self.nakama("prompt", out["name"], "task.md")
        prompt = [c for c in self.calls() if c[1:3] == ["agent", "prompt"]][-1]
        self.assertIn("All 3 steps are committed (Nakama-Step trailers); run the full ## Acceptance.", prompt[-1])
        self.assertNotIn("step 4", prompt[-1])

    def test_prompt_fix_file_has_no_step_progress(self):
        out = self.stepped(["1/3"])
        Path(out["dir"], "fix-1.md").write_text("## Goal\nfix\n")
        self.nakama("prompt", out["name"], "fix-1.md")
        prompt = [c for c in self.calls() if c[1:3] == ["agent", "prompt"]][-1]
        self.assertNotIn("continue at step", prompt[-1])

    def test_resume_mentions_dirty_tree_and_steps(self):
        out = self.stepped(["1/3", "2/3"])
        tracked = Path(out["worktree"], "tracked.txt")
        tracked.write_text("clean\n")
        self.wgit(out["worktree"], "add", "tracked.txt")
        self.wgit(out["worktree"], "commit", "-q", "-m", "track file")
        tracked.write_text("dirty\n")
        self.nakama("teardown", out["name"])
        self.set_state(status=None)
        self.nakama("resume", out["name"])
        prompt = [c for c in self.calls() if c[1:3] == ["agent", "prompt"]][-1]
        self.assertIn("continue at step 3", prompt[-1])
        self.assertIn("uncommitted changes", prompt[-1])

    def test_resume_ignores_agent_dir_when_checking_dirty(self):
        out = self.stepped(["1/3"])
        self.nakama("teardown", out["name"])
        self.set_state(status=None)
        self.nakama("resume", out["name"])
        prompt = [c for c in self.calls() if c[1:3] == ["agent", "prompt"]][-1]
        self.assertNotIn("uncommitted changes", prompt[-1])

    def test_resume_read_reviewer_ignores_builder_dirty_tree(self):
        builder, reviewer = self.read_review()
        Path(builder["worktree"], "tracked.txt").write_text("dirty\n")
        self.nakama("teardown", reviewer["name"])
        self.set_state(status=None)
        self.nakama("resume", reviewer["name"])
        prompt = [c for c in self.calls() if c[1:3] == ["agent", "prompt"]][-1]
        self.assertNotIn("uncommitted changes", prompt[-1])

    def test_wait_returns_valid_next_hop(self):
        result = self.wait_with_result("Status: succeeded\nNext: review\n")
        self.assertEqual((result["status"], result["next"]), ("succeeded", "review"))
        self.assertNotIn("warnings", result)

    def test_wait_accepts_bold_status_and_next_lines(self):
        result = self.wait_with_result("**Status: succeeded**\n**Next: none**\n")
        self.assertEqual((result["status"], result["next"]), ("succeeded", "none"))

    def test_succeeded_without_next_hop_warns(self):
        result = self.wait_with_result("Status: succeeded\n")
        self.assertEqual(result["warnings"], ["no_next_hop"])
        self.assertNotIn("next", result)

    def test_unknown_next_hop_warns(self):
        result = self.wait_with_result("Status: succeeded\nNext: unknown\n")
        self.assertEqual(result["warnings"], ["no_next_hop"])
        self.assertNotIn("next", result)

    def test_failed_without_next_hop_has_no_warning(self):
        result = self.wait_with_result("Status: failed\n")
        self.assertEqual(result["status"], "failed")
        self.assertNotIn("warnings", result)

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
        result = self.nakama("wait", out["name"], "--timeout", "5")[1]
        self.assertEqual(result["status"], "malformed")
        self.assertEqual(result["hint"], "Status must be exactly one of succeeded, partial, failed, needs_input")

    def test_result_with_unknown_status_is_malformed_with_enum_hint(self):
        result = self.wait_with_result("Status: done\n")
        self.assertEqual(result["status"], "malformed")
        self.assertEqual(result["hint"],
                         'Status must be exactly one of succeeded, partial, failed, needs_input (got "done")')

    def test_wait_accepts_trailing_text_after_status_value(self):
        for text, expected in (("Status: succeeded.\n", "succeeded"),
                               ("Status: succeeded — all green\n", "succeeded"),
                               ("Status: partial (2/4)\n", "partial")):
            with self.subTest(text=text):
                result = self.wait_with_result(text)
                self.assertEqual(result["status"], expected)
                self.assertNotIn("hint", result)

    def test_prompt_includes_required_result_status_enum(self):
        out = self.launch()
        self.task(out)
        self.nakama("prompt", out["name"], "task.md")
        prompt = [c for c in self.calls() if c[1:3] == ["agent", "prompt"]][-1]
        self.assertIn("Status exactly one of succeeded, partial, failed, needs_input", prompt[-1])

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

    def test_over_budget_counts_banked_spent_min(self):
        out = self.launch()
        self.task(out)
        self.nakama("prompt", out["name"], "task.md")
        self.set_state(status="working")
        record = self.root / "records" / f"{out['name']}.json"
        rec = json.loads(record.read_text())
        rec["spent_min"] = 46  # over the role's 45-minute budget on its own
        record.write_text(json.dumps(rec))
        self.assertEqual(self.nakama("wait", out["name"], "--timeout", "5")[1]["status"], "over_budget")

    def test_pause_banks_spent_min_and_resume_keeps_budget(self):
        out = self.launch()
        self.task(out)
        self.nakama("prompt", out["name"], "task.md")
        self.set_state(status="working")
        self.nakama("extend", out["name"], "15")
        record = self.root / "records" / f"{out['name']}.json"
        rec = json.loads(record.read_text())
        rec["prompted_at"] -= 10 * 60
        record.write_text(json.dumps(rec))
        self.assertEqual(self.nakama("pause", out["name"], "--timeout", "0")[1]["status"],
                         "paused_forced")
        self.assertGreaterEqual(self.record(out["name"])["spent_min"], 9.9)
        self.assertEqual(self.nakama("resume", out["name"])[1]["status"], "fresh_primed")
        rec = self.record(out["name"])
        self.assertEqual(rec["extended_min"], 15)
        self.assertGreaterEqual(rec["spent_min"], 9.9)

    def test_prompt_resets_spent_min(self):
        out = self.launch()
        self.task(out)
        self.nakama("prompt", out["name"], "task.md")
        record = self.root / "records" / f"{out['name']}.json"
        rec = json.loads(record.read_text())
        rec.update(spent_min=12, extended_min=5)
        record.write_text(json.dumps(rec))
        self.task(out, "fix-1.md")
        self.assertEqual(self.nakama("prompt", out["name"], "fix-1.md")[1]["status"], "prompted")
        rec = self.record(out["name"])
        self.assertEqual((rec["spent_min"], rec["extended_min"]), (0, 0))

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

    def test_wait_reports_stalled_after_unchanged_screen(self):
        out = self.launch()
        self.task(out)
        self.nakama("prompt", out["name"], "task.md")
        screen = "Thinking 12:34 ⠋\nGenerating 99%"
        self.set_state(status="working", screen=screen)
        env = {**self.env, "NAKAMA_STALL_SEC": "0"}
        rc, result = self.nakama("wait", out["name"], "--timeout", "5", env=env)
        self.assertEqual(rc, 0)
        self.assertEqual(result["status"], "stalled")
        self.assertEqual(result["screen"], screen + "\n")
        self.assertGreaterEqual(result["idle_seconds"], 0)

    def test_wait_stalls_when_only_digits_and_spinner_change(self):
        out = self.launch()
        self.task(out)
        self.nakama("prompt", out["name"], "task.md")
        self.set_state(status="working", screen="Thinking 12:34 ⠋\nGenerating 99%")
        env = {**self.env, "NAKAMA_STALL_SEC": "0"}
        self.assertEqual(self.nakama("wait", out["name"], "--timeout", "5", env=env)[1]["status"], "stalled")
        screen = "Thinking 87:65 ⠙\nGenerating 10%"
        self.set_state(status="working", screen=screen)
        _, result = self.nakama("wait", out["name"], "--timeout", "5", env=env)
        self.assertEqual(result["status"], "stalled")
        self.assertEqual(result["screen"], screen + "\n")

    def test_wait_not_stalled_when_output_scrolls_above_fixed_footer(self):
        out = self.launch()
        self.task(out)
        self.nakama("prompt", out["name"], "task.md")
        footer = """Todos 1/4
✓ Read the task
▶ Implement the change
○ Run focused tests
○ Write the final report
── ⠙ Working ─────────────────────────
──────────────────────────────────────
~/work (branch)
↑96 ↓47k R860k W248k CH26.9% $0.063 18.5%/272k (auto) (provider) model • max
provider · model · thinking max · session $0.06"""
        first = "Changing output line one\n" + footer
        second = "Changing output line two\n" + footer
        env = {**self.env, "NAKAMA_STALL_SEC": "0"}
        self.set_state(status="working", screen=first)
        self.assertEqual(self.nakama("wait", out["name"], "--timeout", "0", env=env)[1]["status"], "running")
        record = self.root / "records" / f"{out['name']}.json"
        before = json.loads(record.read_text())

        self.set_state(status="working", screen=second)
        _, result = self.nakama("wait", out["name"], "--timeout", "0", env=env)
        after = json.loads(record.read_text())
        self.assertEqual(result["status"], "running")
        self.assertNotEqual(after["screen_sig"], before["screen_sig"])

    def test_changed_screen_words_reset_stall_timer(self):
        out = self.launch()
        self.task(out)
        self.nakama("prompt", out["name"], "task.md")
        env = {**self.env, "NAKAMA_STALL_SEC": "0"}
        self.set_state(status="working", screen="Thinking through solution")
        self.assertEqual(self.nakama("wait", out["name"], "--timeout", "0", env=env)[1]["status"], "running")
        record = self.root / "records" / f"{out['name']}.json"
        before = json.loads(record.read_text())
        self.set_state(status="working", screen="Editing the implementation")
        _, result = self.nakama("wait", out["name"], "--timeout", "0", env=env)
        after = json.loads(record.read_text())
        self.assertEqual(result["status"], "running")
        self.assertNotEqual(after["screen_sig"], before["screen_sig"])
        self.assertGreaterEqual(after["screen_changed_at"], before["screen_changed_at"])

    def test_fresh_prompt_resets_stall_tracking(self):
        out = self.launch()
        self.task(out)
        self.task(out, "fix-1.md")
        self.nakama("prompt", out["name"], "task.md")
        env = {**self.env, "NAKAMA_STALL_SEC": "0"}
        self.set_state(status="working", screen="Same visible screen")
        self.assertEqual(self.nakama("wait", out["name"], "--timeout", "0", env=env)[1]["status"], "running")
        self.set_state(status="idle", screen="Same visible screen")
        self.nakama("prompt", out["name"], "fix-1.md")
        record = self.root / "records" / f"{out['name']}.json"
        rec = json.loads(record.read_text())
        self.assertIsNone(rec["screen_sig"])
        self.assertIsNone(rec["screen_changed_at"])
        self.set_state(status="working", screen="Same visible screen")
        _, result = self.nakama("wait", out["name"], "--timeout", "0", env=env)
        self.assertEqual(result["status"], "running")

    def test_wait_does_not_track_screens_when_not_working(self):
        out = self.launch()
        self.task(out)
        self.nakama("prompt", out["name"], "task.md")
        self.set_state(status="blocked", screen="Blocked screen")
        _, result = self.nakama("wait", out["name"], "--timeout", "5",
                                env={**self.env, "NAKAMA_STALL_SEC": "0"})
        self.assertEqual(result["status"], "blocked")
        rec = json.loads((self.root / "records" / f"{out['name']}.json").read_text())
        self.assertIsNone(rec["screen_sig"])
        self.assertIsNone(rec["screen_changed_at"])

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

    def test_pause_waits_for_note_then_closes_and_keeps_worktree(self):
        out = self.launch()
        self.task(out)
        self.nakama("prompt", out["name"], "task.md")
        self.set_state(status="idle", write_on_prompt={
            "path": self.result(out), "text": "Status: partial\n## Resume\nfinish the current edit\n"})
        rc, result = self.nakama("pause", out["name"], "--timeout", "5")
        self.assertEqual(rc, 0, result)
        self.assertEqual(result["status"], "paused")
        self.assertEqual(result, {"name": out["name"], "status": "paused", "steps": None, "dirty": False})
        rec = self.record(out["name"])
        self.assertTrue(rec["closed"] and rec["paused"])
        self.assertFalse(rec["paused_forced"])
        self.assertIn(["herdr", "tab", "close", rec["tab"]], self.calls())
        self.assertFalse(any(c[0] == "wt" and c[1] == "remove" for c in self.calls()))
        pause_message = Path(out["dir"], "pause.md").read_text()
        self.assertIn("Do not commit", pause_message)
        self.assertIn("Next: retry", pause_message)

    def test_pause_reports_finished_for_clean_non_partial_note(self):
        out = self.launch()
        worktree = out["worktree"]
        self.wgit(worktree, "init", "-q", "-b", "main")
        Path(worktree, "tracked.txt").write_text("clean")
        self.wgit(worktree, "add", "tracked.txt")
        self.wgit(worktree, "commit", "-q", "-m", "baseline")
        self.task(out)
        self.nakama("prompt", out["name"], "task.md")
        self.set_state(status="idle", write_on_prompt={
            "path": self.result(out), "text": "Status: succeeded"})
        rc, result = self.nakama("pause", out["name"], "--timeout", "5")
        self.assertEqual(rc, 0, result)
        self.assertEqual(result, {
            "name": out["name"], "status": "finished", "result_status": "succeeded",
            "steps": None, "dirty": False})
        rec = self.record(out["name"])
        self.assertTrue(rec["closed"])
        self.assertFalse(rec["paused"])
        self.assertFalse(rec["paused_forced"])
        self.assertEqual(self.nakama("sweep")[1]["paused_workers"], [])
        rc, teardown = self.nakama("teardown", out["name"], "--remove-worktree")
        self.assertEqual(rc, 0, teardown)
        self.assertEqual(teardown["status"], "closed")
        self.assertTrue(teardown["worktree_removed"])

    def test_pause_keeps_non_partial_note_paused_when_worktree_dirty(self):
        out = self.launch()
        worktree = out["worktree"]
        self.wgit(worktree, "init", "-q", "-b", "main")
        Path(worktree, "tracked.txt").write_text("clean")
        self.wgit(worktree, "add", "tracked.txt")
        self.wgit(worktree, "commit", "-q", "-m", "baseline")
        self.task(out)
        self.nakama("prompt", out["name"], "task.md")
        Path(worktree, "tracked.txt").write_text("uncommitted")
        self.set_state(status="idle", write_on_prompt={
            "path": self.result(out), "text": "Status: succeeded"})
        rc, result = self.nakama("pause", out["name"], "--timeout", "5")
        self.assertEqual(rc, 0, result)
        self.assertEqual(result["status"], "paused")
        self.assertTrue(result["dirty"])
        rec = self.record(out["name"])
        self.assertTrue(rec["closed"] and rec["paused"])
        self.assertFalse(rec["paused_forced"])

    def test_pause_keeps_note_without_valid_status_paused(self):
        out = self.launch()
        self.task(out)
        self.nakama("prompt", out["name"], "task.md")
        self.set_state(status="idle", write_on_prompt={
            "path": self.result(out), "text": "No valid Status line"})
        rc, result = self.nakama("pause", out["name"], "--timeout", "5")
        self.assertEqual(rc, 0, result)
        self.assertEqual(result["status"], "paused")
        rec = self.record(out["name"])
        self.assertTrue(rec["closed"] and rec["paused"])
        self.assertFalse(rec["paused_forced"])

    def test_pause_all_keeps_paused_summary_when_worker_finishes(self):
        out = self.launch()
        self.task(out)
        self.nakama("prompt", out["name"], "task.md")
        self.set_state(status="idle", write_on_prompt={
            "path": self.result(out), "text": "Status: succeeded"})
        rc, result = self.nakama("pause", "--all", "--timeout", "5")
        self.assertEqual(rc, 0, result)
        self.assertEqual(result["status"], "paused")
        self.assertEqual(result["results"][0]["status"], "finished")

    def test_pause_forced_when_no_note_before_timeout(self):
        out = self.launch()
        self.task(out)
        self.nakama("prompt", out["name"], "task.md")
        self.set_state(status="working")
        rc, result = self.nakama("pause", out["name"], "--timeout", "0")
        self.assertEqual(rc, 0, result)
        self.assertEqual(result["status"], "paused_forced")
        rec = self.record(out["name"])
        self.assertTrue(rec["paused_forced"])
        self.assertIn(["herdr", "tab", "close", rec["tab"]], self.calls())
        self.assertTrue(Path(out["worktree"]).is_dir())
        self.assertFalse(any(c[0] == "wt" and c[1] == "remove" for c in self.calls()))

    def test_pause_skips_named_settled_worker(self):
        out = self.launch()
        self.task(out)
        self.set_state(status="idle", write_on_prompt={
            "path": self.result(out), "text": "Status: succeeded\nNext: review\n"})
        self.nakama("prompt", out["name"], "task.md")
        record_path = self.root / "records" / f"{out['name']}.json"
        before = record_path.read_text()
        rc, result = self.nakama("pause", out["name"])
        self.assertEqual(rc, 0)
        self.assertEqual(result, {"name": out["name"], "status": "skipped", "reason": "settled"})
        self.assertEqual(record_path.read_text(), before)
        self.assertFalse(Path(out["dir"], "pause.md").exists())

    def test_pause_refuses_read_only_worker(self):
        reviewer = self.nakama("launch", "reviewer", "review", "--no-worktree")[1]
        rc, result = self.nakama("pause", reviewer["name"])
        self.assertEqual(rc, 2)
        self.assertIn("read", result["error"])
        self.assertFalse(any(c[1:3] in (["tab", "close"], ["pane", "close"]) for c in self.calls()))

    def test_pause_all_skips_missing_worktree_without_aborting_others(self):
        live, missing = self.launch(), self.launch()
        self.task(live)
        self.nakama("prompt", live["name"], "task.md")
        shutil.rmtree(missing["worktree"])
        missing_record = self.root / "records" / f"{missing['name']}.json"
        before = missing_record.read_text()
        self.set_state(status=None, statuses={live["name"]: "working", missing["name"]: "working"})
        rc, result = self.nakama("pause", "--all", "--timeout", "0")
        self.assertEqual(rc, 0, result)
        entries = {entry["name"]: entry for entry in result["results"]}
        self.assertEqual(entries[missing["name"]], {
            "name": missing["name"], "status": "skipped", "reason": "no_worktree"})
        self.assertEqual(entries[live["name"]]["status"], "paused_forced")
        self.assertEqual(missing_record.read_text(), before)
        self.assertTrue(self.record(live["name"])["paused"])

    def test_pause_named_worker_without_worktree_fails_cleanly(self):
        out = self.launch()
        shutil.rmtree(out["worktree"])
        before = self.calls()
        rc, result = self.nakama("pause", out["name"])
        self.assertEqual(rc, 2)
        self.assertEqual(result["error"], f"{out['name']}: worktree {out['worktree']} does not exist")
        self.assertEqual(self.calls()[len(before):], [])

    def test_pause_all_skips_missing_agent_dir_without_aborting_others(self):
        live, missing = self.launch(), self.launch()
        self.task(live)
        self.nakama("prompt", live["name"], "task.md")
        shutil.rmtree(missing["dir"])
        missing_record = self.root / "records" / f"{missing['name']}.json"
        before = missing_record.read_text()
        self.set_state(status=None, statuses={live["name"]: "working", missing["name"]: "working"})
        rc, result = self.nakama("pause", "--all", "--timeout", "0")
        self.assertEqual(rc, 0, result)
        entries = {entry["name"]: entry for entry in result["results"]}
        self.assertEqual(entries[missing["name"]], {
            "name": missing["name"], "status": "skipped", "reason": "no_worktree"})
        self.assertEqual(entries[live["name"]]["status"], "paused_forced")
        self.assertEqual(missing_record.read_text(), before)
        self.assertFalse(Path(missing["dir"]).exists())
        self.assertTrue(self.record(live["name"])["paused"])

    def test_pause_named_worker_without_agent_dir_fails_cleanly(self):
        out = self.launch()
        shutil.rmtree(out["dir"])
        before = self.calls()
        rc, result = self.nakama("pause", out["name"])
        self.assertEqual(rc, 2)
        self.assertEqual(result["error"], f"{out['name']}: worktree {out['worktree']} does not exist")
        self.assertEqual(self.calls()[len(before):], [])
        self.assertFalse(Path(out["dir"]).exists())

    def test_pause_all_stops_read_only_and_skips_settled_and_other_sessions(self):
        builder_a, builder_b = self.launch(), self.launch()
        self.task(builder_a)
        self.task(builder_b)
        reviewer = self.nakama("launch", "reviewer", "review", "--no-worktree")[1]
        env_other = {**self.env, "HERDR_PANE_ID": "other-session"}
        builder_c = self.nakama("launch", "builder", "other", env=env_other)[1]
        self.nakama("prompt", builder_a["name"], "task.md")
        self.set_state(status="idle", write_on_prompt={
            "path": self.result(builder_b), "text": "Status: succeeded\nNext: review\n"})
        self.nakama("prompt", builder_b["name"], "task.md")
        self.set_state(status=None, statuses={builder_a["name"]: "working", builder_b["name"]: "idle",
                                             reviewer["name"]: "working", builder_c["name"]: "working"})
        rc, result = self.nakama("pause", "--all", "--timeout", "0")
        self.assertEqual(rc, 0, result)
        entries = {entry["name"]: entry for entry in result["results"]}
        self.assertEqual(set(entries), {builder_a["name"], builder_b["name"], reviewer["name"]})
        self.assertEqual(entries[builder_a["name"]]["status"], "paused_forced")
        self.assertEqual(entries[builder_b["name"]], {
            "name": builder_b["name"], "status": "skipped", "reason": "settled"})
        self.assertEqual(entries[reviewer["name"]], {
            "name": reviewer["name"], "status": "stopped", "reason": "read_only"})
        reviewer_rec = self.record(reviewer["name"])
        self.assertTrue(reviewer_rec["closed"])
        self.assertFalse(reviewer_rec.get("paused", False))
        self.assertIn(["herdr", "agent", "send-keys", reviewer["name"], "ctrl+c", "ctrl+c"], self.calls())
        self.assertIn(["herdr", "tab", "close", reviewer_rec["tab"]], self.calls())
        self.assertFalse(self.record(builder_b["name"])["closed"])
        self.assertFalse(self.record(builder_b["name"]).get("paused", False))
        self.assertNotIn(builder_c["name"], entries)

    def test_pause_all_stops_lone_read_only_worker(self):
        reviewer = self.nakama("launch", "reviewer", "review", "--no-worktree")[1]
        self.set_state(status="working")
        rc, result = self.nakama("pause", "--all", "--timeout", "30")
        self.assertEqual(rc, 0, result)
        self.assertEqual(result, {
            "status": "paused",
            "results": [{"name": reviewer["name"], "status": "stopped", "reason": "read_only"}]})
        rec = self.record(reviewer["name"])
        self.assertTrue(rec["closed"])
        self.assertFalse(rec.get("paused", False))
        self.assertEqual(rec.get("spent_min", 0), 0)  # never prompted, so nothing to bank

    def test_pause_all_stop_banks_read_only_spent_min(self):
        reviewer = self.nakama("launch", "reviewer", "review", "--no-worktree")[1]
        self.task(reviewer)
        self.nakama("prompt", reviewer["name"], "task.md")
        self.set_state(status="working")
        record = self.root / "records" / f"{reviewer['name']}.json"
        rec = json.loads(record.read_text())
        rec["prompted_at"] -= 10 * 60
        record.write_text(json.dumps(rec))
        rc, result = self.nakama("pause", "--all", "--timeout", "30")
        self.assertEqual(rc, 0, result)
        self.assertEqual(result["results"][0]["status"], "stopped")
        self.assertGreaterEqual(self.record(reviewer["name"])["spent_min"], 9.9)

    def test_pause_all_uses_one_shared_deadline(self):
        workers = [self.launch() for _ in range(3)]
        for worker in workers:
            self.task(worker)
            self.nakama("prompt", worker["name"], "task.md")
        self.set_state(status=None, statuses={worker["name"]: "working" for worker in workers})
        started = time.monotonic()
        rc, result = self.nakama("pause", "--all", "--timeout", "1")
        elapsed = time.monotonic() - started
        self.assertEqual(rc, 0, result)
        self.assertLess(elapsed, 2.5)
        self.assertEqual([worker["status"] for worker in result["results"]], ["paused_forced"] * 3)

    def test_pause_interrupted_by_ctrl_c_finishes_classification(self):
        out = self.launch()
        self.task(out)
        self.nakama("prompt", out["name"], "task.md")
        self.set_state(status="working")
        proc = subprocess.Popen(
            [sys.executable, str(NAKAMA), "pause", out["name"], "--timeout", "30"],
            env=self.env, cwd=self.root, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        pause_file = Path(out["dir"], "pause.md")
        deadline = time.monotonic() + 15
        while not pause_file.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(pause_file.exists())
        time.sleep(0.3)
        proc.send_signal(signal.SIGINT)
        stdout, stderr = proc.communicate(timeout=15)
        self.assertEqual(proc.returncode, 0, stderr)
        result = json.loads(stdout)
        self.assertEqual((result["status"], result["interrupted"]), ("paused_forced", True))
        rec = self.record(out["name"])
        self.assertTrue(rec["closed"] and rec["paused"])

    def test_resume_after_pause_clears_paused_flags(self):
        out = self.launch()
        self.task(out)
        self.nakama("prompt", out["name"], "task.md")
        self.set_state(status="working")
        self.assertEqual(self.nakama("pause", out["name"], "--timeout", "0")[1]["status"], "paused_forced")
        self.assertEqual(self.nakama("resume", out["name"])[1]["status"], "fresh_primed")
        rec = self.record(out["name"])
        self.assertFalse(rec["paused"])
        self.assertFalse(rec["paused_forced"])
        self.assertIsNone(rec["pause_sent_at"])

    def test_resume_restart_failure_keeps_paused_flags_and_retry_warns(self):
        out = self.launch()
        self.task(out)
        self.nakama("prompt", out["name"], "task.md")
        self.set_state(status="working")
        self.assertEqual(self.nakama("pause", out["name"], "--timeout", "0")[1]["status"],
                         "paused_forced")
        self.set_state(status=None, start_rc=1)
        rc, blocked = self.nakama("resume", out["name"])
        self.assertEqual((rc, blocked["status"]), (0, "blocked"))
        rec = self.record(out["name"])
        self.assertTrue(rec["paused"] and rec["paused_forced"])
        self.set_state(status=None, start_rc=0)
        self.assertEqual(self.nakama("resume", out["name"])[1]["status"], "fresh_primed")
        prompts = [c for c in self.calls() if c[1:3] == ["agent", "prompt"]]
        self.assertIn("The earlier pause was forced before a Resume note was written", prompts[-1][-1])
        rec = self.record(out["name"])
        self.assertFalse(rec["paused"] or rec["paused_forced"])

    def test_pause_forces_unreachable_worker_once_and_clears_index_lock(self):
        out = self.launch()
        worktree = out["worktree"]
        self.wgit(worktree, "init", "-q", "-b", "main")
        self.wgit(worktree, "commit", "-q", "--allow-empty", "-m", "root")
        lock = Path(worktree, ".git", "index.lock")
        lock.write_text("")
        self.task(out)
        self.nakama("prompt", out["name"], "task.md")
        self.set_state(status="working", prompt_fail=[out["name"]])
        started = time.monotonic()
        rc, result = self.nakama("pause", out["name"], "--timeout", "30")
        elapsed = time.monotonic() - started
        self.assertEqual(rc, 0, result)
        self.assertEqual(result["status"], "paused_forced")
        self.assertLess(elapsed, 10)
        rec = self.record(out["name"])
        self.assertTrue(rec["closed"] and rec["paused"] and rec["paused_forced"])
        calls = self.calls()
        keys = calls.index(["herdr", "agent", "send-keys", out["name"], "ctrl+c", "ctrl+c"])
        self.assertLess(keys, calls.index(["herdr", "tab", "close", rec["tab"]]))
        self.assertFalse(lock.exists())

    def test_forced_pause_keeps_orchestrator_index_lock_for_no_worktree_worker(self):
        self.wgit(self.root, "init", "-q", "-b", "main")
        lock = Path(self.root, ".git", "index.lock")
        lock.write_text("")
        out = self.launch("--no-worktree")
        self.assertIsNone(self.record(out["name"])["branch"])
        self.task(out)
        self.nakama("prompt", out["name"], "task.md")
        self.set_state(status="working")
        self.assertEqual(self.nakama("pause", out["name"], "--timeout", "0")[1]["status"],
                         "paused_forced")
        self.assertIn(["herdr", "agent", "send-keys", out["name"], "ctrl+c", "ctrl+c"], self.calls())
        self.assertTrue(lock.exists())  # that checkout belongs to the orchestrator, not the worker

    def test_pause_timeout_forced_worker_interrupts_and_clears_index_lock(self):
        out = self.launch()
        worktree = out["worktree"]
        self.wgit(worktree, "init", "-q", "-b", "main")
        self.wgit(worktree, "commit", "-q", "--allow-empty", "-m", "root")
        lock = Path(worktree, ".git", "index.lock")
        lock.write_text("")
        self.task(out)
        self.nakama("prompt", out["name"], "task.md")
        self.set_state(status="working")
        self.assertEqual(self.nakama("pause", out["name"], "--timeout", "0")[1]["status"],
                         "paused_forced")
        self.assertIn(["herdr", "agent", "send-keys", out["name"], "ctrl+c", "ctrl+c"], self.calls())
        self.assertFalse(lock.exists())

    def test_resume_after_forced_pause_warns_about_half_finished_edits(self):
        out = self.launch()
        self.task(out)
        self.nakama("prompt", out["name"], "task.md")
        self.set_state(status="working")
        self.assertEqual(self.nakama("pause", out["name"], "--timeout", "0")[1]["status"],
                         "paused_forced")
        self.assertEqual(self.nakama("resume", out["name"])[1]["status"], "fresh_primed")
        prompts = [c for c in self.calls() if c[1:3] == ["agent", "prompt"]]
        self.assertIn("The earlier pause was forced before a Resume note was written", prompts[-1][-1])

    def test_resume_after_noted_pause_has_no_forced_warning(self):
        out = self.launch()
        self.task(out)
        self.nakama("prompt", out["name"], "task.md")
        self.set_state(status="idle", write_on_prompt={
            "path": self.result(out), "text": "Status: partial\n## Resume\nfinish\n"})
        self.assertEqual(self.nakama("pause", out["name"], "--timeout", "5")[1]["status"], "paused")
        self.assertEqual(self.nakama("resume", out["name"])[1]["status"], "fresh_primed")
        prompts = [c for c in self.calls() if c[1:3] == ["agent", "prompt"]]
        self.assertNotIn("was forced before a Resume note was written", prompts[-1][-1])
        self.assertIn("The earlier pause is over", prompts[-1][-1])

    def test_pause_requires_exactly_one_name_or_all(self):
        rc, missing = self.nakama("pause")
        self.assertEqual(rc, 2)
        self.assertIn("exactly one", missing["error"])
        out = self.launch()
        rc, both = self.nakama("pause", out["name"], "--all")
        self.assertEqual(rc, 2)
        self.assertIn("exactly one", both["error"])

    def test_pause_all_without_workers_reports_no_recipients(self):
        self.assertEqual(self.nakama("pause", "--all", "--timeout", "0")[1],
                         {"status": "no_recipients", "results": []})

    def test_pause_skips_never_prompted_worker_by_name_and_in_all(self):
        out = self.launch()
        rc, result = self.nakama("pause", out["name"])
        self.assertEqual(rc, 0, result)
        self.assertEqual(result, {"name": out["name"], "status": "skipped", "reason": "not_prompted"})
        self.assertFalse(Path(out["dir"], "pause.md").exists())
        rc, result = self.nakama("pause", "--all", "--timeout", "30")
        self.assertEqual(rc, 0, result)
        self.assertEqual(result["status"], "no_recipients")
        self.assertEqual(result["results"], [{"name": out["name"], "status": "skipped",
                                              "reason": "not_prompted"}])
        rec = self.record(out["name"])
        self.assertFalse(rec["closed"] or rec.get("paused", False))
        self.assertFalse(Path(out["dir"], "pause.md").exists())
        self.assertFalse(any(c[1:3] in (["tab", "close"], ["pane", "close"]) for c in self.calls()))

    def test_pause_needs_input_note_on_clean_tree_parks_worker(self):
        out = self.launch()
        self.task(out)
        self.nakama("prompt", out["name"], "task.md")
        self.set_state(status="idle", write_on_prompt={
            "path": self.result(out), "text": "Status: needs_input\nNext: ask\n"})
        rc, result = self.nakama("pause", out["name"], "--timeout", "5")
        self.assertEqual(rc, 0, result)
        self.assertEqual(result["status"], "paused")
        rec = self.record(out["name"])
        self.assertTrue(rec["closed"] and rec["paused"])
        self.assertFalse(rec["paused_forced"])

    def test_pause_help_describes_its_effect(self):
        text = self.help_text("pause")
        for phrase in ("--all", "Resume note", "uncommitted", "keeps the worktree"):
            self.assertIn(phrase, text)

    def test_teardown_remove_worktree_refuses_paused_worker(self):
        out = self.launch()
        self.task(out)
        self.nakama("prompt", out["name"], "task.md")
        self.set_state(status="working")
        self.nakama("pause", out["name"], "--timeout", "0")
        before = self.calls()
        rc, result = self.nakama("teardown", out["name"], "--remove-worktree")
        self.assertEqual(rc, 2)
        self.assertIn("is paused", result["error"])
        self.assertEqual(self.calls()[len(before):], [])

    def test_teardown_force_removes_paused_worktree(self):
        out = self.launch()
        self.task(out)
        self.nakama("prompt", out["name"], "task.md")
        self.set_state(status="working")
        self.nakama("pause", out["name"], "--timeout", "0")
        rc, result = self.nakama("teardown", out["name"], "--remove-worktree", "--force")
        self.assertEqual(rc, 0)
        self.assertEqual(result["status"], "closed")
        self.assertTrue(result["worktree_removed"])
        rec = self.record(out["name"])
        self.assertFalse(rec["paused"])
        self.assertFalse(rec["paused_forced"])
        self.assertIsNone(rec["pause_sent_at"])
        self.assertEqual(self.nakama("sweep")[1]["paused_workers"], [])

    def test_teardown_without_removal_allows_paused_worker(self):
        out = self.launch()
        self.task(out)
        self.nakama("prompt", out["name"], "task.md")
        self.set_state(status="working")
        self.nakama("pause", out["name"], "--timeout", "0")
        rc, result = self.nakama("teardown", out["name"])
        self.assertEqual(rc, 0)
        self.assertEqual(result["status"], "closed")
        self.assertFalse(any(c[0] == "wt" and c[1] == "remove" for c in self.calls()))

    def test_sweep_lists_paused_workers(self):
        out = self.launch()
        self.task(out)
        self.nakama("prompt", out["name"], "task.md")
        self.set_state(status="working")
        self.nakama("pause", out["name"], "--timeout", "0")
        result = self.nakama("sweep")[1]
        self.assertEqual(result["paused_workers"], [{"name": out["name"], "worktree": out["worktree"]}])

    def test_sweep_omits_paused_workers_with_missing_worktree(self):
        out = self.launch()
        self.task(out)
        self.nakama("prompt", out["name"], "task.md")
        self.set_state(status="working")
        self.nakama("pause", out["name"], "--timeout", "0")
        shutil.rmtree(out["worktree"])
        result = self.nakama("sweep")[1]
        self.assertEqual(result["paused_workers"], [])

    def test_sweep_paused_workers_filters_sessions_unless_all_requested(self):
        current = {**self.env, "NAKAMA_SESSION": "session-current"}
        other = {**self.env, "NAKAMA_SESSION": "session-other", "HERDR_PANE_ID": "other-session"}
        first = self.nakama("launch", "builder", "first", env=current)[1]
        second = self.nakama("launch", "builder", "second", env=other)[1]
        self.task(first)
        self.nakama("prompt", first["name"], "task.md", env=current)
        self.task(second)
        self.nakama("prompt", second["name"], "task.md", env=other)
        self.set_state(status=None, statuses={first["name"]: "working", second["name"]: "working"})
        self.nakama("pause", first["name"], "--timeout", "0", env=current)
        self.nakama("pause", second["name"], "--timeout", "0", env=other)
        current_sweep = self.nakama("sweep", env=current)[1]
        all_sweep = self.nakama("sweep", "--all", env=current)[1]
        self.assertEqual([worker["name"] for worker in current_sweep["paused_workers"]], [first["name"]])
        self.assertEqual({worker["name"] for worker in all_sweep["paused_workers"]},
                         {first["name"], second["name"]})

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

    def test_sweep_filters_open_records_to_session_unless_all_requested(self):
        env_a = {**self.env, "HERDR_PANE_ID": "pane:A"}
        env_b = {**self.env, "HERDR_PANE_ID": "pane:B"}
        a = self.nakama("launch", "builder", "Session A", env=env_a)[1]
        b = self.nakama("launch", "builder", "Session B", env=env_b)[1]
        self.set_state(status=None, statuses={a["name"]: "idle", b["name"]: "idle"},
                       agents=[{"name": a["name"]}, {"name": b["name"]}, {"name": "r9-stray"}])

        default = self.nakama("sweep", env=env_a)[1]
        all_sessions = self.nakama("sweep", "--all", env=env_a)[1]
        self.assertEqual([r["name"] for r in default["open_records"]], [a["name"]])
        self.assertEqual({r["name"] for r in all_sessions["open_records"]}, {a["name"], b["name"]})
        self.assertEqual(default["stray_agents"], ["r9-stray"])
        self.assertEqual(all_sessions["stray_agents"], ["r9-stray"])
        self.assertEqual(default["agent_worktrees"], all_sessions["agent_worktrees"])

    def test_sweep_help_describes_session_scope_and_all_option(self):
        text = self.help_text("sweep")
        self.assertIn("--all", text)
        self.assertIn("every session", text)

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

    def test_teardown_worktree_failure_reports_reason_and_closes_record(self):
        out = self.launch()
        env = {**self.env, "FAKE_WT_REMOVE_RC": "1",
               "FAKE_WT_REMOVE_STDERR": "Cannot remove worktree: uncommitted changes",
               "FAKE_WT_REMOVE_STDOUT": "Commit or stash changes first"}
        rc, result = self.nakama("teardown", out["name"], "--remove-worktree", env=env)
        self.assertEqual(rc, 0)
        self.assertFalse(result["worktree_removed"])
        self.assertIn("Cannot remove worktree: uncommitted changes", result["reason"])
        self.assertIn("Commit or stash changes first", result["reason"])
        record = json.loads((self.root / "records" / f"{out['name']}.json").read_text())
        self.assertTrue(record["closed"])

    def test_teardown_success_reports_no_reason_and_only_passes_force_when_requested(self):
        out = self.launch()
        rc, result = self.nakama("teardown", out["name"], "--remove-worktree")
        self.assertEqual(rc, 0)
        self.assertTrue(result["worktree_removed"])
        self.assertNotIn("reason", result)
        self.assertNotIn("forced", result)
        self.assertIn(["wt", "remove", "agent/b1-order-tip", "--yes"], self.calls())
        self.assertFalse(any(c[:2] == ["wt", "remove"] and "--force" in c for c in self.calls()))

        forced = self.launch()
        rc, result = self.nakama("teardown", forced["name"], "--remove-worktree", "--force")
        self.assertEqual(rc, 0)
        self.assertTrue(result["worktree_removed"])
        self.assertNotIn("reason", result)
        self.assertTrue(result["forced"])
        self.assertIn(["wt", "remove", "agent/b2-order-tip", "--yes", "--force"], self.calls())

    def test_teardown_joined_reviewer_force_reports_no_owned_worktree(self):
        builder = self.launch()
        rc, reviewer = self.nakama("launch", "reviewer", "review", "--join", builder["name"])
        self.assertEqual(rc, 0, reviewer)
        rc, result = self.nakama("teardown", reviewer["name"], "--remove-worktree", "--force")
        self.assertEqual(rc, 0)
        self.assertFalse(result["worktree_removed"])
        self.assertEqual(result["reason"], "no worktree owned by this agent")
        self.assertNotIn("forced", result)
        self.assertFalse(any(c[:2] == ["wt", "remove"] for c in self.calls()))
        record = json.loads((self.root / "records" / f"{reviewer['name']}.json").read_text())
        self.assertTrue(record["closed"])

    def test_teardown_force_without_remove_worktree_does_nothing(self):
        out = self.launch()
        rc, result = self.nakama("teardown", out["name"], "--force")
        self.assertEqual(rc, 2)
        self.assertIn("--force requires --remove-worktree", result["error"])
        self.assertFalse(any(c[1:3] == ["agent", "send-keys"] or c[1:3] in
                             (["tab", "close"], ["pane", "close"]) for c in self.calls()))
        record = json.loads((self.root / "records" / f"{out['name']}.json").read_text())
        self.assertFalse(record["closed"])
        self.assertFalse(any(c[:2] == ["wt", "remove"] for c in self.calls()))

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
        # Read-only inspection and creating .agent files must not stop on a permission prompt.
        read_tools = starts[0][starts[0].index("--allowedTools") + 1:starts[0].index("--model")]
        self.assertEqual(read_tools, ["Bash(git diff:*)", "Bash(git log:*)", "Bash(git show:*)",
                                      "Bash(git status:*)", "Bash(git grep:*)", "Bash(git ls-files:*)",
                                      "Edit(.agent/**)", "Write(.agent/**)", "Bash(nakama send:*)",
                                      "Bash(nakama inbox:*)"])
        write_tools = starts[1][starts[1].index("--allowedTools") + 1:]
        self.assertEqual(write_tools, ["Bash(git status:*)", "Bash(git diff:*)", "Bash(git log:*)",
                                       "Bash(git add:*)", "Bash(git commit:*)", "Bash(mkdir:*)",
                                       "Bash(nakama send:*)", "Bash(nakama inbox:*)"])

    def test_flags_override_frontmatter(self):
        rc, out = self.nakama("launch", "reviewer", "tip", "--no-worktree",
                              "--kind", "pi", "--model", "x/y", "--budget", "5")
        self.assertEqual(rc, 0, out)
        start = self.start_call()
        self.assertEqual(start[start.index("--kind") + 1], "pi")
        self.assertEqual(start[start.index("--model") + 1], "x/y")
        tools_index = start.index("--tools")
        self.assertEqual(start[tools_index:tools_index + 4],
                         ["--tools", "+grep,+find,+ls", "--exclude-tools", "edit"])
        rec = json.loads((self.root / "records" / f"{out['name']}.json").read_text())
        self.assertEqual(rec["budget_min"], 5)

    def test_pi_write_mode_uses_default_tools_without_flags(self):
        rc, out = self.nakama("launch", "builder", "tip", "--no-worktree")
        self.assertEqual(rc, 0, out)
        start = self.start_call()
        self.assertNotIn("--tools", start)
        self.assertNotIn("--exclude-tools", start)

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

    def test_role_frontmatter_keeps_hash_inside_quotes_and_strips_trailing_comments(self):
        (self.root / "roles" / "hash.md").write_text("""---
prefix: h
model: "opus#dev"  # pinned
budget: 9 # minutes
---
You are a reviewer.
""")
        rc, out = self.nakama("roles")
        self.assertEqual(rc, 0, out)
        role = next(role for role in out["roles"] if role["role"] == "hash")
        self.assertEqual(role["model"], "opus#dev")
        self.assertEqual(role["budget"], 9)

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
        rc, out = self.nakama("list", env={**self.env, "NAKAMA_SESSION": "default"})
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

    def test_frontmatter_only_user_role_keeps_bundled_persona(self):
        user = self.root / "userroles"
        user.mkdir()
        (user / "reviewer.md").write_text("---\nkind: pi\nmodel: glm\n---\n")
        (user / "builder.md").write_text("---\nmodel: luna\n---\n")
        reviewer = {r["role"]: r for r in self.nakama("roles")[1]["roles"]}["reviewer"]
        self.assertEqual((reviewer["kind"], reviewer["model"]), ("pi", "glm"))
        self.assertEqual((reviewer["prefix"], reviewer["names"]), ("r", ["zoro", "sanji"]))
        self.assertEqual(reviewer["path"], str(user / "reviewer.md"))
        out = self.launch()
        self.assertEqual(Path(out["dir"], "persona.md").read_text(), "You are a builder.\n")

    def test_common_persona_precedes_role_persona(self):
        (self.root / "roles" / "_common.md").write_text("Shared policy overrides local text.\n")
        out = self.launch()
        persona = Path(out["dir"], "persona.md").read_text()
        self.assertEqual(persona, "Shared policy overrides local text.\n\nYou are a builder.\n")
        self.assertIn("overrides", persona)

    def test_user_common_overrides_bundled(self):
        (self.root / "roles" / "_common.md").write_text("Bundled common.\n")
        user = self.root / "userroles"
        user.mkdir()
        (user / "_common.md").write_text("User common.\n")
        out = self.launch()
        persona = Path(out["dir"], "persona.md").read_text()
        self.assertEqual(persona, "User common.\n\nYou are a builder.\n")

    def test_roles_skips_underscore_files_and_launch_rejects_them(self):
        roles_dir = self.root / "roles"
        (roles_dir / "_common.md").write_text("Shared rules.\n")
        (roles_dir / "_hidden.md").write_text("Not a role.\n")
        roles = {r["role"] for r in self.nakama("roles")[1]["roles"]}
        self.assertEqual(roles, {"builder", "reviewer"})
        rc, out = self.nakama("launch", "_common", "x", "--kind", "pi")
        self.assertEqual(rc, 2)
        self.assertIn("unknown role _common", out["error"])

    def test_launch_without_common_persona_still_works(self):
        out = self.launch()
        self.assertEqual(out["status"], "ready")
        self.assertEqual(Path(out["dir"], "persona.md").read_text(), "You are a builder.\n")

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
        env = {k: v for k, v in self.env.items() if k not in ("XDG_CONFIG_HOME", "GIT_CONFIG_GLOBAL")}
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
        self.assertIn("in the background", wait)
        self.assertIn("--timeout 1800", wait)
        self.assertIn("180-minute ceiling applies per prompt", extend)
        self.assertIn("--answer", extend)

    def start_session(self, name="cart-rework", about="shopee cart group lazy-row fix", env=None):
        return self.nakama("session", "start", name, "--about", about, env=env)

    def orch_mail(self, session, text="hello", kind="info", sender="b1-x"):
        box = Path(self.env["NAKAMA_STATE_DIR"]) / "inbox" / "orchestrator" / session
        box.mkdir(parents=True, exist_ok=True)
        (box / f"{time.time_ns()}.{sender}.{kind}.md").write_text(text)

    def pane_record(self, name, session, pane, **extra):
        state = Path(self.env["NAKAMA_STATE_DIR"])
        state.mkdir(parents=True, exist_ok=True)
        rec = {"name": name, "role": "builder", "kind": "pi", "worktree": str(self.root),
               "rounds": 0, "closed": False, "session": session, "orch_pane": pane}
        (state / f"{name}.json").write_text(json.dumps({**rec, **extra}))

    def named_meta(self, name, pane, about="a run", terminal=None, agent=None, created=None):
        path = Path(self.env["NAKAMA_STATE_DIR"]) / "sessions" / name / "meta.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        created = time.time() if created is None else created
        path.write_text(json.dumps({"name": name, "about": about, "owner_pane": pane,
                                    "owner_terminal": terminal, "created": created,
                                    "last_seen": created, "adoptions": [], "agent": agent}))
        return path

    def session_ls(self, *extra):
        return self.nakama("session", "ls", *extra)

    def wait_for_call(self, *prefix):
        for _ in range(500):
            if any(c[:len(prefix)] == list(prefix) for c in self.calls()):
                return
            time.sleep(0.01)
        self.fail(f"no herdr call {prefix}")

    def test_session_start_creates_bound_meta_and_export(self):
        self.set_state(status="idle", terminal_id="term-A")
        rc, out = self.start_session()
        self.assertEqual(rc, 0, out)
        self.assertEqual(out["name"], "cart-rework")
        self.assertEqual(out["about"], "shopee cart group lazy-row fix")
        self.assertEqual(out["export"], "export NAKAMA_SESSION=cart-rework")
        path = Path(self.env["NAKAMA_STATE_DIR"]) / "sessions" / "cart-rework" / "meta.json"
        self.assertEqual(out["dir"], str(path.parent))
        meta = json.loads(path.read_text())
        self.assertEqual(sorted(meta),
                         ["about", "adoptions", "agent", "created", "last_seen", "name", "owner_pane",
                          "owner_terminal"])
        self.assertEqual((meta["name"], meta["about"]), ("cart-rework", "shopee cart group lazy-row fix"))
        self.assertEqual(meta["owner_pane"], self.env["HERDR_PANE_ID"])
        self.assertEqual(meta["owner_terminal"], "term-A")
        self.assertEqual((meta["adoptions"], meta["agent"]), ([], None))
        self.assertAlmostEqual(meta["created"], meta["last_seen"])
        self.assertAlmostEqual(meta["created"], time.time(), delta=60)

    def test_session_start_tolerates_unavailable_terminal_identity(self):
        self.set_state(status=None)
        rc, out = self.start_session()
        self.assertEqual(rc, 0, out)
        path = Path(self.env["NAKAMA_STATE_DIR"]) / "sessions" / "cart-rework" / "meta.json"
        self.assertIsNone(json.loads(path.read_text())["owner_terminal"])

    def test_session_start_rejects_name_needing_sanitizing(self):
        rc, err = self.start_session("pr-6506/review")
        self.assertEqual(rc, 2)
        self.assertIn("pr-6506_review", err["error"])
        self.assertFalse((Path(self.env["NAKAMA_STATE_DIR"]) / "sessions").exists())

    def test_session_start_rejects_missing_or_multiline_about(self):
        self.assertEqual(self.nakama("session", "start", "cart-rework")[0], 2)
        self.assertEqual(self.start_session(about="")[0], 2)
        self.assertEqual(self.start_session(about="one\ntwo")[0], 2)
        self.assertEqual(self.start_session(about="x" * 201)[0], 2)
        self.assertFalse((Path(self.env["NAKAMA_STATE_DIR"]) / "sessions").exists())

    def test_session_start_rejects_duplicate_name(self):
        self.assertEqual(self.start_session()[0], 0)
        rc, err = self.start_session(about="another about")
        self.assertEqual(rc, 2)
        self.assertEqual(err["about"], "shopee cart group lazy-row fix")
        self.assertEqual(err["pane"], self.env["HERDR_PANE_ID"])
        self.assertTrue(err["pane_alive"])
        self.set_state(statuses={self.env["HERDR_PANE_ID"]: None})
        self.assertFalse(self.start_session()[1]["pane_alive"])

    def test_session_start_needs_a_herdr_pane(self):
        env = {k: v for k, v in self.env.items() if k != "HERDR_PANE_ID"}
        rc, err = self.start_session(env=env)
        self.assertEqual(rc, 2, err)
        self.assertIn("HERDR_PANE_ID", err["error"])

    def test_session_start_rejects_case_insensitive_duplicate(self):
        self.assertEqual(self.start_session("cart", "first")[0], 0)
        path = Path(self.env["NAKAMA_STATE_DIR"]) / "sessions" / "cart" / "meta.json"
        before = path.read_bytes()
        rc, err = self.start_session("Cart", "SECOND")
        self.assertEqual(rc, 2, err)
        self.assertEqual(err["about"], "first")
        self.assertEqual(path.read_bytes(), before)

    def test_session_start_rejects_unreadable_existing_meta(self):
        ghost = Path(self.env["NAKAMA_STATE_DIR"]) / "sessions" / "ghost"
        ghost.mkdir(parents=True)
        (ghost / "meta.json").write_text("not json")
        rc, err = self.start_session("ghost")
        self.assertEqual(rc, 2, err)
        self.assertEqual((ghost / "meta.json").read_text(), "not json")

    def test_session_start_rebinds_this_pane_records(self):
        self.pane_record("b1-x", "w3_p1", "w3:p1")
        rc, out = self.start_session("named")
        self.assertEqual(rc, 0, out)
        self.assertEqual(out["rebound"], 1)
        self.assertEqual(self.record("b1-x")["session"], "named")
        self.assertEqual([r["name"] for r in self.nakama("list")[1]["agents"]], ["b1-x"])

    def test_session_start_rebind_leaves_other_panes_alone(self):
        self.pane_record("b1-other", "w9_p9", "w9:p9")
        rc, out = self.start_session("named")
        self.assertEqual(rc, 0, out)
        self.assertEqual(out["rebound"], 0)
        self.assertEqual(self.record("b1-other")["session"], "w9_p9")

    def test_session_start_rejects_second_session_in_one_pane(self):
        self.set_state(status="idle", terminal_id="term-A")
        self.assertEqual(self.start_session("alpha", "a")[0], 0)
        rc, err = self.start_session("beta", "b")
        self.assertEqual(rc, 2, err)
        self.assertEqual((err["session"], err["about"]), ("alpha", "a"))
        self.assertFalse((Path(self.env["NAKAMA_STATE_DIR"]) / "sessions" / "beta").exists())

    def test_session_start_reclaims_stale_pane_claim_after_session_deleted(self):
        self.set_state(status="idle", terminal_id="term-A")
        self.assertEqual(self.start_session("alpha", "first")[0], 0)
        sessions = Path(self.env["NAKAMA_STATE_DIR"]) / "sessions"
        shutil.rmtree(sessions / "alpha")
        rc, out = self.start_session("beta", "second")
        self.assertEqual(rc, 0, out)
        self.assertEqual(out["name"], "beta")
        self.assertEqual((sessions / ".by-pane" / "w3_p1").read_text(), "beta")

    def test_session_start_rejects_pane_key_name(self):
        rc, err = self.start_session("w3_p1")
        self.assertEqual(rc, 2, err)
        self.assertIn("pane key", err["error"])
        self.assertFalse((Path(self.env["NAKAMA_STATE_DIR"]) / "sessions").exists())

    def test_session_start_leaves_no_temp_file(self):
        self.assertEqual(self.start_session()[0], 0)
        files = [p.name for p in
                 (Path(self.env["NAKAMA_STATE_DIR"]) / "sessions" / "cart-rework").iterdir()]
        self.assertEqual(files, ["meta.json"])

    def test_wait_save_does_not_clobber_a_concurrent_session_rebind(self):
        self.pane_record("b1-x", "w3_p1", "w3:p1", prompted_at=time.time(), nudged_at=None,
                         budget_min=45, extended_min=0, mode="write")
        self.set_state(status="working", screen="SCREEN one")
        barrier = self.root / "release-wait"
        proc = subprocess.Popen([sys.executable, str(NAKAMA), "wait", "b1-x", "--timeout", "0.5"],
                                env={**self.env, "FAKE_GET_BARRIER": str(barrier)}, cwd=self.root,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            self.wait_for_call("herdr", "agent", "get", "b1-x")
            self.assertIsNone(proc.poll())
            rc, out = self.start_session("named")
            self.assertEqual(rc, 0, out)
            self.assertEqual(out["rebound"], 1)
            self.assertEqual(self.record("b1-x")["session"], "named")
            self.assertIsNone(proc.poll())
        finally:
            barrier.touch()
            stdout, stderr = proc.communicate(timeout=30)
        self.assertEqual(proc.returncode, 0, (stdout, stderr))
        self.assertEqual(self.record("b1-x")["session"], "named")

    def test_prompt_save_does_not_clobber_a_concurrent_session_rebind(self):
        self.pane_record("b1-x", "w3_p1", "w3:p1")
        task = self.root / ".agent" / "b1-x" / "task.md"
        task.parent.mkdir(parents=True)
        task.write_text("## Goal\nwork\n")
        self.set_state(status="idle", terminal_id="term-A")
        barrier = self.root / "release-prompt"
        proc = subprocess.Popen([sys.executable, str(NAKAMA), "prompt", "b1-x", "task.md"],
                                env={**self.env, "FAKE_PROMPT_BARRIER": str(barrier)}, cwd=self.root,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            self.wait_for_call("herdr", "agent", "prompt", "b1-x")
            self.assertIsNone(proc.poll())
            rc, out = self.start_session("named")
            self.assertEqual(rc, 0, out)
            self.assertEqual(out["rebound"], 1)
            self.assertEqual(self.record("b1-x")["session"], "named")
        finally:
            barrier.touch()
            stdout, stderr = proc.communicate(timeout=30)
        self.assertEqual(proc.returncode, 0, (stdout, stderr))
        self.assertEqual(self.record("b1-x")["session"], "named")

    def test_resume_writes_only_owned_fields_after_concurrent_session_rebind(self):
        self.pane_record("b1-x", "w3_p1", "w3:p1", mode="write", tab=None, layout="tab",
                         direction="right", branch=None, prompted_at=None, git_before=None, receipt=None,
                         nudged_at=None, screen_sig=None, screen_changed_at=None, paused=True,
                         paused_forced=True, pause_sent_at=time.time())
        record = self.record("b1-x")
        record["pane"] = "w1:p9"
        (Path(self.env["NAKAMA_STATE_DIR"]) / "b1-x.json").write_text(json.dumps(record))
        task = self.root / ".agent" / "b1-x" / "task.md"
        task.parent.mkdir(parents=True)
        task.write_text("## Goal\nwork\n")
        self.set_state(status="idle", statuses={"b1-x": None}, terminal_id="term-A")
        tab_barrier = self.root / "release-tab"
        prompt_barrier = self.root / "release-resume-prompt"
        proc = subprocess.Popen([sys.executable, str(NAKAMA), "resume", "b1-x"],
                                env={**self.env, "FAKE_TAB_BARRIER": str(tab_barrier),
                                     "FAKE_PROMPT_BARRIER": str(prompt_barrier)}, cwd=self.root,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            self.wait_for_call("herdr", "tab", "create")
            self.assertIsNone(proc.poll())
            rc, out = self.start_session("named")
            self.assertEqual(rc, 0, out)
            self.assertEqual(out["rebound"], 1)
            self.assertEqual(self.record("b1-x")["session"], "named")
            tab_barrier.touch()
            self.wait_for_call("herdr", "agent", "prompt", "b1-x")
            self.assertIsNone(proc.poll())
            self.assertEqual(self.record("b1-x")["session"], "named")
        finally:
            tab_barrier.touch()
            prompt_barrier.touch()
            stdout, stderr = proc.communicate(timeout=30)
        self.assertEqual(proc.returncode, 0, (stdout, stderr))
        self.assertEqual(self.record("b1-x")["session"], "named")
        self.assertFalse(self.record("b1-x")["paused"])
        self.assertFalse(self.record("b1-x")["paused_forced"])

    def test_session_start_rebind_ignores_another_sessions_worker(self):
        self.pane_record("b1-other", "other-session", "w3:p1")
        rc, out = self.start_session("named")
        self.assertEqual(rc, 0, out)
        self.assertEqual(out["rebound"], 0)
        self.assertEqual(self.record("b1-other")["session"], "other-session")

    def test_session_start_failure_leaves_no_directory(self):
        state = Path(self.env["NAKAMA_STATE_DIR"])
        state.mkdir(parents=True, exist_ok=True)
        (state / "broken.json").write_text("{not json")  # makes the rebind step raise mid-start
        rc, out = self.start_session("named")
        self.assertNotEqual(rc, 0, out)
        self.assertFalse((state / "sessions" / "named").exists())
        self.assertFalse((state / "sessions" / ".by-pane" / "w3_p1").exists())

    def test_session_start_rolls_back_all_rebinds_when_a_patch_fails(self):
        self.set_state(status="idle", terminal_id="term-A")
        self.pane_record("b1-first", "w3_p1", "w3:p1")
        self.pane_record("b1-second", "w3_p1", "w3:p1")
        state = Path(self.env["NAKAMA_STATE_DIR"])
        loader = importlib.machinery.SourceFileLoader("nakama_under_test", str(NAKAMA))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        nakama = importlib.util.module_from_spec(spec)
        with mock.patch.dict(os.environ, self.env):
            loader.exec_module(nakama)
            nakama.STATE_DIR = state
            original_patch_record = nakama.patch_record
            patches = []

            def fail_second(name, **changes):
                patches.append((name, changes.get("session")))
                if name == "b1-second" and changes.get("session") == "named":
                    raise OSError("injected patch failure")
                return original_patch_record(name, **changes)
            with mock.patch.object(nakama, "patch_record", fail_second):
                with self.assertRaisesRegex(OSError, "injected patch failure"):
                    nakama.cmd_session_start(SimpleNamespace(name="named", about="failure test"))
        self.assertEqual(patches, [("b1-first", "named"), ("b1-second", "named"),
                                   ("b1-second", "w3_p1"), ("b1-first", "w3_p1")])
        self.assertEqual(self.record("b1-first")["session"], "w3_p1")
        self.assertEqual(self.record("b1-second")["session"], "w3_p1")
        self.assertFalse((state / "sessions" / "named").exists())
        self.assertFalse((state / "sessions" / ".by-pane" / "w3_p1").exists())

    def test_session_start_reports_incomplete_existing_directory(self):
        ghost = Path(self.env["NAKAMA_STATE_DIR"]) / "sessions" / "ghost"
        ghost.mkdir(parents=True)
        rc, err = self.start_session("ghost")
        self.assertEqual(rc, 2, err)
        self.assertIn("unreadable", err["error"])
        self.assertIn(str(ghost), err["error"])

    def test_session_start_second_concurrent_claim_is_refused(self):
        self.set_state(status="idle", terminal_id="term-A")
        state = Path(self.env["NAKAMA_STATE_DIR"])
        alpha = state / "sessions" / "alpha"
        alpha.mkdir(parents=True)
        (alpha / "meta.json").write_text(json.dumps({
            "name": "alpha", "about": "a", "owner_pane": self.env["HERDR_PANE_ID"],
            "owner_terminal": "term-A"}))
        claim = state / "sessions" / ".by-pane" / "w3_p1"
        claim.parent.mkdir(parents=True)
        claim.write_text("alpha")
        rc, err = self.start_session("beta")
        self.assertEqual(rc, 2, err)
        self.assertEqual(err["session"], "alpha")
        self.assertEqual(err["about"], "a")
        self.assertFalse((state / "sessions" / "beta").exists())

    def test_session_resolution_uses_pane_bound_session(self):
        self.assertEqual(self.start_session()[0], 0)
        self.orch_mail("cart-rework", "bound")
        self.orch_mail("w3_p1", "pane")
        self.assertEqual([m["text"] for m in self.nakama("inbox", "--peek")[1]["messages"]], ["bound"])

    def test_session_resolution_uses_terminal_identity_when_pane_recycled(self):
        self.set_state(status="idle", terminal_id="term-A")
        self.assertEqual(self.start_session()[0], 0)
        self.orch_mail("cart-rework", "bound")
        self.orch_mail("w3_p1", "pane")
        self.set_state(status="idle", terminal_id="term-B")
        messages = self.nakama("inbox", "--peek")[1]["messages"]
        self.assertEqual([message["text"] for message in messages], ["pane"])
        rc, out = self.start_session("beta", "second terminal")
        self.assertEqual(rc, 0, out)
        self.assertEqual(out["rebound"], 0)

    def test_session_resolution_falls_back_when_terminal_lookup_fails(self):
        self.set_state(status="idle", terminal_id="term-A")
        self.assertEqual(self.start_session()[0], 0)
        self.orch_mail("cart-rework", "bound")
        self.orch_mail("w3_p1", "pane")
        self.set_state(status=None)
        messages = self.nakama("inbox", "--peek")[1]["messages"]
        self.assertEqual([message["text"] for message in messages], ["bound"])

    def test_session_resolution_refuses_ambiguous_pane_owner(self):
        # A recycled pane left two sessions claiming it, and the terminal lookup cannot tell them
        # apart. Binding either one would give this run another session's workers.
        state = Path(self.env["NAKAMA_STATE_DIR"]) / "sessions"
        for name in ("alpha", "beta"):
            meta = state / name / "meta.json"
            meta.parent.mkdir(parents=True, exist_ok=True)
            meta.write_text(json.dumps({"name": name, "about": name, "owner_pane": self.env["HERDR_PANE_ID"],
                                        "owner_terminal": "term-A", "created": 1, "last_seen": 1,
                                        "adoptions": [], "agent": None}))
        self.orch_mail("alpha", "stale-a")
        self.orch_mail("beta", "stale-b")
        self.orch_mail("w3_p1", "pane")
        self.set_state(status=None)  # herdr cannot report a terminal id
        messages = [m["text"] for m in self.nakama("inbox", "--peek")[1]["messages"]]
        self.assertEqual(messages, ["pane"])

    def test_session_resolution_falls_back_to_pane_id(self):
        self.orch_mail("w3_p1", "pane")
        self.orch_mail("cart-rework", "other")
        self.assertEqual([m["text"] for m in self.nakama("inbox", "--peek")[1]["messages"]], ["pane"])

    def test_session_resolution_lists_legacy_raw_pane_record(self):
        state = Path(self.env["NAKAMA_STATE_DIR"])
        state.mkdir(parents=True, exist_ok=True)
        for name, session in (("b1-legacy", "w3:p1"), ("b1-other", "w9:p9")):
            (state / f"{name}.json").write_text(json.dumps({
                "name": name, "role": "builder", "kind": "pi", "worktree": str(self.root),
                "rounds": 0, "closed": False, "session": session}))
        self.assertEqual([r["name"] for r in self.nakama("list")[1]["agents"]], ["b1-legacy"])

    def test_session_resolution_prefers_env_over_pane_binding(self):
        self.assertEqual(self.start_session()[0], 0)
        self.orch_mail("cart-rework", "bound")
        self.orch_mail("chosen", "env")
        env = {**self.env, "NAKAMA_SESSION": "chosen"}
        self.assertEqual([m["text"] for m in self.nakama("inbox", "--peek", env=env)[1]["messages"]],
                         ["env"])

    def test_session_resolution_ignores_non_dict_meta(self):
        broken = Path(self.env["NAKAMA_STATE_DIR"]) / "sessions" / "broken"
        broken.mkdir(parents=True)
        (broken / "meta.json").write_text("1")
        self.orch_mail("w3_p1", "pane")
        self.assertEqual([m["text"] for m in self.nakama("inbox", "--peek")[1]["messages"]],
                         ["pane"])

    def test_session_resolution_binding_survives_polluted_runner_env(self):
        os.environ["NAKAMA_SESSION"] = "leaked-from-runner"
        try:
            self.setUp()  # rebuilds self.env from the polluted os.environ
        finally:
            os.environ.pop("NAKAMA_SESSION", None)
        self.assertNotIn("NAKAMA_SESSION", self.env)
        self.assertEqual(self.start_session()[0], 0)
        self.orch_mail("cart-rework", "bound")
        self.assertEqual([m["text"] for m in self.nakama("inbox", "--peek")[1]["messages"]],
                         ["bound"])

    def session_meta_file(self, name="cart-rework"):
        return Path(self.env["NAKAMA_STATE_DIR"]) / "sessions" / name / "meta.json"

    def test_session_start_records_pi_agent_session_resume_token(self):
        token = {"agent": "pi", "kind": "path", "value": "/abs/session.jsonl"}
        self.set_state(status="idle", agent_session=token)
        rc, out = self.start_session()
        self.assertEqual(rc, 0, out)
        self.assertEqual(json.loads(self.session_meta_file().read_text())["agent"],
                         {**token, "source": "herdr:pi", "cwd": os.path.realpath(str(self.root))})
        self.assertEqual(out["resume_with"], "pi --session /abs/session.jsonl")

    def test_session_start_records_claude_agent_session_resume_token(self):
        uuid = "11111111-2222-3333-4444-555555555555"
        self.set_state(status="idle", agent_session={"agent": "claude", "kind": "id", "value": uuid})
        rc, out = self.start_session()
        self.assertEqual(rc, 0, out)
        self.assertEqual(out["resume_with"], f"claude --resume {uuid}")

    def test_session_start_unknown_agent_session_has_no_resume_token_command(self):
        self.set_state(status="idle", agent_session={"agent": "codex", "kind": "id", "value": "x"})
        rc, out = self.start_session()
        self.assertEqual(rc, 0, out)
        self.assertEqual(json.loads(self.session_meta_file().read_text())["agent"]["agent"], "codex")
        self.assertIsNone(out["resume_with"])

    def test_session_start_resume_token_quotes_paths_with_spaces(self):
        value = "/tmp/my session/session.jsonl"
        self.set_state(status="idle", agent_session={"agent": "pi", "kind": "path", "value": value})
        rc, out = self.start_session()
        self.assertEqual(rc, 0, out)
        self.assertEqual(shlex.split(out["resume_with"]), ["pi", "--session", value])

    def test_launch_refreshes_agent_session_resume_token(self):
        self.set_state(status="idle")
        self.assertEqual(self.start_session()[0], 0)
        self.assertIsNone(json.loads(self.session_meta_file().read_text())["agent"])
        value = "/abs/later.jsonl"
        self.set_state(status="idle", agent_session={"agent": "pi", "kind": "path", "value": value})
        rc, out = self.nakama("launch", "builder", "Order Tip", "--kind", "pi",
                              env={**self.env, "NAKAMA_SESSION": "cart-rework"})
        self.assertEqual(rc, 0, out)
        self.assertEqual(json.loads(self.session_meta_file().read_text())["agent"]["value"], value)

    def test_launch_keeps_agent_session_resume_token_when_herdr_fails(self):
        token = {"agent": "pi", "kind": "path", "value": "/abs/kept.jsonl"}
        self.set_state(status="idle", agent_session=token)
        self.assertEqual(self.start_session()[0], 0)
        before = json.loads(self.session_meta_file().read_text())["agent"]
        self.assertEqual(before, {**token, "source": "herdr:pi", "cwd": os.path.realpath(str(self.root))})
        self.set_state(status="idle", statuses={"w3:p1": None})
        rc, out = self.nakama("launch", "builder", "Order Tip", "--kind", "pi",
                              env={**self.env, "NAKAMA_SESSION": "cart-rework"})
        self.assertEqual(rc, 0, out)
        self.assertEqual(out["status"], "ready")
        self.assertEqual(json.loads(self.session_meta_file().read_text())["agent"], before)

    def test_second_launch_does_not_clobber_agent_session_resume_token(self):
        token = {"agent": "pi", "kind": "path", "value": "/abs/one.jsonl"}
        self.set_state(status="idle", agent_session=token)
        self.assertEqual(self.start_session()[0], 0)
        before = json.loads(self.session_meta_file().read_text())["agent"]
        self.assertEqual(before, {**token, "source": "herdr:pi", "cwd": os.path.realpath(str(self.root))})
        self.set_state(status="idle")
        for _ in range(2):
            rc, out = self.nakama("launch", "builder", "Order Tip", "--kind", "pi",
                                  env={**self.env, "NAKAMA_SESSION": "cart-rework"})
            self.assertEqual(rc, 0, out)
        self.assertEqual(json.loads(self.session_meta_file().read_text())["agent"], before)

    def test_launch_unnamed_session_writes_no_agent_session_meta(self):
        self.set_state(status="idle", agent_session={"agent": "pi", "kind": "path", "value": "/a.jsonl"})
        self.launch()
        self.assertFalse((Path(self.env["NAKAMA_STATE_DIR"]) / "sessions").exists())

    def test_session_start_duplicate_reports_resume_token(self):
        uuid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        self.set_state(status="idle", agent_session={"agent": "claude", "kind": "id", "value": uuid})
        self.assertEqual(self.start_session()[0], 0)
        rc, err = self.start_session(about="another about")
        self.assertEqual(rc, 2, err)
        self.assertEqual(err["resume_with"], f"claude --resume {uuid}")

    def test_launch_without_pane_id_keeps_agent_session_resume_token(self):
        token = {"agent": "pi", "kind": "path", "value": "/abs/nopane.jsonl"}
        self.set_state(status="idle", agent_session=token)
        self.assertEqual(self.start_session()[0], 0)
        before = json.loads(self.session_meta_file().read_text())["agent"]
        env = {k: v for k, v in self.env.items() if k != "HERDR_PANE_ID"}
        env["NAKAMA_SESSION"] = "cart-rework"
        rc, out = self.nakama("launch", "builder", "Order Tip", "--kind", "pi", env=env)
        self.assertEqual(rc, 0, out)
        self.assertEqual(out["status"], "ready")
        self.assertEqual(json.loads(self.session_meta_file().read_text())["agent"], before)

    def test_session_start_records_agent_session_when_cwd_is_gone(self):
        value = "/abs/gone.jsonl"
        self.set_state(status="idle", agent_session={"agent": "pi", "kind": "path", "value": value})
        gone = self.root / "gone-cwd"
        gone.mkdir()
        script = (f"cd {shlex.quote(str(gone))} && rmdir {shlex.quote(str(gone))} && "
                  f"exec {shlex.quote(sys.executable)} {shlex.quote(str(NAKAMA))} "
                  "session start cart-rework --about gone")
        p = subprocess.run(["sh", "-c", script], capture_output=True, text=True, env=self.env)
        self.assertEqual(p.returncode, 0, (p.stdout, p.stderr))
        self.assertEqual(json.loads(p.stdout)["name"], "cart-rework")
        agent = json.loads(self.session_meta_file().read_text())["agent"]
        self.assertIsNone(agent["cwd"])
        self.assertEqual(agent["value"], value)

    def test_nested_launch_records_orchestrator_agent_session_resume_token(self):
        user_roles = self.root / "userroles"
        user_roles.mkdir()
        (user_roles / "lead.md").write_text(
            "---\nprefix: l\nkind: pi\nmode: write\nspawns: true\n---\nLead.\n")
        self.set_state(status="idle")
        self.assertEqual(self.start_session()[0], 0)
        rc, parent = self.nakama("launch", "lead", "Parent",
                                 env={**self.env, "NAKAMA_SESSION": "cart-rework"})
        self.assertEqual(rc, 0, parent)
        self.assertIsNone(json.loads(self.session_meta_file().read_text())["agent"])
        token = {"agent": "pi", "kind": "path", "value": "/abs/orch.jsonl"}
        self.set_state(status="idle", statuses={"w3:p77": None}, agent_session=token)
        child_env = {**self.env, "NAKAMA_AGENT": parent["name"], "HERDR_PANE_ID": "w3:p77"}
        rc, child = self.nakama("launch", "builder", "Child", "--kind", "pi", env=child_env)
        self.assertEqual(rc, 0, child)
        self.assertEqual(json.loads(self.session_meta_file().read_text())["agent"],
                         {**token, "source": "herdr:pi", "cwd": os.path.realpath(str(self.root))})

    def test_session_ls_counts_workers_per_session(self):
        self.named_meta("alpha", "w3:p1")
        self.named_meta("beta", "w3:p2")
        self.pane_record("b1-a1", "alpha", "w3:p1")
        self.pane_record("b1-a2", "alpha", "w3:p1", closed=True)
        self.pane_record("b1-b1", "beta", "w3:p2")
        self.pane_record("b1-b2", "beta", "w3:p2")
        self.pane_record("b1-other", "other", "w3:p3")
        rc, out = self.session_ls()
        self.assertEqual(rc, 0, out)
        rows = {row["name"]: row for row in out["sessions"]}
        self.assertEqual(rows["alpha"]["workers"], {"open": 1, "total": 2})
        self.assertEqual(rows["beta"]["workers"], {"open": 2, "total": 2})
        self.assertNotIn("other", rows)

    def test_session_ls_alive_when_pane_terminal_and_token_match(self):
        token = {"agent": "pi", "kind": "path", "value": "/abs/live.jsonl"}
        self.named_meta("cart-rework", "w3:p1", about="cart fix", terminal="term-A",
                        agent={**token, "source": "herdr:pi", "cwd": "/tmp"})
        self.set_state(status="idle", terminal_id="term-A", agent_session=token)
        rc, out = self.session_ls()
        self.assertEqual(rc, 0, out)
        row = out["sessions"][0]
        self.assertEqual((row["name"], row["about"]), ("cart-rework", "cart fix"))
        self.assertEqual((row["pane"], row["alive"], row["pane_reused"]), ("w3:p1", True, False))
        self.assertEqual(row["kind"], "pi")
        self.assertEqual(row["resume_with"], "pi --session /abs/live.jsonl")
        self.assertFalse(row["legacy"])

    def test_session_ls_gone_when_herdr_fails(self):
        self.named_meta("cart-rework", "w3:p1", terminal="term-A")
        self.set_state(status="idle", statuses={"w3:p1": None})
        rc, out = self.session_ls()
        self.assertEqual(rc, 0, out)
        row = out["sessions"][0]
        self.assertEqual((row["alive"], row["pane_reused"]), (False, False))

    def test_session_ls_pane_reused_when_agent_session_differs(self):
        self.named_meta("cart-rework", "w3:p1", terminal="term-A",
                        agent={"agent": "pi", "kind": "path", "value": "/abs/old.jsonl",
                               "source": "herdr:pi", "cwd": "/tmp"})
        self.set_state(status="idle", terminal_id="term-A",
                       agent_session={"agent": "pi", "kind": "path", "value": "/abs/new.jsonl"})
        rc, out = self.session_ls()
        self.assertEqual(rc, 0, out)
        row = out["sessions"][0]
        self.assertEqual((row["alive"], row["pane_reused"]), (False, True))

    def test_session_ls_pane_reused_when_terminal_differs(self):
        self.named_meta("cart-rework", "w3:p1", terminal="term-A")
        self.set_state(status="idle", terminal_id="term-B")
        rc, out = self.session_ls()
        self.assertEqual(rc, 0, out)
        row = out["sessions"][0]
        self.assertEqual((row["alive"], row["pane_reused"]), (False, True))

    def test_session_ls_not_alive_when_agent_exited(self):
        self.named_meta("cart-rework", "w3:p1", terminal="term-A",
                        agent={"agent": "claude", "kind": "id", "value": "uuid-1",
                               "source": "herdr:claude", "cwd": "/tmp"})
        self.set_state(status="idle", terminal_id="term-A")
        rc, out = self.session_ls()
        self.assertEqual(rc, 0, out)
        row = out["sessions"][0]
        self.assertEqual((row["alive"], row["pane_reused"]), (False, False))

    def test_session_ls_sorted_by_last_seen_and_reports_notes_and_unread_mail(self):
        old, new = time.time() - 7200, time.time() - 60
        old_meta = self.named_meta("older", "w3:p1", created=old)
        new_meta = self.named_meta("newer", "w3:p2", created=new)
        self.pane_record("b1-old", "older", "w3:p1")
        self.pane_record("b1-new", "newer", "w3:p2")
        notes = new_meta.parent / "notes.md"
        notes.write_text("- 10:00 decided X\n")
        for path, when in ((old_meta, old), (new_meta, new), (notes, new),
                           (self.root / "records" / "b1-old.json", old),
                           (self.root / "records" / "b1-new.json", new)):
            os.utime(path, (when, when))
        box = Path(self.env["NAKAMA_STATE_DIR"]) / "inbox" / "orchestrator" / "newer"
        box.mkdir(parents=True)
        (box / "a.md").write_text("a")
        (box / "b.md").write_text("b")
        (box / "c.read").write_text("c")
        rc, out = self.session_ls()
        self.assertEqual(rc, 0, out)
        self.assertEqual([row["name"] for row in out["sessions"]], ["newer", "older"])
        newer, older = out["sessions"]
        self.assertEqual(newer["inbox_unread"], 2)
        self.assertEqual(newer["notes"]["bytes"], len("- 10:00 decided X\n"))
        self.assertGreater(newer["notes"]["age_minutes"], 0)
        self.assertLess(newer["notes"]["age_minutes"], 3)
        self.assertEqual(older["notes"], {"bytes": 0, "age_minutes": None})
        self.assertEqual(older["inbox_unread"], 0)
        self.assertAlmostEqual(newer["last_seen"], new, places=0)
        self.assertAlmostEqual(older["last_seen"], old, places=0)

    def test_session_ls_lists_unnamed_sessions_with_open_workers(self):
        self.pane_record("b1-open", "w3_p9", "w3:p9")
        self.pane_record("b1-closed", "w3_p8", "w3:p8", closed=True)
        self.set_state(status="idle")
        rc, out = self.session_ls()
        self.assertEqual(rc, 0, out)
        self.assertEqual(out["sessions"], [])
        self.assertEqual([(u["session"], u["pane"], u["workers_open"]) for u in out["unnamed"]],
                         [("w3_p9", "w3:p9", 1)])
        self.assertGreater(out["unnamed"][0]["last_seen"], 0)

    def test_session_ls_legacy_adds_default_and_pane_id_sessions(self):
        self.pane_record("b1-default", None, None)
        self.pane_record("b1-pane", "w3_p9", "w3:p9", closed=True)
        self.set_state(status="idle")
        rc, out = self.session_ls()
        self.assertEqual(rc, 0, out)
        self.assertEqual(out["sessions"], [])
        rc, out = self.session_ls("--legacy")
        self.assertEqual(rc, 0, out)
        rows = {row["name"]: row for row in out["sessions"]}
        self.assertEqual(sorted(rows), ["default", "w3_p9"])
        default = rows["default"]
        self.assertIsNone(default["about"])
        self.assertIsNone(default["created"])
        self.assertTrue(default["legacy"])
        self.assertEqual(default["cwd"], str(self.root))
        self.assertEqual(default["workers"], {"open": 1, "total": 1})
        self.assertEqual(rows["w3_p9"]["workers"], {"open": 0, "total": 1})
        self.assertEqual(rows["w3_p9"]["pane"], "w3:p9")

    def test_session_ls_legacy_survives_odd_herdr_payload(self):
        self.pane_record("b1-pane", "w3_p9", "w3:p9", closed=True)
        self.set_state(status="")
        rc, out = self.session_ls("--legacy")
        self.assertEqual(rc, 0, out)
        rows = {row["name"]: row for row in out["sessions"]}
        self.assertIsInstance(rows["w3_p9"]["alive"], bool)

    def test_session_ls_skips_unreadable_record_files(self):
        self.named_meta("cart-rework", "w3:p1")
        self.pane_record("b1-good", "cart-rework", "w3:p1")
        state = Path(self.env["NAKAMA_STATE_DIR"])
        (state / "bad.json").write_text("{")
        (state / "noname.json").write_text("{}")
        (state / "noclosed.json").write_text(json.dumps(
            {"name": "b1-noclosed", "session": "cart-rework", "orch_pane": "w3:p1"}))
        self.set_state(status="idle")
        rc, out = self.session_ls()
        self.assertEqual(rc, 0, out)
        self.assertEqual(out["sessions"][0]["workers"], {"open": 2, "total": 2})

    def test_session_ls_skips_record_with_non_string_session(self):
        self.named_meta("cart-rework", "w3:p1")
        self.pane_record("b1-good", "cart-rework", "w3:p1")
        (Path(self.env["NAKAMA_STATE_DIR"]) / "b1-broken.json").write_text(json.dumps(
            {"name": "b1-broken", "session": 5, "worktree": str(self.root)}))
        self.set_state(status="idle")
        rc, out = self.session_ls()
        self.assertEqual(rc, 0, out)
        self.assertEqual(out["sessions"][0]["workers"], {"open": 1, "total": 1})

    def test_session_ls_is_read_only(self):
        self.named_meta("cart-rework", "w3:p1")
        self.pane_record("b1-x", "cart-rework", "w3:p1")
        box = Path(self.env["NAKAMA_STATE_DIR"]) / "inbox" / "orchestrator" / "cart-rework"
        box.mkdir(parents=True)
        (box / "unread.md").write_text("hi")
        self.set_state(status="idle")
        state_dir = Path(self.env["NAKAMA_STATE_DIR"])

        def snapshot():
            return sorted((str(p.relative_to(state_dir)), p.stat().st_mtime_ns, p.stat().st_size)
                          for p in state_dir.rglob("*") if p.is_file())

        before = snapshot()
        self.assertEqual(self.session_ls()[0], 0)
        self.assertEqual(self.session_ls("--legacy")[0], 0)
        self.assertEqual(before, snapshot())
        self.assertTrue((box / "unread.md").is_file())

    def test_session_ls_marks_stale_sessions(self):
        self.named_meta("live", "w3:p1")
        self.named_meta("dead", "w3:p2")
        self.named_meta("busy", "w3:p3")
        self.pane_record("b1-live", "live", "w3:p1")
        self.pane_record("b1-busy", "busy", "w3:p3")
        self.set_state(status="idle", statuses={"w3:p2": None, "w3:p3": None})
        rc, out = self.session_ls()
        self.assertEqual(rc, 0, out)
        rows = {row["name"]: row for row in out["sessions"]}
        self.assertFalse(rows["live"]["stale"])
        self.assertTrue(rows["dead"]["stale"])
        self.assertFalse(rows["busy"]["stale"])  # not alive but still has open workers

    def test_session_ls_help_lists_the_command(self):
        text = self.help_text("session")
        self.assertIn("ls", text)
        self.assertIn("list named sessions", text)


    def brief(self, *extra, env=None):
        return self.nakama("brief", *extra, env=env)

    def brief_row(self, name, *extra, env=None):
        rc, out = self.brief(*extra, env=env)
        self.assertEqual(rc, 0, out)
        return next(row for row in out["workers"] if row["name"] == name)

    def test_brief_maps_worker_state_and_result(self):
        self.set_state(status="idle")
        out = self.launch()
        self.task(out)
        self.assertEqual(self.nakama("prompt", out["name"], "task.md")[0], 0)
        rec = self.record(out["name"])
        result = Path(self.result(out))
        result.write_text("Status: succeeded\nNext: review\n")
        stamp = rec["prompted_at"] + 5
        os.utime(result, (stamp, stamp))
        unprompted = self.launch()
        rc, brief = self.brief()
        self.assertEqual(rc, 0, brief)
        rows = {row["name"]: row for row in brief["workers"]}
        row = rows[out["name"]]
        self.assertEqual(row["result"], {"status": "succeeded", "next": "review", "steps": None})
        self.assertEqual(row["state"], "idle")
        self.assertEqual((row["rounds"], row["paused"], row["session"]), (0, False, "w3_p1"))
        self.assertIsNone(rows[unprompted["name"]]["result"])

    def test_brief_counts_blocker_mail_without_consuming_it(self):
        out = self.launch()
        self.orch_mail("w3_p1", "blocked on you", kind="blocker")
        box = Path(out["dir"], "inbox")
        box.mkdir(parents=True)
        mail = box / f"{time.time_ns()}.orch.info.md"
        mail.write_text("look")
        for _ in range(2):
            self.assertEqual(self.brief_row(out["name"])["mail"], {"unread": 1, "kinds": ["info"]})
            self.assertEqual(self.brief()[1]["unread_orchestrator_mail"]["count"], 1)
        self.assertEqual(list(box.glob("*.md")), [mail])
        self.assertTrue(all(p.suffix == ".md" for p in self.orch_box("w3_p1").glob("*")))

    def test_brief_reports_worktree_dirty_then_clean(self):
        wt = self.root / "dirty-wt"
        wt.mkdir()
        self.wgit(wt, "init", "-q")
        self.wgit(wt, "commit", "-q", "--allow-empty", "-m", "root")
        self.pane_record("b1-dirty", "w3:p1", "w3:p1", worktree=str(wt))
        self.set_state(status="idle")
        Path(wt, "scratch.txt").write_text("x")
        self.assertTrue(self.brief_row("b1-dirty")["worktree_dirty"])
        Path(wt, "scratch.txt").unlink()
        self.assertFalse(self.brief_row("b1-dirty")["worktree_dirty"])
        shutil.rmtree(wt)
        self.assertFalse(self.brief_row("b1-dirty")["worktree_dirty"])

    def test_brief_cross_kind_for_claude_token_in_pi_pane(self):
        token = {"agent": "claude", "kind": "id", "value": "5f0c-uuid",
                 "source": "herdr:claude", "cwd": "/x"}
        self.named_meta("cart-rework", "w3:p1", agent=token)
        self.set_state(status="idle",
                       agent_session={"agent": "pi", "kind": "path", "value": "/tmp/s.jsonl"})
        rc, brief = self.brief()
        self.assertEqual(rc, 0, brief)
        self.assertEqual(brief["session"]["name"], "cart-rework")
        self.assertEqual(brief["transcript"], {"agent": "claude", "kind": "id", "value": "5f0c-uuid",
                                               "resume_with": "claude --resume 5f0c-uuid",
                                               "cross_kind": True})

    def test_brief_warns_unnamed_session(self):
        rc, brief = self.brief()
        self.assertEqual(rc, 0, brief)
        self.assertIsNone(brief["orchestrator"])
        self.assertIsNone(brief["transcript"])
        self.assertEqual((brief["session"]["name"], brief["session"]["source"],
                          brief["session"]["adopt"]), ("w3_p1", "pane_id", None))
        self.assertEqual(brief["warnings"],
                         ["unnamed_session: run 'nakama session start <name> --about \"<one line>\"'",
                          "no_handoff_notes"])

    def test_brief_pane_bound_session_offers_adopt_and_flags_old_surface(self):
        self.named_meta("cart-rework", "w3:p2", agent=None)
        self.set_state(status="idle")
        rc, brief = self.brief(env={**self.env, "NAKAMA_SESSION": "cart-rework"})
        self.assertEqual(rc, 0, brief)
        self.assertEqual(brief["session"]["source"], "env")
        self.assertIsNone(brief["session"]["adopt"])
        self.assertEqual(brief["orchestrator"],
                         {"pane": "w3:p2", "status": "idle", "maybe_alive": True})
        self.assertIn("old_orchestrator_surface_may_be_alive (w3:p2)", brief["warnings"])
        rc, brief = self.brief(env={**self.env, "HERDR_PANE_ID": "w3:p2"})
        self.assertEqual(rc, 0, brief)
        self.assertEqual(brief["session"]["source"], "pane")
        self.assertEqual(brief["session"]["adopt"], "export NAKAMA_SESSION=cart-rework")
        self.assertFalse(brief["orchestrator"]["maybe_alive"])
        self.assertNotIn("old_orchestrator_surface_may_be_alive (w3:p2)", brief["warnings"])

    def test_brief_reports_handoff_notes(self):
        self.named_meta("cart-rework", "w3:p1")
        self.set_state(status="idle")
        env = {**self.env, "NAKAMA_SESSION": "cart-rework"}
        notes = Path(self.env["NAKAMA_STATE_DIR"]) / "sessions" / "cart-rework" / "notes.md"
        notes.write_text("- 10:00 did a thing\n")
        rc, brief = self.brief(env=env)
        self.assertEqual(rc, 0, brief)
        self.assertTrue(brief["notes"]["exists"])
        self.assertEqual(brief["notes"]["path"], str(notes))
        self.assertIsInstance(brief["notes"]["age_minutes"], float)
        self.assertGreaterEqual(brief["notes"]["age_minutes"], 0.0)
        self.assertNotIn("no_handoff_notes", brief["warnings"])
        notes.unlink()
        rc, brief = self.brief(env=env)
        self.assertFalse(brief["notes"]["exists"])
        self.assertIsNone(brief["notes"]["age_minutes"])
        self.assertIn("no_handoff_notes", brief["warnings"])

    def note_env(self, key="cart-rework"):
        self.named_meta(key, "w3:p1")
        return {**self.env, "NAKAMA_SESSION": key}

    def notes_path(self, key="cart-rework"):
        return Path(self.env["NAKAMA_STATE_DIR"]) / "sessions" / key / "notes.md"

    def test_note_creates_file_with_heading_then_appends(self):
        env = self.note_env()
        notes = self.notes_path()
        rc, first = self.nakama("note", "first", env=env)
        self.assertEqual(rc, 0, first)
        self.assertTrue(first["heading_added"])
        self.assertTrue(notes.is_file())
        self.assertEqual(notes.stat().st_mode & 0o777, 0o600)
        rc, second = self.nakama("note", "second", env=env)
        self.assertEqual(rc, 0, second)
        self.assertFalse(second["heading_added"])
        content = notes.read_text()
        today = re.escape(time.strftime("%Y-%m-%d"))
        self.assertRegex(content, rf"^## {today}\n\n- \d\d:\d\d first\n- \d\d:\d\d second\n\Z")
        self.assertEqual(second["line"], content.splitlines()[-1])

    def test_note_starts_new_heading_on_a_new_day(self):
        env = self.note_env()
        notes = self.notes_path()
        today = re.escape(time.strftime("%Y-%m-%d"))
        notes.write_text("## 2000-01-01\n\n- 09:00 old\n")
        rc, out = self.nakama("note", "new", env=env)
        self.assertEqual(rc, 0, out)
        self.assertTrue(out["heading_added"])
        self.assertRegex(notes.read_text(),
                         rf"\A## 2000-01-01\n\n- 09:00 old\n\n## {today}\n\n- \d\d:\d\d new\n\Z")
        notes.write_text("## 2000-01-01\n\n- 09:00 old")
        rc, out = self.nakama("note", "new", env=env)
        self.assertEqual(rc, 0, out)
        self.assertTrue(out["heading_added"])
        self.assertRegex(notes.read_text(),
                         rf"\A## 2000-01-01\n\n- 09:00 old\n\n## {today}\n\n- \d\d:\d\d new\n\Z")

    def test_note_repairs_missing_trailing_newline(self):
        env = self.note_env()
        notes = self.notes_path()
        today = re.escape(time.strftime("%Y-%m-%d"))
        notes.write_text(f"## {time.strftime('%Y-%m-%d')}\n\n- 09:00 old")
        rc, out = self.nakama("note", "new", env=env)
        self.assertEqual(rc, 0, out)
        self.assertFalse(out["heading_added"])
        self.assertRegex(notes.read_text(),
                         rf"\A## {today}\n\n- 09:00 old\n- \d\d:\d\d new\n\Z")

    def test_note_refuses_unnamed_session(self):
        rc, out = self.nakama("note", "hello")
        self.assertEqual(rc, 2, out)
        self.assertIn("session start", out["error"])
        self.assertFalse((Path(self.env["NAKAMA_STATE_DIR"]) / "sessions" / "w3_p1").exists())

    def test_note_reaches_brief(self):
        env = self.note_env()
        self.set_state(status="idle")
        rc, out = self.nakama("note", "decided X", env=env)
        self.assertEqual(rc, 0, out)
        rc, brief = self.brief(env=env)
        self.assertEqual(rc, 0, brief)
        self.assertTrue(brief["notes"]["exists"])
        self.assertEqual(brief["notes"]["path"], out["path"])
        self.assertNotIn("no_handoff_notes", brief["warnings"])
        self.assertIn("decided X", Path(out["path"]).read_text())

    def test_note_write_failure_is_rc_2(self):
        env = self.note_env()
        notes = self.notes_path()
        notes.mkdir()
        rc, out = self.nakama("note", "hello", env=env)
        self.assertEqual(rc, 2, out)
        self.assertTrue(out["error"].startswith("cannot write notes"), out)

    def test_note_rejects_empty_and_multiline_text(self):
        env = self.note_env()
        notes = self.notes_path()
        rc, out = self.nakama("note", "   ", env=env)
        self.assertEqual(rc, 2, out)
        self.assertIn("empty", out["error"])
        rc, out = self.nakama("note", "a\nb", env=env)
        self.assertEqual(rc, 2, out)
        self.assertIn("one line", out["error"])
        rc, out = self.nakama("note", "x\n## 2099-01-01", env=env)
        self.assertEqual(rc, 2, out)
        self.assertFalse(notes.exists())

    def test_note_help_listed(self):
        text = self.help_text()
        self.assertIn("append a timestamped line", text)

    def test_note_stores_text_verbatim(self):
        env = self.note_env()
        notes = self.notes_path()
        text = "-x and \u2028 and \u00e9 and \u2028## 2099-01-01"
        rc, out = self.nakama("note", "--", text, env=env)
        self.assertEqual(rc, 0, out)
        self.assertIn(text, notes.read_text())
        self.assertEqual(out["line"].split(" ", 2)[2], text)
        rc, out = self.nakama("note", "same day", env=env)
        self.assertEqual(rc, 0, out)
        self.assertFalse(out["heading_added"])

    def test_brief_lists_only_this_sessions_workers(self):
        mine = self.launch()
        self.pane_record("b1-other", "another-run", "w3:p2")
        self.set_state(status="idle")
        rc, brief = self.brief()
        self.assertEqual(rc, 0, brief)
        self.assertEqual([row["name"] for row in brief["workers"]], [mine["name"]])

    def test_brief_closed_includes_closed_workers(self):
        self.pane_record("b1-open", "w3:p1", "w3:p1")
        self.pane_record("b1-done", "w3:p1", "w3:p1", closed=True)
        self.set_state(status="idle")
        self.assertEqual([row["name"] for row in self.brief()[1]["workers"]], ["b1-open"])
        rc, brief = self.brief("--closed")
        self.assertEqual(rc, 0, brief)
        rows = {row["name"]: row for row in brief["workers"]}
        self.assertEqual(sorted(rows), ["b1-done", "b1-open"])
        self.assertIsNone(rows["b1-done"]["state"])

    def test_brief_is_read_only(self):
        self.named_meta("cart-rework", "w3:p1")
        self.pane_record("b1-x", "cart-rework", "w3:p1")
        worker_inbox = Path(self.root, ".agent", "b1-x", "inbox")
        worker_inbox.mkdir(parents=True)
        (worker_inbox / f"{time.time_ns()}.orchestrator.info.md").write_text("note")
        box = Path(self.env["NAKAMA_STATE_DIR"]) / "inbox" / "orchestrator" / "cart-rework"
        box.mkdir(parents=True)
        (box / "unread.md").write_text("hi")
        self.set_state(status="idle")
        env = {**self.env, "NAKAMA_SESSION": "cart-rework"}
        state_dir = Path(self.env["NAKAMA_STATE_DIR"])

        def snap():
            rows = []
            for label, top in (("state", state_dir), ("agent", self.root / ".agent")):
                if not top.is_dir():
                    continue
                for path in [top, *top.rglob("*")]:
                    st = path.stat()
                    rows.append((label, str(path.relative_to(top)), st.st_mtime_ns,
                                 None if path.is_dir() else st.st_size))
            return sorted(rows)

        before = snap()
        self.assertEqual(self.brief(env=env)[0], 0)
        self.assertEqual(self.brief("--closed", env=env)[0], 0)
        self.assertEqual(before, snap())
        self.assertTrue((box / "unread.md").is_file())

    def test_brief_does_not_rewrite_worktree_git_index(self):
        wt = self.root / "index-wt"
        wt.mkdir()
        self.wgit(wt, "init", "-q")
        Path(wt, "tracked.txt").write_text("v1")
        self.wgit(wt, "add", "tracked.txt")
        self.wgit(wt, "commit", "-q", "-m", "root")
        self.pane_record("b1-index", "w3:p1", "w3:p1", worktree=str(wt))
        self.set_state(status="idle")
        index = wt / ".git" / "index"
        # A stale stat cache makes plain `git status` refresh and rewrite the index.
        stale = time.time() + 10
        os.utime(wt / "tracked.txt", (stale, stale))
        before = (index.stat().st_mtime_ns, index.stat().st_size)
        self.brief_row("b1-index")
        self.brief_row("b1-index")
        self.assertEqual((index.stat().st_mtime_ns, index.stat().st_size), before)

    def test_brief_survives_bad_records_and_odd_herdr(self):
        self.pane_record("b1-ok", "w3:p1", "w3:p1")
        state = Path(self.env["NAKAMA_STATE_DIR"])
        (state / "b1-noworktree.json").write_text(json.dumps(
            {"name": "b1-noworktree", "role": "builder", "session": "w3:p1"}))
        (state / "b1-broken.json").write_text(json.dumps(
            {"name": "b1-broken", "session": 5, "worktree": str(self.root)}))
        (state / "broken.json").write_text("{")
        self.set_state(status="")
        rc, brief = self.brief()
        self.assertEqual(rc, 0, brief)
        rows = {row["name"]: row for row in brief["workers"]}
        self.assertEqual(sorted(rows), ["b1-noworktree", "b1-ok"])
        self.assertEqual(rows["b1-ok"]["state"], "")
        self.assertIn("error", rows["b1-noworktree"])
        self.assertIsNone(rows["b1-noworktree"]["result"])
        self.assertFalse(rows["b1-noworktree"]["worktree_dirty"])
        self.assertEqual(rows["b1-noworktree"]["mail"], {"unread": 0, "kinds": []})

    def test_brief_help_listed(self):
        text = self.help_text()
        self.assertIn("brief", text)
        self.assertIn("read-only bearings", text)

    def write_jsonl(self, path, lines):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(json.dumps(line) for line in lines) + "\n")
        return path

    def pi_meta(self, name, path):
        self.named_meta(name, "w3:p1", agent={"agent": "pi", "kind": "path", "value": str(path),
                                              "source": "herdr:pi", "cwd": str(self.root)})
        return {**self.env, "NAKAMA_SESSION": name}

    def claude_meta(self, name, uuid, home):
        self.named_meta(name, "w3:p1", agent={"agent": "claude", "kind": "id", "value": uuid,
                                              "source": "herdr:claude", "cwd": str(self.root)})
        return {**self.env, "NAKAMA_SESSION": name, "HOME": str(home)}

    def digest(self, *extra, env=None):
        return self.nakama("digest", *extra, env=env)

    def test_digest_renders_pi_transcript(self):
        lines = [
            {"type": "session", "version": 3, "id": "s1", "cwd": "/x"},
            {"type": "model_change", "model": "m"},
            {"type": "message", "timestamp": "2026-01-01T00:00:01Z",
             "message": {"role": "user", "content": [{"type": "text", "text": "hello prompt"}]}},
            {"type": "message", "timestamp": "2026-01-01T00:00:02Z",
             "message": {"role": "assistant", "content": [
                 {"type": "thinking", "thinking": "secret-thought"},
                 {"type": "text", "text": "answer text"},
                 {"type": "toolCall", "name": "bash", "arguments": {"command": "ls"}}]}},
            {"type": "message", "timestamp": "2026-01-01T00:00:03Z",
             "message": {"role": "toolResult", "toolName": "bash",
                         "content": [{"type": "text", "text": "file.txt"}]}},
            {"type": "compaction", "timestamp": "2026-01-01T00:00:04Z", "summary": "earlier summary"},
        ]
        path = self.write_jsonl(self.root / "pi.jsonl", lines)
        rc, out = self.digest("--stdout", env=self.pi_meta("cart-rework", path))
        self.assertEqual(rc, 0, out)
        text = out["text"]
        self.assertEqual(out["messages"], 4)
        self.assertEqual((out["matched"], out["total"]), (4, 4))
        self.assertIsNone(out["path"])
        self.assertFalse(out["truncated"])
        self.assertLess(text.index("### user · 2026-01-01T00:00:01Z"), text.index("### assistant"))
        self.assertNotIn("secret-thought", text)
        self.assertIn("`bash({\"command\": \"ls\"})`", text)
        self.assertIn("`bash: file.txt`", text)
        self.assertLess(text.index("### assistant"), text.index("### tool"))
        self.assertLess(text.index("### tool"), text.index("### summary"))
        self.assertIn("earlier summary", text)

    def test_digest_renders_claude_transcript(self):
        home = self.root / "home"
        uuid = "5f0c-uuid"
        lines = [
            {"type": "mode", "mode": "default"},
            {"type": "user", "timestamp": "t1", "message": {"role": "user", "content": "plain prompt"}},
            {"type": "assistant", "timestamp": "t2", "message": {"role": "assistant", "content": [
                {"type": "thinking", "thinking": "hidden-thought"},
                {"type": "text", "text": "claude answer"},
                {"type": "tool_use", "name": "Bash", "input": {"command": "pwd"}}]}},
            {"type": "user", "timestamp": "t3", "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "tu1", "content": "output text"}]}},
            {"type": "api-request-blob",
             "message": {"role": "user", "content": [{"type": "text", "text": "blob-secret"}]}},
            {"type": "assistant", "isSidechain": True, "timestamp": "t4",
             "message": {"role": "assistant", "content": [{"type": "text", "text": "sidechain-secret"}]}},
        ]
        path = home / ".claude" / "projects" / "-x" / f"{uuid}.jsonl"
        self.write_jsonl(path, lines)
        rc, out = self.digest("--stdout", env=self.claude_meta("cart-rework", uuid, home))
        self.assertEqual(rc, 0, out)
        text = out["text"]
        self.assertEqual(out["messages"], 3)
        self.assertIn("plain prompt", text)
        self.assertIn("`Bash({\"command\": \"pwd\"})`", text)
        self.assertIn("`result: output text`", text)
        self.assertIn("### tool · t3", text)
        self.assertNotIn("blob-secret", text)
        self.assertNotIn("sidechain-secret", text)
        self.assertNotIn("hidden-thought", text)

    def test_digest_truncates_tool_args_and_assistant_text(self):
        arg = "a" * 300
        assistant = "a" * 3000 + "needle" + "b" * 994
        lines = [
            {"type": "message", "timestamp": "t1", "message": {"role": "user",
             "content": [{"type": "text", "text": "u" * 4000}]}},
            {"type": "message", "timestamp": "t2", "message": {"role": "assistant",
             "content": [{"type": "text", "text": assistant}]}},
            {"type": "message", "timestamp": "t3", "message": {"role": "assistant",
             "content": [{"type": "toolCall", "name": "bash", "arguments": {"x": arg}}]}},
        ]
        path = self.write_jsonl(self.root / "pi.jsonl", lines)
        env = self.pi_meta("cart-rework", path)
        rc, out = self.digest("--stdout", env=env)
        self.assertEqual(rc, 0, out)
        text = out["text"]
        self.assertIn("u" * 4000, text)
        expected_preview = json.dumps({"x": arg}, ensure_ascii=False)[:120] + "…"
        self.assertIn(f"`bash({expected_preview})`", text)
        self.assertIn(assistant.encode()[:1536].decode() + " … [cut]", text)
        rc, out = self.digest("--stdout", "--grep", "needle", env=env)
        self.assertEqual(rc, 0, out)
        self.assertEqual((out["messages"], out["matched"]), (1, 1))

    def test_digest_max_bytes_drops_oldest_first(self):
        lines = [{"type": "message", "timestamp": "t", "message": {
            "role": "user", "content": [{"type": "text", "text": f"msg-{i:02d}" + "x" * 94}]}}
            for i in range(50)]
        path = self.write_jsonl(self.root / "pi.jsonl", lines)
        rc, out = self.digest("--stdout", "--max-bytes", "2048", env=self.pi_meta("cart-rework", path))
        self.assertEqual(rc, 0, out)
        text = out["text"]
        self.assertIn("msg-49", text)
        self.assertNotIn("msg-00", text)
        self.assertGreater(out["dropped"], 0)
        self.assertEqual(out["messages"], text.count("### user"))
        self.assertIn(f"… {out['dropped']} older messages dropped to fit 2048 bytes", text)
        self.assertLessEqual(out["bytes"], 2048)
        self.assertEqual(out["bytes"], len(text.encode()))

    def test_digest_single_message_over_cap_is_truncated(self):
        lines = [f"line-{i:03d}-" + "y" * 71 for i in range(70)]
        path = self.write_jsonl(self.root / "pi.jsonl", [
            {"type": "message", "timestamp": "t", "message": {"role": "user",
             "content": [{"type": "text", "text": "\n".join(lines)}]}}])
        rc, out = self.digest("--stdout", "--max-bytes", "1024", env=self.pi_meta("cart-rework", path))
        self.assertEqual(rc, 0, out)
        self.assertTrue(out["truncated"])
        self.assertLessEqual(out["bytes"], 1024)
        footer = "\n… truncated at 1024 bytes\n"
        self.assertTrue(out["text"].endswith(footer))
        before = out["text"][:-len(footer)]
        self.assertIn(before.split("\n")[-1], lines)

    def test_digest_grep_filters_before_tail(self):
        lines = [{"type": "message", "timestamp": "t", "message": {"role": "user",
                  "content": [{"type": "text", "text": body}]}}
                 for body in ("needle one", "needle two", "after no match")]
        path = self.write_jsonl(self.root / "pi.jsonl", lines)
        rc, out = self.digest("--stdout", "--grep", "needle", "--tail", "1",
                              env=self.pi_meta("cart-rework", path))
        self.assertEqual(rc, 0, out)
        self.assertEqual((out["messages"], out["matched"], out["total"]), (1, 2, 3))
        self.assertIn("needle two", out["text"])
        self.assertNotIn("needle one", out["text"])
        self.assertNotIn("after no match", out["text"])

    def test_digest_writes_file_and_stdout_writes_nothing(self):
        path = self.write_jsonl(self.root / "pi.jsonl", [
            {"type": "message", "timestamp": "t", "message": {"role": "user",
             "content": [{"type": "text", "text": "hello"}]}}])
        env = self.pi_meta("cart-rework", path)
        rc, out = self.digest(env=env)
        self.assertEqual(rc, 0, out)
        digest_path = Path(self.env["NAKAMA_STATE_DIR"]) / "sessions" / "cart-rework" / "digest.md"
        self.assertEqual(out["path"], str(digest_path))
        self.assertTrue(digest_path.is_file())
        self.assertIn("hello", digest_path.read_text())
        self.assertNotIn("text", out)
        digest_path.unlink()
        rc, out = self.digest("--stdout", env=env)
        self.assertEqual(rc, 0, out)
        self.assertIsNone(out["path"])
        self.assertIn("hello", out["text"])
        self.assertFalse(digest_path.exists())
        self.assertEqual(list(digest_path.parent.glob("*.tmp")), [])

    def test_digest_missing_transcript_names_path(self):
        missing = self.root / "nope.jsonl"
        rc, out = self.digest("--stdout", env=self.pi_meta("cart-rework", missing))
        self.assertEqual(rc, 2, out)
        self.assertIn(str(missing), out["error"])
        home = self.root / "home"
        uuid = "abc-123"
        rc, out = self.digest("--stdout", env=self.claude_meta("cart-rework", uuid, home))
        self.assertEqual(rc, 2, out)
        self.assertIn("projects/*/", out["error"])
        self.assertIn(uuid, out["error"])

    def test_digest_requires_named_session_and_token(self):
        rc, out = self.digest("--stdout")
        self.assertEqual(rc, 2, out)
        self.assertIn("nakama session start", out["error"])
        self.named_meta("cart-rework", "w3:p1", agent=None)
        rc, out = self.digest("--stdout", env={**self.env, "NAKAMA_SESSION": "cart-rework"})
        self.assertEqual(rc, 2, out)
        self.assertIn("transcript", out["error"])

    def test_digest_skips_corrupt_lines(self):
        good = {"type": "message", "timestamp": "t1", "message": {
            "role": "user", "content": [{"type": "text", "text": "good"}]}}
        no_ts = {"type": "message", "message": {
            "role": "user", "content": [{"type": "text", "text": "no ts"}]}}
        pi_bytes = b"\n".join([
            json.dumps(good).encode(),
            json.dumps({"type": "message"}).encode(),
            json.dumps({"type": "message", "message": {"role": "user", "content": None}}).encode(),
            json.dumps({"type": "message", "message": {
                "role": "user", "content": [{"type": "bogus"}]}}).encode(),
            json.dumps(no_ts).encode(),
            b'{"type": "message"',
            b'"\xc3',
        ])
        path = self.root / "pi.jsonl"
        path.write_bytes(pi_bytes)
        rc, out = self.digest("--stdout", env=self.pi_meta("cart-rework", path))
        self.assertEqual(rc, 0, out)
        self.assertEqual(out["messages"], 2)
        self.assertIn("good", out["text"])
        self.assertIn("### user · ?", out["text"])

        good = {"type": "user", "timestamp": "t1", "message": {"role": "user", "content": "good"}}
        no_ts = {"type": "user", "message": {
            "role": "user", "content": [{"type": "text", "text": "no ts"}]}}
        claude_bytes = b"\n".join([
            json.dumps(good).encode(),
            json.dumps({"type": "user"}).encode(),
            json.dumps({"type": "user", "message": {"role": "user", "content": None}}).encode(),
            json.dumps({"type": "user", "message": {
                "role": "user", "content": [{"type": "bogus"}]}}).encode(),
            json.dumps(no_ts).encode(),
            b'{"type": "user"',
            b'"\xc3',
        ])
        home = self.root / "home"
        uuid = "corrupt-1"
        claude_path = home / ".claude" / "projects" / "-x" / f"{uuid}.jsonl"
        claude_path.parent.mkdir(parents=True)
        claude_path.write_bytes(claude_bytes)
        rc, out = self.digest("--stdout", env=self.claude_meta("cart-rework", uuid, home))
        self.assertEqual(rc, 0, out)
        self.assertEqual(out["messages"], 2)
        self.assertIn("good", out["text"])
        self.assertIn("### user · ?", out["text"])

    def test_digest_rejects_bad_options(self):
        path = self.write_jsonl(self.root / "pi.jsonl", [
            {"type": "message", "timestamp": "t", "message": {"role": "user",
             "content": [{"type": "text", "text": "hello"}]}}])
        env = self.pi_meta("cart-rework", path)
        for extra in (["--grep", "["], ["--tail", "0"], ["--max-bytes", "10"]):
            rc, out = self.digest("--stdout", *extra, env=env)
            self.assertEqual(rc, 2, (extra, out))
            self.assertIn("error", out)

    def test_digest_help_listed(self):
        text = self.help_text()
        self.assertIn("digest", text)
        self.assertIn("markdown digest", text)

    def test_digest_empty_and_no_match(self):
        path = self.root / "empty.jsonl"
        path.write_text("")
        rc, out = self.digest("--stdout", env=self.pi_meta("cart-rework", path))
        self.assertEqual(rc, 0, out)
        self.assertEqual((out["messages"], out["dropped"], out["truncated"]), (0, 0, False))
        self.assertIn("# Digest: cart-rework", out["text"])
        path = self.write_jsonl(self.root / "pi.jsonl", [
            {"type": "message", "timestamp": "t", "message": {"role": "user",
             "content": [{"type": "text", "text": "hello"}]}}])
        rc, out = self.digest("--stdout", "--grep", "absent", env=self.pi_meta("cart-rework", path))
        self.assertEqual(rc, 0, out)
        self.assertEqual((out["messages"], out["matched"]), (0, 0))
        self.assertIn("# Digest: cart-rework", out["text"])

    def test_digest_claude_token_validation(self):
        env = {**self.env, "NAKAMA_SESSION": "cart-rework"}
        self.named_meta("cart-rework", "w3:p1", agent={
            "agent": "claude", "kind": "path", "value": "x", "source": "s", "cwd": "/x"})
        rc, out = self.digest("--stdout", env=env)
        self.assertEqual(rc, 2, out)
        self.assertIn("error", out)
        self.named_meta("cart-rework", "w3:p1", agent={
            "agent": "claude", "kind": "id", "value": "../x", "source": "s", "cwd": "/x"})
        rc, out = self.digest("--stdout", env=env)
        self.assertEqual(rc, 2, out)
        self.assertIn("error", out)

    def test_digest_mixed_claude_user_line(self):
        home = self.root / "home"
        uuid = "mix-1"
        self.write_jsonl(home / ".claude" / "projects" / "-x" / f"{uuid}.jsonl", [
            {"type": "user", "timestamp": "t1", "message": {"role": "user", "content": [
                {"type": "text", "text": "please run"},
                {"type": "tool_result", "tool_use_id": "tu1",
                 "content": [{"type": "text", "text": "the output"}]}]}}])
        rc, out = self.digest("--stdout", env=self.claude_meta("cart-rework", uuid, home))
        self.assertEqual(rc, 0, out)
        text = out["text"]
        self.assertIn("### user · t1", text)
        self.assertLess(text.index("please run"), text.index("the output"))
        self.assertIn("`result: the output`", text)

    def test_digest_file_mode_and_write_failure(self):
        path = self.write_jsonl(self.root / "pi.jsonl", [
            {"type": "message", "timestamp": "t", "message": {"role": "user",
             "content": [{"type": "text", "text": "hello"}]}}])
        env = self.pi_meta("cart-rework", path)
        rc, out = self.digest(env=env)
        self.assertEqual(rc, 0, out)
        digest_path = Path(out["path"])
        self.assertEqual(os.stat(digest_path).st_mode & 0o777, 0o600)
        digest_path.unlink()
        session = digest_path.parent
        session.chmod(0o500)
        try:
            rc, out = self.digest(env=env)
        finally:
            session.chmod(0o700)
        self.assertEqual(rc, 2, out)
        self.assertIn(str(digest_path), out["error"])
        self.assertEqual(sorted(p.name for p in session.iterdir()), ["meta.json"])

    def test_digest_sanitises_lone_surrogates(self):
        path = self.write_jsonl(self.root / "pi.jsonl", [
            {"type": "message", "timestamp": "t1", "message": {"role": "user",
             "content": [{"type": "text", "text": "bad \ud83d prompt"}]}},
            {"type": "message", "timestamp": "t2", "message": {"role": "toolResult",
             "toolName": "bash", "content": [{"type": "text", "text": "tool \ud83d out"}]}},
        ])
        rc, out = self.digest("--stdout", env=self.pi_meta("cart-rework", path))
        self.assertEqual(rc, 0, out)
        self.assertEqual(out["messages"], 2)
        self.assertIn("bad ", out["text"])
        self.assertIn("prompt", out["text"])
        self.assertIn("out", out["text"])
        self.assertEqual(out["bytes"], len(out["text"].encode()))

    def test_digest_truncates_long_header_without_messages(self):
        deep = self.root
        for _ in range(8):
            deep = deep / ("d" * 100)
        deep.mkdir(parents=True)
        path = deep / "pi.jsonl"
        path.write_text("")
        self.assertGreater(len(str(path)), 700)
        name = "c" * 200  # a long key pushes the header past --max-bytes on its own
        rc, out = self.digest("--stdout", "--max-bytes", "1024", "--grep", "absent",
                              env=self.pi_meta(name, path))
        self.assertEqual(rc, 0, out)
        self.assertEqual(out["messages"], 0)
        self.assertTrue(out["truncated"])
        self.assertLessEqual(out["bytes"], 1024)
        self.assertEqual(out["bytes"], len(out["text"].encode()))

    def test_digest_rejects_pathological_regex(self):
        path = self.write_jsonl(self.root / "pi.jsonl", [
            {"type": "message", "timestamp": "t", "message": {"role": "user",
             "content": [{"type": "text", "text": "a"}]}}])
        rc, out = self.digest("--stdout", "--grep", "a{99999999999}",
                              env=self.pi_meta("cart-rework", path))
        self.assertEqual(rc, 2, out)
        self.assertIn("error", out)
        rc, out = self.digest("--stdout", "--grep", "a{99999999999}")
        self.assertEqual(rc, 2, out)
        self.assertIn("error", out)

    def test_digest_skips_deeply_nested_line(self):
        path = self.root / "pi.jsonl"
        good = json.dumps({"type": "message", "timestamp": "t", "message": {
            "role": "user", "content": [{"type": "text", "text": "good"}]}}).encode()
        path.write_bytes(good + b"\n" + b"[" * 100000 + b"\n")
        rc, out = self.digest("--stdout", env=self.pi_meta("cart-rework", path))
        self.assertEqual(rc, 0, out)
        self.assertEqual(out["messages"], 1)
        self.assertIn("good", out["text"])

    def test_digest_pi_path_with_nul_names_path(self):
        path = str(self.root / "bad\x00.jsonl")
        rc, out = self.digest("--stdout", env=self.pi_meta("cart-rework", path))
        self.assertEqual(rc, 2, out)
        self.assertIn("bad\x00.jsonl", out["error"])

    def test_digest_head_sanitises_surrogate_transcript_path(self):
        loader = importlib.machinery.SourceFileLoader("nakama_digest_head", str(NAKAMA))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        nakama = importlib.util.module_from_spec(spec)
        with mock.patch.dict(os.environ, self.env):
            loader.exec_module(nakama)
        head = nakama.digest_head("k", "/x/\udcff.jsonl", "pi", 0, 0, 0, 0, 1024)
        head.encode()  # a lone surrogate in the path must not reach a strict encode
        self.assertIn("/x/", head)


    def claim_file(self, pane_key):
        path = Path(self.env["NAKAMA_STATE_DIR"]) / "sessions" / ".by-pane" / pane_key
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def takeover(self, name, env=None):
        return self.nakama("takeover", name, env=env or {**self.env, "HERDR_PANE_ID": "w3:p2"})

    def test_takeover_binds_pane_and_repoints_records(self):
        token = {"agent": "pi", "kind": "path", "value": "/t/s.jsonl"}
        meta_path = self.named_meta("cart-rework", "w3:p1", agent=token)
        self.pane_record("b1-a", "cart-rework", "w3:p1")
        self.pane_record("b1-b", "cart-rework", "w3:p1", closed=True)
        self.pane_record("b1-other", "other", "w3:p1")
        self.pane_record("b1-legacy", None, "w3:p1")
        untouched = {name: self.record_bytes(name) for name in ("b1-other", "b1-legacy")}
        self.set_state(statuses={"w3:p2": "idle"})
        env = {**self.env, "HERDR_PANE_ID": "w3:p2"}
        rc, out = self.nakama("takeover", "cart-rework", env=env)
        self.assertEqual(rc, 0, out)
        self.assertFalse(out["already_owner"])
        self.assertEqual(out["previous_pane"], "w3:p1")
        self.assertEqual((out["repointed"], out["skipped"]), (2, []))
        self.assertEqual(self.record("b1-a")["orch_pane"], "w3:p2")
        self.assertEqual(self.record("b1-b")["orch_pane"], "w3:p2")
        for name, before in untouched.items():
            self.assertEqual(self.record_bytes(name), before, name)
        meta = json.loads(meta_path.read_text())
        self.assertEqual(meta["owner_pane"], "w3:p2")
        self.assertEqual(meta["agent"], token)
        adoption = meta["adoptions"][-1]
        self.assertEqual((adoption["pane"], adoption["from"], adoption["agent"]),
                         ("w3:p2", "w3:p1", token))
        self.assertIsInstance(adoption["at"], float)
        self.assertEqual(self.claim_file("w3_p2").read_text(), "cart-rework")
        self.assertEqual(out["export"], "export NAKAMA_SESSION=cart-rework")
        self.assertEqual(out["next"], "nakama brief")
        self.assertEqual(out["transcript"]["resume_with"], "pi --session /t/s.jsonl")
        self.assertEqual(out["orchestrator"], {"pane": "w3:p1", "maybe_alive": False,
                                                "close_with": None})
        self.assertEqual(out["warnings"], [])
        rc, brief = self.nakama("brief", env=env)
        self.assertEqual(rc, 0, brief)
        self.assertEqual((brief["session"]["name"], brief["session"]["source"]),
                         ("cart-rework", "pane"))

    def record_bytes(self, name):
        return (self.root / "records" / f"{name}.json").read_bytes()

    def test_takeover_warns_when_old_pane_alive(self):
        self.named_meta("cart-rework", "w3:p1", agent=None)
        self.set_state(status="idle")
        rc, out = self.takeover("cart-rework")
        self.assertEqual(rc, 0, out)
        self.assertIsNone(out["transcript"])
        self.assertEqual(out["orchestrator"], {"pane": "w3:p1", "maybe_alive": True,
                                                "close_with": "herdr pane close w3:p1"})
        self.assertIn("old_orchestrator_surface_may_be_alive (w3:p1)", out["warnings"])

    def test_takeover_removes_old_claim_only_if_it_is_ours(self):
        self.set_state(statuses={"w3:p2": "idle"})
        self.named_meta("cart-rework", "w3:p1")
        self.claim_file("w3_p1").write_text("cart-rework")
        rc, out = self.takeover("cart-rework")
        self.assertEqual(rc, 0, out)
        self.assertFalse(self.claim_file("w3_p1").exists())

        shutil.rmtree(Path(self.env["NAKAMA_STATE_DIR"]) / "sessions")
        self.named_meta("cart-rework", "w3:p1")
        self.claim_file("w3_p1").write_text("other")
        rc, out = self.takeover("cart-rework")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.claim_file("w3_p1").read_text(), "other")

    def test_takeover_already_owner_writes_nothing(self):
        meta_path = self.named_meta("cart-rework", "w3:p1")
        self.pane_record("b1-a", "cart-rework", "w3:p1")
        self.set_state(statuses={"w3:p2": "idle"})
        rc, first = self.takeover("cart-rework")
        self.assertEqual(rc, 0, first)
        meta_before = meta_path.read_bytes()
        record_before = self.record_bytes("b1-a")
        rc, second = self.takeover("cart-rework")
        self.assertEqual(rc, 0, second)
        self.assertTrue(second["already_owner"])
        self.assertEqual(second["repointed"], 0)
        self.assertEqual(sorted(second), sorted(first))
        self.assertEqual(meta_path.read_bytes(), meta_before)
        self.assertEqual(self.record_bytes("b1-a"), record_before)

    def test_takeover_already_owner_finishes_repoint(self):
        meta_path = self.named_meta("cart-rework", "w3:p2")
        self.pane_record("b1-a", "cart-rework", "w3:p1")
        self.set_state(statuses={"w3:p2": "idle"})
        meta_before = meta_path.read_bytes()
        rc, out = self.takeover("cart-rework")
        self.assertEqual(rc, 0, out)
        self.assertTrue(out["already_owner"])
        self.assertEqual(out["repointed"], 1)
        self.assertEqual(self.record("b1-a")["orch_pane"], "w3:p2")
        self.assertEqual(meta_path.read_bytes(), meta_before)

    def test_takeover_already_owner_repairs_claims(self):
        meta_path = self.named_meta("cart-rework", "w3:p2")
        self.claim_file("w3_p1").write_text("cart-rework")
        self.set_state(statuses={"w3:p2": "idle"})
        meta_before = meta_path.read_bytes()
        rc, out = self.takeover("cart-rework")
        self.assertEqual(rc, 0, out)
        self.assertTrue(out["already_owner"])
        self.assertEqual(self.claim_file("w3_p2").read_text(), "cart-rework")
        self.assertFalse(self.claim_file("w3_p1").exists())
        self.assertEqual(meta_path.read_bytes(), meta_before)

    def test_takeover_refuses_unknown_case_variant_and_bad_input(self):
        self.set_state(statuses={"w3:p2": "idle"})
        rc, err = self.takeover("nope")
        self.assertEqual(rc, 2, err)
        self.assertIn("session ls", err["error"])
        meta_path = self.named_meta("Cart-Rework", "w3:p1")
        before = meta_path.read_bytes()
        rc, err = self.takeover("cart-rework")
        self.assertEqual(rc, 2, err)
        self.assertEqual(err["did_you_mean"], "Cart-Rework")
        self.assertEqual(meta_path.read_bytes(), before)
        self.assertEqual(self.takeover("a b")[0], 2)
        no_pane = {k: v for k, v in self.env.items() if k != "HERDR_PANE_ID"}
        rc, err = self.nakama("takeover", "Cart-Rework", env=no_pane)
        self.assertEqual(rc, 2, err)
        self.assertIn("HERDR_PANE_ID", err["error"])

    def test_takeover_refuses_pane_that_owns_another_session(self):
        self.set_state(statuses={"w3:p2": "idle"})
        meta_path = self.named_meta("cart-rework", "w3:p1")
        before = meta_path.read_bytes()
        self.named_meta("mine", "w3:p2", about="my own run")
        rc, err = self.takeover("cart-rework")
        self.assertEqual(rc, 2, err)
        self.assertEqual(err["session"], "mine")
        self.assertIn("my own run", err["error"])
        self.assertEqual(meta_path.read_bytes(), before)

    def test_takeover_cross_kind_and_env_warning(self):
        self.named_meta("cart-rework", "w3:p1",
                        agent={"agent": "claude", "kind": "id", "value": "abc-123"})
        self.set_state(statuses={"w3:p2": "idle"},
                       agent_session={"agent": "pi", "kind": "path", "value": "/t/s.jsonl"})
        rc, out = self.takeover("cart-rework")
        self.assertEqual(rc, 0, out)
        self.assertTrue(out["transcript"]["cross_kind"])
        self.assertEqual(out["transcript"]["resume_with"], "claude --resume abc-123")
        env = {**self.env, "HERDR_PANE_ID": "w3:p2", "NAKAMA_SESSION": "elsewhere"}
        rc, out = self.takeover("cart-rework", env=env)
        self.assertEqual(rc, 0, out)
        self.assertIn("nakama_session_env_points_elsewhere (elsewhere)", out["warnings"])

    def test_session_about_updates_description(self):
        meta_path = self.named_meta("cart-rework", "w3:p1", about="old line")
        before = json.loads(meta_path.read_text())
        env = {**self.env, "NAKAMA_SESSION": "cart-rework"}
        rc, out = self.nakama("session", "about", "new line", env=env)
        self.assertEqual(rc, 0, out)
        self.assertEqual((out["name"], out["about"], out["previous"]),
                         ("cart-rework", "new line", "old line"))
        meta = json.loads(meta_path.read_text())
        self.assertEqual(sorted(meta), sorted(before))
        self.assertEqual(meta["about"], "new line")
        for key in before:
            if key not in ("about", "last_seen"):
                self.assertEqual(meta[key], before[key], key)
        unchanged = meta_path.read_bytes()
        for text in ("", "   ", "one\ntwo", "x" * 201):
            rc, err = self.nakama("session", "about", text, env=env)
            self.assertEqual(rc, 2, err)
            self.assertIn("about", err["error"])
            self.assertEqual(meta_path.read_bytes(), unchanged)
        rc, err = self.nakama("session", "about", "x", env={**self.env, "HERDR_PANE_ID": "w3:p9"})
        self.assertEqual(rc, 2, err)
        self.assertIn("session start", err["error"])
        broken = Path(self.env["NAKAMA_STATE_DIR"]) / "sessions" / "broken"
        broken.mkdir(parents=True)
        (broken / "meta.json").write_text("not json")
        rc, err = self.nakama("session", "about", "x",
                              env={**self.env, "NAKAMA_SESSION": "broken"})
        self.assertEqual(rc, 2, err)
        self.assertIn("unreadable", err["error"])
        self.assertEqual((broken / "meta.json").read_text(), "not json")

    def test_session_about_writes_its_own_directory(self):
        sessions = Path(self.env["NAKAMA_STATE_DIR"]) / "sessions"
        meta_path = sessions / "cart-rework" / "meta.json"
        meta_path.parent.mkdir(parents=True)
        meta_path.write_text(json.dumps({"name": "other", "about": "old", "owner_pane": "w3:p1"}))
        env = {**self.env, "NAKAMA_SESSION": "cart-rework"}
        rc, out = self.nakama("session", "about", "new", env=env)
        self.assertEqual(rc, 0, out)
        self.assertEqual(json.loads(meta_path.read_text())["about"], "new")
        self.assertFalse((sessions / "other").exists())

    def test_launch_survives_unusable_claim_lock(self):
        meta_path = self.named_meta("cart-rework", "w3:p1", agent=None)
        self.set_state(status="idle",
                       agent_session={"agent": "pi", "kind": "path", "value": "/t/s.jsonl"})
        lock = Path(self.env["NAKAMA_STATE_DIR"]) / "sessions" / ".by-pane" / ".lock"
        lock.mkdir(parents=True)
        env = {**self.env, "NAKAMA_SESSION": "cart-rework"}
        rc, out = self.nakama("launch", "builder", "Order Tip", "--kind", "pi", env=env)
        self.assertEqual(rc, 0, out)
        self.assertEqual(out["name"], "b1-order-tip")
        self.assertIsNone(json.loads(meta_path.read_text())["agent"])

    def test_takeover_and_about_report_lock_failure_as_json(self):
        meta_path = self.named_meta("cart-rework", "w3:p1")
        self.set_state(statuses={"w3:p2": "idle"})
        before = meta_path.read_bytes()
        lock = Path(self.env["NAKAMA_STATE_DIR"]) / "sessions" / ".by-pane" / ".lock"
        lock.mkdir(parents=True)
        rc, err = self.takeover("cart-rework")
        self.assertEqual(rc, 1, err)
        self.assertTrue(err["error"].startswith("takeover failed"), err)
        self.assertEqual(meta_path.read_bytes(), before)
        env = {**self.env, "NAKAMA_SESSION": "cart-rework"}
        rc, err = self.nakama("session", "about", "x", env=env)
        self.assertEqual(rc, 1, err)
        self.assertTrue(err["error"].startswith("session about failed"), err)
        self.assertEqual(meta_path.read_bytes(), before)

    def test_takeover_help_listed(self):
        self.assertIn("adopt a named session", self.help_text())
        self.assertIn("replace this session", self.help_text("session"))

    def test_session_rm_previews_without_yes(self):
        state = Path(self.env["NAKAMA_STATE_DIR"])
        self.named_meta("old-run", "w9:p9")
        self.pane_record("b1-a", "old-run", "w9:p9", closed=True)
        self.orch_mail("old-run")
        rc, out = self.nakama("session", "rm", "old-run")
        self.assertEqual(rc, 0, out)
        self.assertEqual(out["mode"], "delete")
        self.assertEqual(out["inbox"]["unread"], 1)
        self.assertEqual(out["records"]["dropped"], 1)
        self.assertTrue(out["confirm"])
        self.assertTrue((state / "sessions" / "old-run").is_dir())
        self.assertTrue((state / "b1-a.json").is_file())
        self.assertEqual(len(list(self.orch_box("old-run").glob("*.md"))), 1)

    def test_session_rm_deletes_unreferenced_session(self):
        state = Path(self.env["NAKAMA_STATE_DIR"])
        self.named_meta("old-run", "w9:p9")
        self.pane_record("b1-a", "old-run", "w9:p9", closed=True)
        self.pane_record("b1-z", "other", "w9:p8", closed=True)
        self.orch_mail("old-run")
        (self.orch_box("old-run") / f"{time.time_ns()}.b1-x.info.read").write_text("seen")
        claims = state / "sessions" / ".by-pane"
        claims.mkdir(parents=True)
        (claims / "w9_p9").write_text("old-run")
        rc, out = self.nakama("session", "rm", "old-run", "--yes")
        self.assertEqual(rc, 0, out)
        self.assertEqual(out["mode"], "delete")
        self.assertFalse((state / "sessions" / "old-run").exists())
        self.assertFalse(self.orch_box("old-run").exists())
        self.assertFalse((state / "b1-a.json").exists())
        self.assertFalse((claims / "w9_p9").exists())
        self.assertTrue((state / "b1-z.json").is_file())
        self.assertEqual(out["removed"]["records"], 1)
        self.assertEqual(out["removed"]["claims"], 1)
        self.assertEqual(out["removed"]["inbox"], {"unread": 1, "read": 1})

    def test_session_rm_refuses_open_and_paused_workers(self):
        state = Path(self.env["NAKAMA_STATE_DIR"])
        self.named_meta("old-run", "w9:p9")
        self.pane_record("b1-a", "old-run", "w9:p9")
        rc, out = self.nakama("session", "rm", "old-run")
        self.assertEqual(rc, 2, out)
        self.assertEqual(out["workers"][0]["state"], "open")
        self.assertEqual(out["workers"][0]["command"], "nakama teardown b1-a")
        rc, out = self.nakama("session", "rm", "old-run", "--yes")
        self.assertEqual(rc, 2, out)
        self.assertTrue((state / "sessions" / "old-run").is_dir())
        self.pane_record("b1-a", "old-run", "w9:p9", closed=True)
        self.pane_record("b1-p", "old-run", "w9:p9", closed=True, paused=True)
        rc, out = self.nakama("session", "rm", "old-run")
        self.assertEqual(rc, 2, out)
        self.assertEqual(out["workers"][0]["state"], "paused")
        self.assertEqual(out["workers"][0]["command"], "nakama resume b1-p")
        self.pane_record("b1-p", "old-run", "w9:p9", closed=True, paused=True,
                         worktree=str(Path(self.root, "gone")))
        rc, out = self.nakama("session", "rm", "old-run")
        self.assertEqual(rc, 0, out)
        self.assertEqual(out["mode"], "delete")
        self.pane_record("b1-a", "old-run", "w9:p9", paused=True)
        rc, out = self.nakama("session", "rm", "old-run")
        self.assertEqual(rc, 2, out)
        self.assertEqual(out["workers"][0]["state"], "open")

    def test_session_rm_archives_when_worktree_remains(self):
        state = Path(self.env["NAKAMA_STATE_DIR"])
        self.named_meta("old-run", "w9:p9")
        wt = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, wt, True)
        self.pane_record("b1-k", "old-run", "w9:p9", closed=True, branch="agent/b1-k",
                         worktree=str(wt))
        self.pane_record("r1-d", "old-run", "w9:p9", closed=True)
        self.orch_mail("old-run")
        kept_before = self.record_bytes("b1-k")
        rc, out = self.nakama("session", "rm", "old-run", "--yes")
        self.assertEqual(rc, 0, out)
        self.assertEqual(out["mode"], "archive")
        archive = Path(out["archive"])
        self.assertEqual(archive.parent, state / "sessions" / ".archive")
        self.assertEqual(json.loads((archive / "meta.json").read_text())["about"], "a run")
        self.assertEqual(len(list((archive / "inbox").glob("*.md"))), 1)
        self.assertFalse((state / "sessions" / "old-run").exists())
        self.assertEqual(self.record_bytes("b1-k"), kept_before)
        self.assertFalse((state / "r1-d.json").exists())
        self.assertTrue(wt.is_dir())
        self.assertIn("kept_records_keep_session_name", out["warnings"])
        rc, ls = self.session_ls()
        self.assertEqual(rc, 0, ls)
        self.assertNotIn("old-run", [row["name"] for row in ls["sessions"]])

    def test_session_rm_resolves_names_exactly(self):
        self.named_meta("Old-Run", "w9:p9")
        rc, out = self.nakama("session", "rm", "default")
        self.assertEqual(rc, 2, out)
        self.assertIn("--legacy", out["error"])
        rc, out = self.nakama("session", "rm", "old-run")
        self.assertEqual(rc, 2, out)
        self.assertEqual(out["did_you_mean"], "Old-Run")
        rc, out = self.nakama("session", "rm", "nope")
        self.assertEqual(rc, 2, out)
        self.assertIn("session ls", out["error"])

    def test_session_rm_reserves_default_name(self):
        state = Path(self.env["NAKAMA_STATE_DIR"])
        self.named_meta("default", "w9:p9")
        rc, out = self.nakama("session", "rm", "default", "--yes")
        self.assertEqual(rc, 2, out)
        self.assertIn("--legacy", out["error"])
        self.assertTrue((state / "sessions" / "default").is_dir())

    def test_session_rm_ignores_unsafe_record_names(self):
        state = Path(self.env["NAKAMA_STATE_DIR"])
        state.mkdir(parents=True, exist_ok=True)
        (state / "evil.json").write_text(json.dumps(
            {"name": "../victim", "session": "old-run", "closed": True,
             "worktree": str(self.root)}))
        victim = state.parent / "victim.json"
        victim.write_text("keep")
        self.named_meta("old-run", "w9:p9")
        rc, out = self.nakama("session", "rm", "old-run", "--yes")
        self.assertEqual(rc, 0, out)
        self.assertTrue((state / "evil.json").is_file())
        self.assertTrue(victim.is_file())

    def test_session_rm_reports_unreadable_claim_dir_entry(self):
        state = Path(self.env["NAKAMA_STATE_DIR"])
        self.named_meta("old-run", "w9:p9")
        (state / "sessions" / ".by-pane" / "w9_p9").mkdir(parents=True)
        rc, out = self.nakama("session", "rm", "old-run")
        self.assertEqual(rc, 1, out)
        self.assertTrue(out["error"].startswith("session rm failed"), out)

    def test_session_rm_archive_rerun_keeps_both_inboxes(self):
        state = Path(self.env["NAKAMA_STATE_DIR"])
        self.named_meta("old-run", "w9:p9")
        wt = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, wt, True)
        self.pane_record("b1-k", "old-run", "w9:p9", closed=True, branch="agent/b1-k",
                         worktree=str(wt))
        crashed = state / "sessions" / "old-run" / "inbox"
        crashed.mkdir(parents=True)
        (crashed / "1.b1-k.info.md").write_text("old")
        self.orch_mail("old-run")
        rc, out = self.nakama("session", "rm", "old-run", "--yes")
        self.assertEqual(rc, 0, out)
        self.assertEqual(out["mode"], "archive")
        archive = Path(out["archive"])
        self.assertEqual((archive / "inbox" / "1.b1-k.info.md").read_text(), "old")
        self.assertEqual(len(list((archive / "inbox-2").glob("*.md"))), 1)
        self.assertFalse(self.orch_box("old-run").exists())

    def test_session_rm_clears_unreadable_meta(self):
        broken = Path(self.env["NAKAMA_STATE_DIR"]) / "sessions" / "bad"
        broken.mkdir(parents=True)
        (broken / "meta.json").write_text("{")
        rc, out = self.nakama("session", "rm", "bad", "--yes")
        self.assertEqual(rc, 0, out)
        self.assertIsNone(out["about"])
        self.assertFalse(broken.exists())

    def test_session_rm_warns_when_env_names_it(self):
        state = Path(self.env["NAKAMA_STATE_DIR"])
        meta_path = self.named_meta("old-run", "w9:p9")
        env = {**self.env, "NAKAMA_SESSION": "old-run"}
        rc, out = self.nakama("session", "rm", "old-run", env=env)
        self.assertEqual(rc, 0, out)
        self.assertTrue(any(w.startswith("nakama_session_env_points_here")
                            for w in out["warnings"]))
        self.assertEqual(out["claims"], 0)
        self.assertFalse((state / "sessions" / ".by-pane").exists())
        (meta_path.parent / "notes.md").write_text("handoff\n")
        self.orch_mail("old-run")
        rc, out = self.nakama("session", "rm", "old-run", env=env)
        self.assertEqual(rc, 0, out)
        self.assertEqual(out["notes"], ["notes.md"])
        self.assertTrue(any(w.startswith("notes_will_be_deleted") for w in out["warnings"]))
        self.assertTrue(any(w.startswith("unread_mail_will_be_deleted")
                            for w in out["warnings"]))
        self.assertFalse((state / "sessions" / ".by-pane").exists())
        self.assertEqual(out["claims"], 0)

    def test_session_rm_then_start_reuses_the_name(self):
        state = Path(self.env["NAKAMA_STATE_DIR"])
        self.set_state(status="idle")
        rc, out = self.start_session("cart-rework")
        self.assertEqual(rc, 0, out)
        claim = state / "sessions" / ".by-pane" / "w3_p1"
        self.assertEqual(claim.read_text(), "cart-rework")
        rc, out = self.nakama("session", "rm", "cart-rework", "--yes")
        self.assertEqual(rc, 0, out)
        self.assertFalse(claim.exists())
        rc, out = self.start_session("cart-rework")
        self.assertEqual(rc, 0, out)
        rc, out = self.start_session("default", env={**self.env, "HERDR_PANE_ID": "w3:p2"})
        self.assertEqual(rc, 2, out)
        self.assertIn("reserved", out["error"])
        self.assertFalse((state / "sessions" / "default").exists())

    def test_session_rm_help_listed(self):
        self.assertIn("remove a finished named session", self.help_text("session"))


class NakamaHcTest(unittest.TestCase):
    """Herdr app terminals: the same commands run on herdr-core (hc) when HC_TERMINAL_ID is set."""

    # Borrowed helpers only: subclassing NakamaTest would re-run its herdr tests in this environment.
    set_state = NakamaTest.set_state
    nakama = NakamaTest.nakama
    calls = NakamaTest.calls
    launch = NakamaTest.launch
    task = NakamaTest.task
    doctor_env = NakamaTest.doctor_env

    def setUp(self):
        NakamaTest.setUp(self)
        self.hc_log = self.root / "hc.jsonl"
        self.hc_log.write_text("")
        for name in ("HERDR_ENV", "HERDR_PANE_ID", "HERDR_WORKSPACE_ID"):
            self.env.pop(name, None)
        self.env.update({"HC_TERMINAL_ID": "t1", "FAKE_HC_LOG": str(self.hc_log),
                         "NAKAMA_HC_STATUS_GRACE_SEC": "0"})  # no real 20 s wait in tests

    def set_hc(self, **hc):
        st = json.loads(self.state.read_text())
        st["hc"] = {**st.get("hc", {}), **hc}
        self.set_state(**st)

    def hc_calls(self):
        return [entry["argv"] for entry in map(json.loads, self.hc_log.read_text().splitlines())]

    def hc_entries(self):
        return [json.loads(line) for line in self.hc_log.read_text().splitlines()]

    def record(self, name):
        return json.loads((Path(self.env["NAKAMA_STATE_DIR"]) / f"{name}.json").read_text())

    def test_launch_starts_worker_in_caller_space_and_waits_for_ready(self):
        out = self.launch()
        run = next(c for c in self.hc_calls() if c[1] == "run")
        prefix = ["hc", "run", "--bg", "--json", "--no-notify", "--outlive-parent", "--name", out["name"],
                  "--cwd", out["worktree"], "--label", "herdr.space=s7", "--", "env", f"NAKAMA_AGENT={out['name']}"]
        self.assertEqual(run[:len(prefix)], prefix)
        self.assertTrue(any(item.startswith("GH_CONFIG_DIR=") for item in run))
        self.assertIn("pi", run[run.index("--") + 1:])
        self.assertIn(["hc", "wait", out["name"], "--timeout", "60", "--json"], self.hc_calls())
        self.assertEqual(self.record(out["name"])["pane"], "t9")
        self.assertIsNone(self.record(out["name"])["tab"])
        self.assertEqual(out["pane"], "t9")

    def test_launch_split_layout_exits_2_before_creating_a_worktree(self):
        rc, out = self.nakama("launch", "builder", "Order Tip", "--kind", "pi", "--layout", "split")
        self.assertEqual(rc, 2)
        self.assertIn("split is not supported", out["error"])
        self.assertFalse(any(c[1] == "run" for c in self.hc_calls()))
        self.assertFalse(any(c[0] == "wt" for c in self.calls()))

    def test_launch_reports_blocked_when_readiness_is_not_idle(self):
        self.set_hc(agent_state="blocked")
        out = self.launch()
        self.assertEqual(out["status"], "blocked")
        self.assertIn("blocked", out["stderr"])
        self.assertNotIn("warnings", out)  # a blocked dialog still reported a status

    def test_launch_warns_when_the_terminal_never_reports_a_status(self):
        self.set_hc(agent_state=None)
        out = self.launch()
        self.assertIn("reports no status to the Herdr app (no OSC 7501)", out["warnings"][0])
        self.assertIn("nakama teardown + launch/resume", out["warnings"][0])

    def test_launch_has_no_warning_when_the_terminal_reports_a_status(self):
        self.assertNotIn("warnings", self.launch())

    def test_wait_maps_hc_states_to_nakama_states(self):
        out = self.launch()
        expected = {"idle": "idle", "done": "done", "working": "working", "blocked": "blocked",
                    "error": "blocked", None: None}
        for hc_state, nakama_state in expected.items():
            self.set_hc(agent_state=hc_state)
            listed = self.nakama("list")[1]["agents"]
            self.assertEqual(listed[0]["state"], nakama_state, hc_state)
        self.set_hc(agent_state="idle", terminal_state="finished")
        self.assertIsNone(self.nakama("list")[1]["agents"][0]["state"])
        self.set_hc(terminal_state="live", missing=[out["name"]])
        self.assertIsNone(self.nakama("list")[1]["agents"][0]["state"])

    def test_prompt_sends_text_with_hc_terminal_id_removed(self):
        out = self.launch()
        self.task(out)
        rc, sent = self.nakama("prompt", out["name"], "task.md")
        self.assertEqual(rc, 0, sent)
        send = next(e for e in self.hc_entries() if e["argv"][1] == "send")
        self.assertEqual(send["argv"][2], out["name"])
        self.assertTrue(send["argv"][3].startswith("Read "))
        self.assertFalse(send["terminal_env"])

    def test_teardown_interrupts_kills_and_removes_the_terminal(self):
        out = self.launch()
        self.assertEqual(self.nakama("teardown", out["name"])[0], 0)
        calls = self.hc_calls()
        order = [c[:2] for c in calls if c[1] in ("keys", "kill", "rm")]
        self.assertEqual(order, [["hc", "keys"], ["hc", "kill"], ["hc", "rm"]])
        self.assertIn(["hc", "keys", out["name"], "ctrl+c", "ctrl+c"], calls)
        self.assertIn(["hc", "kill", "t9"], calls)
        self.assertIn(["hc", "rm", "t9"], calls)

    def test_session_start_and_takeover_refuse_on_hc(self):
        rc, out = self.nakama("session", "start", "cart-rework", "--about", "x")
        self.assertEqual(rc, 2)
        self.assertIn("named sessions need the herdr TUI", out["error"])
        rc, out = self.nakama("takeover", "cart-rework")
        self.assertEqual(rc, 2)
        self.assertIn("named sessions need the herdr TUI", out["error"])

    def test_doctor_passes_with_hc_and_no_herdr_on_path(self):
        env = self.doctor_env()
        (self.bin_dir / "herdr").unlink()
        env = {k: v for k, v in env.items() if k not in ("HERDR_ENV", "HERDR_PANE_ID", "HERDR_WORKSPACE_ID")}
        env.update({"HC_TERMINAL_ID": "t1", "PATH": f"{self.bin_dir}:/usr/bin:/bin"})
        rc, out = self.nakama("doctor", env=env)
        self.assertEqual(rc, 0, out)
        self.assertTrue(out["ok"], out)
        checks = {c["check"]: c["ok"] for c in out["checks"]}
        self.assertTrue(checks["hc >= 0.1.19"])
        self.assertNotIn("herdr claude integration", checks)

    def test_herdr_env_wins_over_hc_terminal_id(self):
        out = self.launch_with_env({**self.env, "HERDR_ENV": "1", "HERDR_PANE_ID": "w3:p1"})
        self.assertTrue(any(c[1:3] == ["tab", "create"] for c in self.calls()))
        self.assertEqual(self.hc_calls(), [])
        self.assertEqual(out["pane"], "w1:p8")

    def launch_with_env(self, env):
        rc, out = self.nakama("launch", "builder", "Order Tip", "--kind", "pi", env=env)
        self.assertEqual(rc, 0, out)
        return out


class NakamaSpaceTest(unittest.TestCase):
    """nakama space: one Herdr app space per ticket, an orchestrator in that ticket's worktree."""

    # Borrowed helpers only, as NakamaHcTest does: subclassing would re-run NakamaTest's herdr tests.
    set_state = NakamaTest.set_state
    nakama = NakamaTest.nakama
    calls = NakamaTest.calls

    def setUp(self):
        NakamaTest.setUp(self)
        self.hc_log = self.root / "hc.jsonl"
        self.hc_log.write_text("")
        for name in ("HERDR_ENV", "HERDR_PANE_ID", "HERDR_WORKSPACE_ID"):
            self.env.pop(name, None)
        self.env.update({"HC_TERMINAL_ID": "t1", "FAKE_HC_LOG": str(self.hc_log)})
        git = self.bin_dir / "git"
        git.write_text(FAKE_GIT)
        git.chmod(0o755)
        self.set_refs("refs/heads/develop")

    def set_refs(self, *refs):
        self.state_update(refs=list(refs))

    def set_spaces(self, *spaces):
        self.state_update(herdr_app={"spaces": list(spaces)})

    def set_worktrees(self, *items):
        self.state_update(worktrees=list(items))

    def set_app(self, **values):
        self.state_update(herdr_app={"spaces": [], **values})

    def state_update(self, **values):
        self.set_state(**{**json.loads(self.state.read_text()), **values})

    def space(self, *args, env=None):
        return self.nakama("space", *args, env=env)

    def app_calls(self):
        return [c for c in self.calls() if c[0] == "herdr-app"]

    def opened_space(self):
        """The payloads this run sent to space.new."""
        return [json.loads(c[3]) for c in self.app_calls() if c[1:3] == ["do", "space.new"]]

    def selected(self):
        return [json.loads(c[3])["id"] for c in self.app_calls() if c[1:3] == ["do", "space.select"]]

    def wt_switch(self):
        return [c for c in self.calls() if c[:2] == ["wt", "switch"]]

    def wt_steps(self):
        return [c for c in self.calls() if c[:2] == ["wt", "step"]]

    def test_existing_space_is_focused_without_a_worktree_or_git(self):
        self.set_spaces({"id": "sp1", "name": "mban-42", "folder": "/elsewhere", "machine": "local",
                         "active": False, "tabs": []})
        self.set_refs("refs/heads/feature/MBAN-42-cart")
        rc, out = self.space("MBAN-42")
        self.assertEqual(rc, 0, out)
        self.assertEqual(out, {"key": "MBAN-42", "space": "sp1", "branch": None, "worktree": None,
                               "created": False, "worktree_created": False, "status": "focused"})
        self.assertEqual(self.selected(), ["sp1"])
        self.assertEqual(self.opened_space(), [])
        self.assertEqual(self.wt_switch(), [])
        self.assertEqual([c for c in self.calls() if c[0] == "git"], [])

    def test_matching_local_branch_switches_and_opens_the_space(self):
        self.set_refs("refs/heads/feature/MBAN-42-cart", "refs/heads/develop")
        rc, out = self.space("MBAN-42")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.wt_switch(), [["wt", "switch", "feature/MBAN-42-cart", "--no-cd", "--yes",
                                             "--format", "json"]])
        self.assertEqual(self.wt_steps(), [["wt", "step", "copy-ignored", "--require-include"]])
        worktree = str(self.root / "wt-feature-MBAN-42-cart")
        self.assertEqual(self.opened_space(), [{"name": "MBAN-42", "folder": worktree,
                                                "tab": "orchestrator", "argv": ["claude"]}])
        self.assertEqual(out, {"key": "MBAN-42", "space": "sp-new", "branch": "feature/MBAN-42-cart",
                               "worktree": worktree, "created": True, "worktree_created": True,
                               "status": "opened"})

    def test_remote_only_branch_switches_by_its_stripped_name(self):
        self.set_refs("refs/remotes/origin/feature/MBAN-42-cart")
        rc, out = self.space("MBAN-42")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.wt_switch()[0][:3], ["wt", "switch", "feature/MBAN-42-cart"])
        self.assertEqual(out["branch"], "feature/MBAN-42-cart")

    def test_branch_already_checked_out_is_reused(self):
        self.set_refs("refs/heads/feature/MBAN-42-cart")
        self.set_worktrees({"branch": "feature/MBAN-42-cart", "path": "/main/checkout"})
        rc, out = self.space("MBAN-42")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.wt_switch(), [])
        self.assertEqual(self.wt_steps(), [])
        self.assertEqual(self.opened_space()[0]["folder"], "/main/checkout")
        self.assertEqual((out["worktree"], out["worktree_created"], out["created"]),
                         ("/main/checkout", False, True))

    def test_new_branch_from_nakama_base(self):
        env = {**self.env, "NAKAMA_BASE": "develop"}
        rc, out = self.space("MBAN-42", "--slug", "cart", env=env)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.wt_switch(), [["wt", "switch", "-c", "feature/MBAN-42-cart", "--base", "develop",
                                             "--no-cd", "--yes", "--format", "json"]])
        self.assertEqual(out["branch"], "feature/MBAN-42-cart")

    def test_new_branch_without_base_omits_the_flag(self):
        env = {k: v for k, v in self.env.items() if k != "NAKAMA_BASE"}
        rc, out = self.space("MBAN-42", "--slug", "cart", env=env)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.wt_switch(), [["wt", "switch", "-c", "feature/MBAN-42-cart",
                                             "--no-cd", "--yes", "--format", "json"]])

    def test_base_flag_beats_nakama_base(self):
        env = {**self.env, "NAKAMA_BASE": "develop"}
        rc, out = self.space("MBAN-42", "--slug", "cart", "--base", "main", env=env)
        self.assertEqual(rc, 0, out)
        switch = self.wt_switch()[0]
        self.assertEqual(switch[switch.index("--base") + 1], "main")
        self.assertNotIn("develop", switch)

    def test_no_match_without_slug_exits_2(self):
        rc, out = self.space("MBAN-42")
        self.assertEqual(rc, 2)
        self.assertIn("--slug", out["error"])
        self.assertEqual((self.wt_switch(), self.opened_space(), self.selected()), ([], [], []))

    def test_two_matches_exit_2_and_branch_resolves(self):
        self.set_refs("refs/heads/feature/MBAN-42-cart", "refs/heads/feature/MBAN-42-checkout")
        rc, out = self.space("MBAN-42")
        self.assertEqual(rc, 2)
        self.assertEqual(out["branches"], ["feature/MBAN-42-cart", "feature/MBAN-42-checkout"])
        self.assertEqual(self.opened_space(), [])
        rc, out = self.space("MBAN-42", "--branch", "feature/MBAN-42-checkout")
        self.assertEqual(rc, 0, out)
        self.assertEqual(out["branch"], "feature/MBAN-42-checkout")

    def test_branch_flag_must_exist(self):
        rc, out = self.space("MBAN-42", "--branch", "feature/MBAN-42-nowhere")
        self.assertEqual(rc, 2)
        self.assertEqual(self.opened_space(), [])

    def test_token_boundary_keeps_mban_12_off_mban_123(self):
        self.set_refs("refs/heads/feature/MBAN-123-x")
        rc, out = self.space("MBAN-12")
        self.assertEqual(rc, 2)
        self.assertIn("--slug", out["error"])
        self.assertEqual(self.opened_space(), [])

    def test_agent_and_remote_head_refs_are_excluded(self):
        self.set_refs("refs/heads/agent/MBAN-42-worker", "refs/remotes/origin/HEAD")
        rc, out = self.space("MBAN-42")
        self.assertEqual(rc, 2)
        self.assertIn("--slug", out["error"])

    def test_slug_with_a_matching_branch_warns_and_is_ignored(self):
        self.set_refs("refs/heads/feature/MBAN-42-cart")
        rc, out = self.space("MBAN-42", "--slug", "other")
        self.assertEqual(rc, 0, out)
        self.assertEqual(out["branch"], "feature/MBAN-42-cart")
        self.assertIn("--slug", out["warnings"][0])
        self.assertNotIn("-c", self.wt_switch()[0])

    def test_herdr_tui_backend_exits_2(self):
        env = {k: v for k, v in self.env.items() if k != "HC_TERMINAL_ID"}
        rc, out = self.space("MBAN-42", env={**env, "HERDR_ENV": "1"})
        self.assertEqual(rc, 2)
        self.assertIn("Herdr app", out["error"])
        self.assertEqual(self.app_calls(), [])

    def test_output_has_exactly_the_documented_keys(self):
        self.set_refs("refs/heads/feature/MBAN-42-cart")
        rc, out = self.space("MBAN-42")
        self.assertEqual(rc, 0, out)
        self.assertEqual(set(out), {"key", "space", "branch", "worktree", "created", "worktree_created",
                                    "status"})

    def test_kind_and_model_build_the_orchestrator_argv(self):
        self.set_refs("refs/heads/feature/MBAN-42-cart")
        rc, out = self.space("MBAN-42", "--kind", "pi", "--model", "anthropic/claude-x")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.opened_space()[0]["argv"], ["pi", "--model", "anthropic/claude-x"])

    def test_a_second_run_focuses_the_space_the_first_opened(self):
        self.set_refs("refs/heads/feature/MBAN-42-cart")
        self.assertEqual(self.space("MBAN-42")[1]["status"], "opened")
        rc, out = self.space("MBAN-42")
        self.assertEqual(rc, 0, out)
        self.assertEqual((out["status"], out["space"], out["branch"], out["worktree"], out["created"]),
                         ("focused", "sp-new", None, None, False))
        self.assertEqual(len(self.opened_space()), 1)
        self.assertEqual(len(self.wt_switch()), 1)

    def test_herdr_app_failure_exits_1_with_its_stderr(self):
        self.set_refs("refs/heads/feature/MBAN-42-cart")
        self.set_app(rc=1, stderr="no app session")
        rc, out = self.space("MBAN-42")
        self.assertEqual(rc, 1)
        self.assertIn("no app session", out["stderr"])


if __name__ == "__main__":
    unittest.main()
