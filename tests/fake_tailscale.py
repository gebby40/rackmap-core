"""A stand-in `tailscale` command for tests: put its directory first on PATH and steer it through a JSON file."""

import json
import os
import stat
import sys
from pathlib import Path

SCRIPT = r"""#!PYTHON
import json, sys
from pathlib import Path
path = Path(__file__).with_name("state.json")
state = json.loads(path.read_text())
args = sys.argv[1:]
def denied():
    print("Access denied: serve config denied\n\nUse 'sudo tailscale serve' or 'tailscale up --operator=$USER' to not require root.", file=sys.stderr)
    sys.exit(1)
if args[:2] == ["status", "--json"]:
    if state["backend"] == "down":
        print("failed to connect to local tailscaled; it doesn't appear to be running", file=sys.stderr); sys.exit(1)
    print("# Health check:\n" if state.get("noise") else "", end="")
    print(json.dumps({"BackendState": state["backend"], "Self": {"DNSName": "box.tail1234.ts.net.", "TailscaleIPs": ["100.64.0.7", "fd7a::7"]},
                      "CurrentTailnet": {"Name": "example.org"}, "CertDomains": ["box.tail1234.ts.net"] if state["https"] else None}))
elif args[:3] == ["serve", "status", "--json"]:
    if state.get("status_denied"): denied()
    print(json.dumps(state["serve"]))
elif args[:2] == ["serve", "--bg"]:
    if not state["operator"]: denied()
    if not state["https"]:
        print("Serve is not enabled on your tailnet.\nTo enable, visit:\n\n  https://login.tailscale.com/f/serve?node=abc", file=sys.stderr); sys.exit(1)
    state["serve"] = {"TCP": {"443": {"HTTPS": True}}, "Web": {"box.tail1234.ts.net:443": {"Handlers": {"/": {"Proxy": args[3]}}}}}
    state["calls"].append(args); path.write_text(json.dumps(state))
elif args[:1] == ["serve"] and args[-1] == "off":
    if not state["operator"]: denied()
    state["serve"] = {}; state["calls"].append(args); path.write_text(json.dumps(state))
else:
    sys.exit(2)
"""


class FakeTailscale:
    def __init__(self, directory):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        command = self.dir / "tailscale"
        command.write_text(SCRIPT.replace("PYTHON", sys.executable, 1))
        command.chmod(command.stat().st_mode | stat.S_IEXEC)
        self.set(backend="Running", https=True, operator=True, serve={}, calls=[])
        self._old_path = os.environ["PATH"]
        os.environ["PATH"] = f"{self.dir}{os.pathsep}{self._old_path}"

    @property
    def state(self):
        return json.loads((self.dir / "state.json").read_text())

    def set(self, **changes):
        current = self.state if (self.dir / "state.json").exists() else {}
        current.update(changes)
        (self.dir / "state.json").write_text(json.dumps(current))

    def remove(self):
        os.environ["PATH"] = self._old_path
