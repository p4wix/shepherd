#!/usr/bin/env python3
"""Claude Code PreToolUse hook: production Salesforce orgs are read-only for agents.

Reads the PreToolUse JSON (tool_name, tool_input, cwd) from stdin and prints a
"deny" decision, or nothing to let the call through. It always exits 0.

Design: an ALLOWLIST on production. A Bash command is production-touching when
any sf/sfdx invocation in it resolves to a production org, when its text names a
production alias, username or instance host, or when it picks the org in a way
the hook cannot resolve (--flags-dir, SF_TARGET_ORG and friends, a variable as
the target, a different HOME). On production only a short list of read-only
commands passes; everything else, and anything the hook cannot parse, is denied.
Salesforce MCP tools get the same treatment (only clearly read-only tool names
pass on production), and browser tools may not open a Salesforce host that is
not provably a sandbox.

Fail closed: an org that cannot be identified, or whose auth file does not prove
it is a sandbox or scratch org, is production.

Security: sf auth files (~/.sfdx/<username>.json) hold access and refresh
tokens. Only their "instanceUrl" value is used, and nothing from them is ever
printed. Deny messages never echo untrusted input: they name an org only when it
is a known alias or username from the alias file. The hook never runs `sf`.
"""

import json
import os
import re
import shlex
import sys
from urllib.parse import unquote, urlparse

REASON_TAIL = (
    "Production is read-only for agents. Do not retry, and do not work around this "
    "block (no other flag, alias, variable, wrapper, script or tool). Write the exact "
    "command out for the user to run themselves."
)
UNVERIFIED = "an org the guard could not verify as a sandbox"

# ---------------------------------------------------------------- sf allowlist

# Exact command word sequences that only read from the org.
SF_READ_ONLY = {
    ("data", "query"), ("data", "query", "resume"),
    ("data", "export", "tree"), ("data", "export", "bulk"), ("data", "export", "resume"),
    ("project", "retrieve", "start"), ("project", "retrieve", "preview"),
    ("project", "deploy", "validate"), ("project", "deploy", "report"),
    ("project", "deploy", "preview"),
    ("org", "list"), ("org", "list", "metadata"), ("org", "list", "metadata-types"),
    ("org", "list", "limits"), ("data", "get", "record"),
    ("sobject", "describe"), ("sobject", "list"),
    ("apex", "get", "log"), ("apex", "list", "log"),
}
# Topics that never talk to an org.
SF_NO_ORG_TOPICS = {"plugins", "autocomplete", "help", "version", "which", "commands",
                    "doctor", "update", "search", "info", "whatsnew"}
# Bare words that start an sf command when the binary itself is hidden, e.g. `$(which sf) project ...`.
SF_TOPIC_HEADS = {"project", "data", "apex", "org", "package", "package1", "sobject", "api",
                  "community", "deploy", "retrieve", "force", "agent", "cmdt", "limits",
                  "lightning", "logic", "flow", "visualforce", "static-resource", "user",
                  "config", "schema"}
READ_METHODS = {"GET", "HEAD", "OPTIONS"}
# Words a deny message may echo; anything else in the command is untrusted text.
SF_KNOWN_WORDS = ({w for cmd in SF_READ_ONLY for w in cmd} | SF_TOPIC_HEADS | SF_NO_ORG_TOPICS |
                  {"deploy", "start", "quick", "resume", "cancel", "report", "create", "update",
                   "delete", "upsert", "import", "record", "bulk", "tree", "run", "test", "tail",
                   "log", "open", "login", "logout", "web", "jwt", "install", "uninstall",
                   "assign", "permset", "permsetlicense", "password", "generate", "sandbox",
                   "scratch", "source", "mdapi", "rest", "graphql", "request", "execute", "push",
                   "pull", "set", "unset", "get", "publish", "refresh", "tracking", "reset",
                   "convert", "manifest", "data", "file", "results", "version", "promote"})

ORG_FLAGS = {"--target-org", "-o", "-u", "--targetusername",
             "--target-dev-hub", "--targetdevhubusername"}
WRAPPERS = {"env", "sudo", "command", "exec", "time", "nohup", "npx", "xargs", "nice",
            "caffeinate", "builtin", "timeout", "noglob", "bunx", "watch", "stdbuf"}
HTTP_TOOLS = {"curl", "wget", "http", "https", "xh", "httpie", "curlie"}
SHELLS = {"bash", "sh", "zsh", "dash", "ksh", "fish", "csh", "tcsh", "pwsh", "powershell",
          "powershell.exe", "pwsh.exe", "cmd", "cmd.exe"}
# Interpreters that can start processes or open connections from inline code.
INTERPRETER_RE = re.compile(r"^(?:python[\d.]*|pypy[\d.]*|node|nodejs|deno|bun|perl[\d.]*|"
                            r"ruby[\d.]*|php[\d.]*|lua[\d.]*|luajit|osascript|rscript|julia|"
                            r"tclsh|groovy|awk|gawk|mawk|nawk|irb|jshell|swift)$", re.I)
INLINE_CODE_FLAGS = {"-c", "-e", "-p", "-r", "-E", "--eval", "--print", "--command", "-Command",
                     "-command"}
# Inline code that can run another program or reach the network.
SPAWN_RE = re.compile(
    r"subprocess|child_process|os\.system|\bsystem\s*\(|popen|spawn|\bexec|execSync|"
    r"execFile|Deno\.(?:run|Command)|shell_exec|passthru|proc_open|`|\bimport\s+os\b|"
    r"__import__|\bos\.|urllib|requests|http|fetch\s*\(|socket|curl|\beval\b|pty\b|"
    r"Process\.start|Runtime\.getRuntime|do\s+shell\s+script", re.I)
# Decoders whose output is code the hook cannot read.
DECODER_RE = re.compile(
    r"\bbase64\b|\bxxd\b|\bopenssl\b|\buudecode\b|\bgunzip\b|\bzcat\b|\bgzip\s+-d|"
    r"\bFromBase64String\b|-EncodedCommand\b|\bbase32\b|\bbasenc\b|\brev\b(?!-)|"
    r"\b(?:pwsh|powershell)(?:\.exe)?\b[^\n;|&]*\s-(?:e|ec|en|enc\w*)(?![\w-])", re.I)
PS_ENCODED_RE = re.compile(r"(?<![\w-])-(?:e|ec|en|enc|enco|encod|encodedcommand)(?![\w-])", re.I)

# vibe-force scripts (vibe-force/scripts/...), which run sf themselves.
VF_CHECK_SAFE = {"format", "lint", "analyzer", "pairing", "jest", "static", "local",
                 "deploy-validate"}
VF_CHECK_STRING_OPTS = {"--files", "--target-org", "-o", "--env", "--report", "--project-dir",
                        "--base-ref", "--tests", "--class-names", "--suite-names",
                        "--test-level", "--job-id", "--manifest"}
VF_SETUP_SAFE = {"check", "audit", "list", "clean"}
VF_SETUP_STRING_OPTS = {"--target-org", "-o", "--path", "--ttl", "--port", "--since",
                        "--limit", "--section", "--project-dir"}
NODE_RUNNERS = {"node", "nodejs", "bun"}
VF_KNOWN = VF_CHECK_SAFE | VF_SETUP_SAFE | {"deploy-quick", "smoke", "verify", "apex", "all",
                                            "serve"}

SFISH_TEXT_RE = re.compile(r"(?<![\w.-])(?:sf|sfdx)(?![\w-])|@salesforce/cli", re.I)
VF_TEXT_RE = re.compile(r"vf-check\.mjs|vf-setup\.js")
# Ways of choosing the org (or the sf state directory) that the hook cannot resolve.
UNRESOLVABLE_RE = re.compile(
    r"--flags-dir|\b(?:SF_TARGET_ORG|SFDX_DEFAULTUSERNAME|SFDX_TARGET_ORG|SF_TARGET_DEV_HUB|"
    r"SFDX_DEFAULTDEVHUBUSERNAME)\b|\b(?:SF|SFDX)_\w*DIR\b|(?<![\w$])HOME=|\bXDG_\w+="
    r"|(?:--target-org|--targetusername|--target-dev-hub|--targetdevhubusername|(?<!\S)-[ouv])"
    r"(?:=|\s+)?[\"']?[$`]")
ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
SAFE_WORD_RE = re.compile(r"^[a-z][a-z0-9-]{0,30}$")

# ---------------------------------------------------------------- MCP and browser

SF_SERVER_RE = re.compile(r"salesforce|sfdx|(^|[_-])sf([_-]|$)", re.I)
BROWSER_SERVER_RE = re.compile(r"playwright|browser|puppeteer|chrome", re.I)
MCP_READ_WORDS = {"query", "describe", "list", "get", "retrieve", "display", "search", "find"}
MCP_WRITE_WORDS = {
    "deploy", "create", "update", "delete", "upsert", "anonymous", "execute", "dml", "assign",
    "insert", "resume", "install", "uninstall", "publish", "send", "post", "put", "patch",
    "save", "reset", "enable", "disable", "cancel", "remove", "modify", "edit", "write",
    "import", "load", "push", "submit", "approve", "activate", "deactivate", "merge",
    "convert", "invoke", "set", "add", "start", "stop", "open", "login", "clone", "copy",
    "move", "upload", "share", "trash", "restore", "refresh", "promote", "apex",
}
ORG_KEY_RE = re.compile(
    r"^(targetorg|org|orgalias|orgname|alias|username|usernameoralias|"
    r"targetusername|instanceurl|targetdevhub|devhub)$")
# Navigation in browser code; the URL argument must be a provably safe string literal.
BROWSER_NAV_CALL_RE = re.compile(
    r"\.goto\s*\(|\blocation\s*\.\s*(?:assign|replace)\s*\(|\bwindow\s*\.\s*open\s*\(|"
    r"\bfetch\s*\(|\.request\s*\.\s*(?:get|post|put|patch|delete|head|fetch)\s*\(|"
    r"\.open\s*\(|\bsendBeacon\s*\(|\bimportScripts\s*\(|\bnew\s+(?:WebSocket|EventSource)\s*\(|"
    r"\b(?:location|href|src|action|formAction)\s*=(?!=)|\blocation\s*\.\s*href\s*=(?!=)",
    re.I)
JS_DECODER_RE = re.compile(
    r"\batob\s*\(|\bBuffer\s*\.\s*from\b|fromCharCode|fromCodePoint|decodeURI|\bunescape\s*\(|"
    r"base64|TextDecoder|\\x[0-9a-f]{2}|\\u[0-9a-f{]|\beval\s*\(|\bFunction\s*\(|reverse\s*\(",
    re.I)
BROWSER_CODE_TOOL_RE = re.compile(r"run_code|evaluate|eval|script|code", re.I)
NAV_CODE_RE = re.compile(r"goto|location|navigat|window\.open|href|reload|fetch|newPage|"
                         r"open\s*\(|XMLHttpRequest|sendBeacon|src\s*=", re.I)
# Salesforce-ish fragments; each must sit inside a host that is provably safe.
SF_FRAGMENT_RE = re.compile(
    r"salesforce|visualforce|cloudforce|(?<![a-z0-9-])force\.com|(?<![a-z0-9-])site\.com", re.I)
SF_HOST_RE = re.compile(
    r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9-]+)*\."
    r"(?:salesforce|force|site|salesforce-sites|salesforce-setup|visualforce|cloudforce|"
    r"salesforce-experience)\.com(?![a-z0-9-])", re.I)
PUBLIC_SF_HOSTS = {"developer.salesforce.com", "help.salesforce.com", "trailhead.salesforce.com",
                   "www.salesforce.com", "status.salesforce.com", "salesforce.com",
                   "test.salesforce.com"}  # the generic sandbox login page


# ---------------------------------------------------------------- org lookup

def _home():
    return os.path.expanduser("~")


def _load_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def load_aliases():
    """alias -> username from ~/.sfdx/alias.json and ~/.sf/alias.json."""
    aliases = {}
    for path in (os.path.join(_home(), ".sfdx", "alias.json"),
                 os.path.join(_home(), ".sf", "alias.json")):
        orgs = _load_json(path).get("orgs")
        if isinstance(orgs, dict):
            for alias, user in orgs.items():
                if isinstance(alias, str) and isinstance(user, str):
                    aliases.setdefault(alias, user)
    return aliases


def auth_usernames():
    """Usernames that have an auth file in ~/.sfdx (file names only, contents not read)."""
    try:
        names = os.listdir(os.path.join(_home(), ".sfdx"))
    except OSError:
        return set()
    return {n[:-5] for n in names if n.endswith(".json") and "@" in n and not n.startswith(".")}


def instance_url(username):
    """The org's instanceUrl from its auth file, or None. Reads nothing else."""
    if not username or not re.match(r"^[^/\\\x00]+$", username) or username.startswith("."):
        return None
    data = _load_json(os.path.join(_home(), ".sfdx", username + ".json"))
    url = data.get("instanceUrl")
    del data  # drop the tokens as early as possible
    return url if isinstance(url, str) else None


def host_of(url):
    try:
        return (urlparse(url if "://" in url else "https://" + url).hostname or "").lower()
    except Exception:
        return ""


def is_sandbox_host(host):
    """True only for hosts that are provably a sandbox or scratch org."""
    host = (host or "").lower()
    if ".sandbox." in host or ".scratch." in host:
        return True
    # Legacy (pre enhanced domains) sandbox My Domain: mydomain--sandbox.my.salesforce.com.
    # Production Visualforce and container hosts also use "--", so no other domain qualifies.
    return (host.endswith(".my.salesforce.com") and host.count(".") == 3
            and "--" in host.split(".")[0] and not host.split(".")[0].endswith("--c"))


def is_sandbox_url(url):
    return is_sandbox_host(host_of(url))


def env_prod_orgs():
    raw = os.environ.get("SHEPHERD_PROD_ORGS", "")
    return {p.strip().lower() for p in raw.split(",") if p.strip()}


def prod_reason(org, aliases=None):
    """Why `org` counts as production, or None if it is a sandbox/scratch org."""
    org = (org or "").strip()
    if not org:
        return "no target org could be determined"
    aliases = load_aliases() if aliases is None else aliases
    listed = env_prod_orgs()

    if SF_HOST_RE.fullmatch(org) or re.match(r"^https?://", org, re.I):
        if org.lower().rstrip("/") in listed or host_of(org) in listed:
            return "listed in SHEPHERD_PROD_ORGS"
        if not is_sandbox_url(org):
            return "the instance URL is not a sandbox or scratch org"
        return None

    lower_aliases = {a.lower(): u for a, u in aliases.items()}
    username = aliases.get(org) or lower_aliases.get(org.lower()) or org
    names = {org.lower(), username.lower()}
    names |= {a.lower() for a, u in aliases.items() if u.lower() == username.lower()}
    if names & listed:
        return "listed in SHEPHERD_PROD_ORGS"
    if any("prod" in n for n in names):
        return "its name contains 'prod'"
    url = instance_url(username)
    if not url:
        return "there is no auth file with an instanceUrl for it"
    if not is_sandbox_url(url):
        return "its instanceUrl is not a sandbox or scratch org"
    return None


class Orgs:
    """What the hook knows about local orgs, computed once per call."""

    def __init__(self):
        self.aliases = load_aliases()
        self.known = {a.lower(): a for a in self.aliases}
        self.known.update({u.lower(): u for u in self.aliases.values()})
        self._prod = None

    def describe(self, org):
        """How a deny message may name `org`: only known aliases/usernames are echoed."""
        name = self.known.get((org or "").strip().lower())
        return f"production org '{name}'" if name else UNVERIFIED

    def prod_ids(self):
        """(names, hosts): production aliases, usernames and instance hosts."""
        if self._prod is None:
            names, hosts = set(env_prod_orgs()), set()
            for alias in self.aliases:
                if prod_reason(alias, self.aliases):
                    names.add(alias.lower())
            for user in set(self.aliases.values()) | auth_usernames():
                if prod_reason(user, self.aliases):
                    names.add(user.lower())
                    url = instance_url(user)
                    if url and host_of(url):
                        hosts.add(host_of(url))
            names.discard("")
            self._prod = (names, hosts)
        return self._prod

    def mentioned_prod(self, text):
        """The first production alias/username/host named in `text`, or None."""
        names, hosts = self.prod_ids()
        for ident in sorted(names | hosts, key=len, reverse=True):
            if re.search(r"(?<![\w.@-])" + re.escape(ident) + r"(?![\w@-]|\.\w)", text, re.I):
                return ident
        return None


# ---------------------------------------------------------------- bash parsing

class Unparseable(Exception):
    pass


def split_segments(cmd):
    """Split on ; & | newlines ( ) { } and backticks outside quotes; ${VAR} stays whole.

    Raises Unparseable when a quote is left open."""
    cmd = cmd.replace("\\\n", " ")
    segs, buf, quote, i = [], [], None, 0
    while i < len(cmd):
        c = cmd[i]
        if quote:
            buf.append(c)
            if c == "\\" and quote == '"' and i + 1 < len(cmd):
                buf.append(cmd[i + 1])
                i += 1
            elif c == quote:
                quote = None
        elif c == "\\" and i + 1 < len(cmd):
            buf.append(c)
            buf.append(cmd[i + 1])
            i += 1
        elif c in "'\"":
            quote = c
            buf.append(c)
        elif c == "$" and cmd[i + 1:i + 2] == "{":
            end = cmd.find("}", i)
            end = len(cmd) - 1 if end < 0 else end
            buf.append(cmd[i:end + 1])
            i = end
        elif c in ";&|\n(){}`":
            segs.append("".join(buf))
            buf = []
        else:
            buf.append(c)
        i += 1
    if quote:
        raise Unparseable("unbalanced quote")
    segs.append("".join(buf))
    return [s.strip() for s in segs if s.strip()]


def tokenize(seg):
    try:
        toks = shlex.split(seg)
    except ValueError as e:
        raise Unparseable(str(e))
    for i, t in enumerate(toks):
        if t.startswith("#"):
            return toks[:i]
    return toks


def is_sf_token(tok):
    return (not tok.startswith("-")
            and (os.path.basename(tok) in ("sf", "sfdx") or "@salesforce/cli" in tok))


def has_sf_piece(tok):
    return any(is_sf_token(p) for p in re.split(r"[=\s,:;]", tok) if p)


def default_org(cwd, env, dev_hub=False):
    if dev_hub:
        env_keys, key, legacy_key = (("SF_TARGET_DEV_HUB", "SFDX_DEFAULTDEVHUBUSERNAME"),
                                     "target-dev-hub", "defaultdevhubusername")
    else:
        env_keys, key, legacy_key = (("SF_TARGET_ORG", "SFDX_DEFAULTUSERNAME"),
                                     "target-org", "defaultusername")
    for k in env_keys:
        v = env.get(k) or os.environ.get(k)
        if v:
            return v
    d = os.path.abspath(cwd or os.getcwd())
    while True:
        for path, k in ((os.path.join(d, ".sf", "config.json"), key),
                        (os.path.join(d, ".sfdx", "sfdx-config.json"), legacy_key)):
            v = _load_json(path).get(k)
            if isinstance(v, str) and v:
                return v
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    for path, k in ((os.path.join(_home(), ".sf", "config.json"), key),
                    (os.path.join(_home(), ".sfdx", "sfdx-config.json"), legacy_key)):
        v = _load_json(path).get(k)
        if isinstance(v, str) and v:
            return v
    return None


def flag_values(rest, long_names, short=None):
    """Every value given to a flag, in any form: --f v, --f=v, -X v, -Xv, -X=v."""
    values = []
    for j, tok in enumerate(rest):
        nxt = rest[j + 1] if j + 1 < len(rest) else ""
        if tok in long_names or (short and tok == short):
            values.append(nxt)
        elif "=" in tok and tok.split("=", 1)[0] in long_names:
            values.append(tok.split("=", 1)[1])
        elif short and tok.startswith(short) and len(tok) > len(short):
            values.append(tok[len(short):].lstrip("="))
    return values


def has_flag(rest, long_name, short=None):
    return any(t == long_name or t.startswith(long_name + "=") or
               (short and (t == short or (t.startswith(short) and len(t) > len(short))))
               for t in rest)


def sf_read_only(words, rest):
    """True if this sf command only reads, or never talks to an org."""
    if "--help" in rest or "-h" in rest or "help" in words[:1]:
        return True
    if not words:
        return all(t in ("--version", "-v", "--help", "-h") for t in rest)
    if words[0] in SF_NO_ORG_TOPICS:
        return True
    w = tuple(words)
    if w in SF_READ_ONLY:
        return True
    if w == ("project", "deploy", "start"):
        return "--dry-run" in rest
    if w == ("apex", "tail", "log"):
        return "--skip-trace-flag" in rest
    if w == ("api", "request", "rest"):
        if has_flag(rest, "--file", "-f"):
            return False
        methods = flag_values(rest, {"--method"}, "-X")
        return bool(methods) and all(m.strip().upper() in READ_METHODS for m in methods)
    if w == ("api", "request", "graphql"):
        if has_flag(rest, "--file", "-f"):
            return False
        bodies = flag_values(rest, {"--body", "--query"}, "-b")
        joined = " ".join(rest).lower()
        return (bool(bodies) and all("{" in b for b in bodies)
                and "mutation" not in joined and "subscription" not in joined)
    return False


def sf_targets(words, rest, cwd, env):
    topic = words[1] if words and words[0] == "force" and len(words) > 1 else (words[0] if words else "")
    names = set(ORG_FLAGS) | ({"-v"} if topic in ("org", "package", "package1") else set())
    targets = []
    for j, tok in enumerate(rest):
        if tok.startswith("-") and "=" in tok and tok.split("=", 1)[0] in names:
            targets.append(tok.split("=", 1)[1])
        elif tok in names:
            nxt = rest[j + 1] if j + 1 < len(rest) else ""
            targets.append("" if nxt.startswith("-") else nxt)
        elif re.match(r"^-[ou][^-=]", tok):
            targets.append(tok[2:])
    targets = ["" if re.search(r"[$`]", t) else t for t in targets]
    if targets:
        return targets
    defaults = [default_org(cwd, env)]
    if topic == "package" or "scratch" in words:
        defaults.append(default_org(cwd, env, dev_hub=True))
    found = [d for d in defaults if d]
    return found or [""]


class Walk:
    """State for one Bash command: what is known about orgs and the production context."""

    def __init__(self, cmd, orgs):
        self.orgs = orgs
        self.sensitive = bool(SFISH_TEXT_RE.search(cmd) or VF_TEXT_RE.search(cmd))
        self.prod_ctx = None
        mentioned = orgs.mentioned_prod(cmd)
        if mentioned:
            self.prod_ctx = ("the command names " + orgs.describe(mentioned)
                             if orgs.known.get(mentioned) else
                             "the command names a production org or host")
        elif self.sensitive and UNRESOLVABLE_RE.search(cmd):
            self.prod_ctx = ("the command chooses the org through a flags directory, "
                             "environment variable, variable or other HOME, which the guard "
                             "cannot resolve")
        self.sensitive = self.sensitive or bool(self.prod_ctx)
        self.cmd = cmd
        self.decoder = bool(DECODER_RE.search(cmd))
        # Opaque code (stdin shells, computed eval, inline interpreter code) is denied when
        # the command has anything to do with Salesforce or decodes something.
        self.risky = self.sensitive or self.decoder

    def code_sensitive(self, code):
        return bool(SFISH_TEXT_RE.search(code) or VF_TEXT_RE.search(code)
                    or UNRESOLVABLE_RE.search(code) or self.orgs.mentioned_prod(code))

    def deny(self, what, why):
        return f"Blocked by shepherd prod-guard: {what} {why}. {REASON_TAIL}"

    def prod_block(self, label, targets):
        if self.prod_ctx:
            return self.deny(label, f"is not on the production read-only allowlist and "
                                    f"{self.prod_ctx}")
        for org in targets:
            why = prod_reason(org, self.orgs.aliases)
            if why:
                return self.deny(label, f"is not on the production read-only allowlist and "
                                        f"targets {self.orgs.describe(org)} ({why})")
        return None


def sf_label(words):
    if words and all(w in SF_KNOWN_WORDS for w in words[:4]):
        return "`sf " + " ".join(words[:4]) + "`"
    return "an sf command"


def check_sf(args, cwd, env, st):
    while args and args[0] == "--":
        args = args[1:]
    words, rest = [], []
    for j, tok in enumerate(args):
        # Command words end at the first flag or argument (e.g. the URL of `api request rest`).
        if not re.match(r"^[A-Za-z][A-Za-z0-9-]*(?::[A-Za-z0-9-]+)*$", tok):
            rest = args[j:]
            break
        words.extend(w.lower() for w in tok.split(":") if w)
    if sf_read_only(words, rest):
        return None
    return st.prod_block(sf_label(words), sf_targets(words, rest, cwd, env))


def check_vf(script, args, cwd, env, st):
    if script == "vf-check.mjs":
        name, string_opts, safe = "vf-check", VF_CHECK_STRING_OPTS, VF_CHECK_SAFE
    else:
        name, string_opts, safe = "vf-setup", VF_SETUP_STRING_OPTS, VF_SETUP_SAFE
    positionals, targets, project_dir, j = [], [], None, 0
    while j < len(args):
        tok = args[j]
        if tok == "--":
            positionals.extend(args[j + 1:])
            break
        if tok.startswith("-") and "=" in tok:
            key, value = tok.split("=", 1)
        elif tok in string_opts:
            key, value = tok, (args[j + 1] if j + 1 < len(args) else "")
            j += 1
        elif re.match(r"^-o[^-=]", tok):
            key, value = "-o", tok[2:]
        elif tok.startswith("-"):
            key, value = tok, None
        else:
            positionals.append(tok)
            j += 1
            continue
        if key in ("--target-org", "-o"):
            targets.append("" if value.startswith("-") or re.search(r"[$`]", value) else value)
        elif key == "--project-dir":
            project_dir = value
        elif key in ("--help", "-h"):
            return None
        j += 1
    command = positionals[0] if positionals else ""
    if not command or command in safe:
        return None
    # vibe-force never uses the sf default org: without --target-org it picks
    # config.orgs[--env or defaultOrgKey] from .vibeforce/config.json, which the hook does not
    # resolve. So only an explicit target that resolves to a sandbox lets these checks run.
    if not targets:
        targets = [""]
    label = f"`{name} {command}`" if command in VF_KNOWN else f"a {name} command"
    return st.prod_block(label, targets)


def check_http(toks, st):
    text = " ".join(toks)
    for m in SF_HOST_RE.finditer(text):
        host = m.group(0).lower()
        if not is_sandbox_host(host) and host not in PUBLIC_SF_HOSTS:
            return st.deny("an HTTP client call", "goes to a Salesforce host that is not a "
                                                  "provable sandbox")
    if st.orgs.mentioned_prod(text):
        return st.deny("an HTTP client call", "names a production org or host")
    return None


def walk(cmd, cwd, st, depth=0):
    if depth > 6:
        return st.deny("a command", "is nested too deeply to check") if st.sensitive else None
    try:
        segments = split_segments(cmd)
    except Unparseable:
        return st.deny("a command", "could not be parsed") if st.sensitive else None
    for seg in segments:
        try:
            toks = tokenize(seg)
        except Unparseable:
            if st.sensitive:
                return st.deny("a command", "could not be parsed")
            continue
        # Quoted strings that hold commands: bash -lc '...', su -c, ssh host '...', "$(...)".
        for tok in toks:
            if re.search(r"\s", tok) and (SFISH_TEXT_RE.search(tok) or VF_TEXT_RE.search(tok)
                                          or "$(" in tok or "`" in tok):
                reason = walk(tok, cwd, st, depth + 1)
                if reason:
                    return reason
        env, i = {}, 0
        while i < len(toks):
            tok = toks[i]
            if ASSIGN_RE.match(tok):
                k, v = tok.split("=", 1)
                env[k] = v
                i += 1
            elif os.path.basename(tok) in WRAPPERS:
                i += 1
                while i < len(toks) and (toks[i].startswith("-") or
                                         (tok == "timeout" and re.match(r"^\d", toks[i]))):
                    i += 1
            else:
                break
        head = toks[i] if i < len(toks) else ""
        base = os.path.basename(head)
        if head == "cd":
            target = os.path.expanduser(toks[i + 1]) if i + 1 < len(toks) else _home()
            cwd = os.path.normpath(os.path.join(cwd or os.getcwd(), target))
            continue
        if base in HTTP_TOOLS:
            reason = check_http(toks[i + 1:], st)
            if reason:
                return reason
            continue
        if base.lower() in SHELLS or base in ("source", "."):
            reason = check_shell(base.lower(), toks[i + 1:], st)
            if reason:
                return reason
        if base in ("eval", "iex") or base.lower() == "invoke-expression":
            code = " ".join(toks[i + 1:])
            if st.risky and re.search(r"[$`]", code):
                return st.deny("an eval of a computed string", "cannot be checked and the "
                                                               "command involves Salesforce")

        # Find the invocation this segment makes, if any.
        kind, args = None, []
        if base in NODE_RUNNERS:
            k = next((n for n in range(i + 1, len(toks)) if not toks[n].startswith("-")), None)
            if k is not None and os.path.basename(toks[k]) in ("vf-check.mjs", "vf-setup.js"):
                kind, args, base = "vf", toks[k + 1:], os.path.basename(toks[k])
        elif base in ("vf-check.mjs", "vf-setup.js"):
            kind, args = "vf", toks[i + 1:]
        if kind is None:
            invocations = sf_invocations(toks, i)
            if invocations:
                kind, args = "sf", invocations
            elif st.sensitive and head and (head.startswith("$") or
                                            head.split(":")[0].lower() in SF_TOPIC_HEADS):
                if st.prod_ctx and head.startswith("$"):
                    return st.deny("a command run through a variable",
                                   f"cannot be checked and {st.prod_ctx}")
                kind, args = "sf", [toks[i + 1:] if head.startswith("$") else toks[i:]]
        if kind is None and INTERPRETER_RE.match(base):
            reason = check_interpreter(base, toks[i + 1:], st)
            if reason:
                return reason
        if kind is None:
            if st.prod_ctx and any(has_sf_piece(t) for t in toks):
                return st.deny("a command that mentions sf", f"cannot be checked and "
                                                             f"{st.prod_ctx}")
            continue
        if kind == "vf":
            reason = check_vf(base, args, cwd, env, st)
            if reason:
                return reason
            continue
        for inv in args:
            reason = check_sf(inv, cwd, env, st)
            if reason:
                return reason
    return None


def sf_invocations(toks, start):
    """Every sf/sfdx invocation in one segment, each with its own arguments.

    `find -exec sf ... \\; -exec sf ... \\;` and `xargs sf` run several; an invocation's
    arguments end at the next `;` or `+` token (find) or the next sf token."""
    found, n = [], start
    while n < len(toks):
        if not is_sf_token(toks[n]):
            n += 1
            continue
        while n + 1 < len(toks) and is_sf_token(toks[n + 1]):
            n += 1  # npx -p @salesforce/cli sf ...
        end = n + 1
        while end < len(toks) and toks[end] not in (";", "+") and not is_sf_token(toks[end]):
            end += 1
        found.append(toks[n + 1:end])
        n = end
    return found


def check_shell(base, args, st):
    """Shells and `source`: encoded, computed or piped-in code cannot be checked."""
    if base.startswith(("pwsh", "powershell")) and any(PS_ENCODED_RE.fullmatch(a) for a in args):
        return st.deny("an encoded PowerShell command", "cannot be checked")
    if base in ("source", "."):
        stdin = not args or args[0] in ("-", "/dev/stdin", "<") or args[0].startswith("/dev/fd")
        if stdin and st.risky:
            return st.deny("sourcing code from a pipe", "cannot be checked and the command "
                                                        "involves Salesforce or decodes data")
        return None
    for n, a in enumerate(args):
        is_c = (a in ("-c", "/c", "/C", "-Command", "-command", "/k", "/K")
                or re.match(r"^-[a-zA-Z]*c[a-zA-Z]*$", a) is not None and not a.startswith("--"))
        if is_c:
            code = args[n + 1] if n + 1 < len(args) else ""
            if st.risky and re.search(r"\$\(|`|\$\{?\w", code):
                return st.deny("a shell running a computed string", "cannot be checked and the "
                               "command involves Salesforce or decodes data")
            return None
    scripts = [a for a in args if not a.startswith("-")]
    if (not scripts or "-s" in args or scripts[0] in ("-", "/dev/stdin")) and st.risky:
        return st.deny("a shell reading code from a pipe or here-document",
                       "cannot be checked and the command involves Salesforce or decodes data")
    return None


def check_interpreter(base, args, st):
    """python -c, node -e, perl -e and friends: their code may start sf or call the API."""
    lower = base.lower()
    if re.match(r"^[gmn]?awk$", lower):
        code = " ".join(a for a in args if not a.startswith("-"))
    elif lower == "deno" and args[:1] == ["eval"]:
        code = " ".join(args[1:])
    else:
        code = None
        for n, a in enumerate(args):
            if a in INLINE_CODE_FLAGS or re.match(r"^-[a-zA-Z]*[ceE]$", a):
                code = args[n + 1] if n + 1 < len(args) else ""
                break
    what = f"inline {lower} code" if SAFE_WORD_RE.match(lower) else "inline interpreter code"
    if code is not None:
        if st.code_sensitive(code):
            return st.deny(what, "mentions Salesforce or a production org and cannot be checked")
        if st.risky and SPAWN_RE.search(code):
            return st.deny(what, "can start processes or reach the network, and the command "
                                 "involves Salesforce or decodes data")
        return None
    scripts = [a for a in args if not a.startswith("-")]
    if (not scripts or scripts[0] in ("-", "/dev/stdin")) and st.risky and SPAWN_RE.search(st.cmd):
        return st.deny(f"{lower if SAFE_WORD_RE.match(lower) else 'an interpreter'} reading code "
                       f"from a pipe or here-document", "cannot be checked and the command "
                       "involves Salesforce or decodes data")
    return None


def normalize_powershell(cmd):
    """Rewrite the PowerShell forms the Bash walker does not know into shell-like text."""
    cmd = cmd.replace("`", "")  # the PowerShell escape character: s`f is sf

    def start_process(m):
        return m.group(1) + " " + re.sub(r"[\"',]", " ", m.group(2))

    cmd = re.sub(r"(?im)\b(?:start-process|saps|start)\s+(?:-FilePath\s+)?(\S+)\s+"
                 r"(?:-(?:ArgumentList|Args)\s+)?([^;\n|]*)", start_process, cmd)
    cmd = re.sub(r"(?i)\b(?:invoke-expression|iex)\b", "eval", cmd)
    cmd = re.sub(r"(?i)\b(?:invoke-command|icm)\b[^{;\n]*", "", cmd)
    return cmd


def analyze_bash(cmd, cwd, powershell=False):
    if not isinstance(cmd, str):
        raise TypeError("command is not a string")
    if powershell:
        cmd = normalize_powershell(cmd)
    orgs = Orgs()
    st = Walk(cmd, orgs)
    if (not st.sensitive and not st.decoder
            and not re.search(r"\b(?:" + "|".join(HTTP_TOOLS) + r")\b", cmd)):
        return None
    return walk(cmd, cwd, st)


# ---------------------------------------------------------------- MCP

def _strings(obj, key=None, out=None):
    out = [] if out is None else out
    if isinstance(obj, dict):
        for k, v in obj.items():
            _strings(v, k, out)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _strings(v, key, out)
    elif isinstance(obj, (str, int, float)) and not isinstance(obj, bool):
        out.append((key, str(obj)))
    return out


def tool_words(name):
    return {w.lower() for w in re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+", name)}


def is_read_tool(tool):
    words = tool_words(tool)
    return bool(words & MCP_READ_WORDS) and not (words & MCP_WRITE_WORDS)


def mcp_orgs(tool_input, orgs, sf_server):
    found = []
    pairs = _strings(tool_input)
    text = "\n".join(v for _, v in pairs)
    if sf_server:
        for key, value in pairs:
            if ORG_KEY_RE.match(re.sub(r"[^a-z]", "", (key or "").lower())) and value.strip():
                found.append(value.strip())
        found.extend(m.group(0) for m in SF_HOST_RE.finditer(text))
    names = set(orgs.aliases) | set(orgs.aliases.values()) | env_prod_orgs()
    for name in names:
        if name and re.search(r"(?<![\w.@-])" + re.escape(name) + r"(?![\w@-]|\.\w)", text, re.I):
            found.append(name)
    return found


def url_host(url):
    """The host a browser would use: backslashes act as slashes, tabs/newlines are dropped,
    and userinfo (user:pass@) is not part of the host. "" if it cannot be parsed."""
    url = re.sub(r"[\t\r\n]", "", url).replace("\\", "/")
    try:
        return (urlparse(url).hostname or "").lower().rstrip(".")
    except Exception:
        return ""


def bad_host(host, prod_hosts):
    """Why a browser must not open `host`, or None."""
    host = (host or "").lower().rstrip(".")
    if host in prod_hosts:
        return "the instance host of a production org"
    if SF_HOST_RE.fullmatch(host) and not is_sandbox_host(host) and host not in PUBLIC_SF_HOSTS:
        return "a Salesforce host that is not provably a sandbox or scratch org"
    return None


def browser_texts(tool_input):
    """Every string argument, also percent-decoded once and twice (redirect parameters)."""
    for _, value in _strings(tool_input):
        seen = [value]
        for _ in range(2):
            decoded = unquote(seen[-1])
            if decoded == seen[-1]:
                break
            seen.append(decoded)
        yield from seen


def js_literal_arg(code, pos):
    """The string literal starting at `pos` (after optional whitespace) when it is a whole
    argument or value, else None. Literals with escapes or ${...} do not count."""
    m = re.compile(r"\s*(?:'([^'\\\n]*)'|\"([^\"\\\n]*)\"|`([^`\\$]*)`)\s*(?=[,);}\n]|$)"
                   ).match(code, pos)
    if not m:
        return None
    return next(g for g in m.groups() if g is not None)


def check_browser_code(code, prod_hosts):
    """Navigation in browser code must go to a string literal that is provably not production."""
    sites = [(m.end(), m.group(0)) for m in BROWSER_NAV_CALL_RE.finditer(code)]
    if not sites:
        return None
    if JS_DECODER_RE.search(code):
        return "a URL decoded at run time (navigation combined with a decoder)"
    for pos, call in sites:
        # xhr.open('GET', url): the URL is the second argument
        method = (re.compile(r"\s*(['\"])[A-Za-z]+\1\s*,").match(code, pos)
                  if re.search(r"\.open\s*\($", call) else None)
        literal = js_literal_arg(code, method.end() if method else pos)
        if literal is None:
            return "a URL computed at run time, which the guard cannot check"
        lowered = literal.strip().lower()
        if re.match(r"^(?:javascript|data|blob|vbscript):", lowered):
            return "a script or data URL, which the guard cannot check"
        if "://" in lowered or lowered.startswith("//"):
            host = url_host(literal if "://" in lowered else "https:" + literal.strip())
            if not host:
                return "a URL the guard cannot parse"
            why = bad_host(host, prod_hosts)
            if why:
                return why
        elif bad_host(lowered.split("/")[0], prod_hosts):
            return bad_host(lowered.split("/")[0], prod_hosts)
    return None


def analyze_browser(tool_name, tool, tool_input, orgs):
    _, prod_hosts = orgs.prod_ids()
    why = None
    for text in browser_texts(tool_input):
        spans = []
        for m in re.finditer(r"[a-z][a-z0-9+.-]*:/{2}|[a-z][a-z0-9+.-]*:\\\\", text, re.I):
            end = re.compile(r"[\s\"'<>`]|$").search(text, m.end()).start()
            spans.append((m.start(), end))
            url = text[m.start():end]
            host = url_host(url)
            if host:
                why = bad_host(host, prod_hosts)
            else:  # unparseable: any unsafe Salesforce host inside it counts
                why = next((bad_host(h.group(0), prod_hosts) for h in SF_HOST_RE.finditer(url)
                            if bad_host(h.group(0), prod_hosts)), None)
            if why:
                break
        if why:
            break
        # Bare hosts outside URLs (not userinfo, which is followed by "@").
        for m in re.finditer(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9-]+)+", text, re.I):
            if any(a <= m.start() < b for a, b in spans) or text[m.end():m.end() + 1] == "@":
                continue
            why = bad_host(m.group(0), prod_hosts)
            if why:
                break
        if why:
            break
        if BROWSER_CODE_TOOL_RE.search(tool):
            why = check_browser_code(text, prod_hosts)
            if why:
                break
        # Code that navigates: URLs may be assembled from pieces, so any Salesforce-ish
        # fragment outside a provably safe host counts.
        if BROWSER_CODE_TOOL_RE.search(tool) and NAV_CODE_RE.search(text):
            safe = [h.span() for h in SF_HOST_RE.finditer(text)
                    if is_sandbox_host(h.group(0)) or h.group(0).lower() in PUBLIC_SF_HOSTS]
            for f in SF_FRAGMENT_RE.finditer(text):
                if not any(a <= f.start() and f.end() <= b for a, b in safe):
                    why = "a Salesforce host that is not provably a sandbox or scratch org"
                    break
        if why:
            break
    if not why:
        return None
    return (f"Blocked by shepherd prod-guard: browser tool `{tool_name}` would open {why}. "
            f"Production is read-only for agents. Do not retry or work around this block; "
            f"ask the user to do it themselves.")


def mcp_write_content(tool_input):
    """A read-named tool whose input still carries a write: a GraphQL mutation, or DML in a
    query/statement field."""
    for key, value in _strings(tool_input):
        if re.search(r"\bmutation\b", value, re.I):
            return True
        norm = re.sub(r"[^a-z]", "", (key or "").lower())
        if re.search(r"query|soql|sql|statement|body|graphql|command|script|apex|code", norm):
            text = re.sub(r"\bfor\s+(?:update|view|reference)\b", "", value, flags=re.I)
            if re.search(r"\b(?:insert|update|delete|upsert|merge|undelete|truncate|drop|"
                         r"alter|create)\b", text, re.I):
                return True
    return False


def analyze_mcp(tool_name, tool_input):
    parts = tool_name.split("__")
    server = parts[1] if len(parts) > 2 else tool_name
    tool = parts[-1]
    orgs = Orgs()
    if BROWSER_SERVER_RE.search(server) or tool.lower().startswith("browser_"):
        return analyze_browser(tool_name, tool, tool_input, orgs)
    sf_server = bool(SF_SERVER_RE.search(server))
    found = mcp_orgs(tool_input, orgs, sf_server)
    if not sf_server and not found:
        return None
    if is_read_tool(tool) and not mcp_write_content(tool_input):
        return None
    if not found:
        for key, value in _strings(tool_input):
            if re.sub(r"[^a-z]", "", (key or "").lower()) in ("directory", "projectdir", "cwd"):
                found = [default_org(value, {}) or ""]
                break
    head = (f"Blocked by shepherd prod-guard: MCP tool `{tool_name}` is not a read-only tool "
            f"and")
    if not found:
        return f"{head} names no org, so it is treated as production. {REASON_TAIL}"
    for org in found:
        why = prod_reason(org, orgs.aliases)
        if why:
            return f"{head} targets {orgs.describe(org)} ({why}). {REASON_TAIL}"
    return None


# ---------------------------------------------------------------- entry point

def evaluate(payload):
    """The deny reason for a PreToolUse payload, or None to allow."""
    tool_name = payload.get("tool_name") or ""
    tool_input = payload.get("tool_input") or {}
    cwd = payload.get("cwd") or os.getcwd()
    if tool_name in ("Bash", "PowerShell"):
        return analyze_bash(tool_input.get("command", ""), cwd, tool_name == "PowerShell")
    if tool_name.startswith("mcp__"):
        return analyze_mcp(tool_name, tool_input)
    return None


def looks_sensitive(raw):
    """On an internal error: does the payload have anything to do with Salesforce?"""
    if SFISH_TEXT_RE.search(raw) or VF_TEXT_RE.search(raw) or SF_FRAGMENT_RE.search(raw):
        return True
    try:
        return bool(Orgs().mentioned_prod(raw))
    except Exception:
        return True


def deny(reason):
    return json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }})


def main():
    raw = ""
    try:
        raw = sys.stdin.read()
        reason = evaluate(json.loads(raw))
    except Exception as e:
        reason = None
        try:
            sensitive = looks_sensitive(raw)
        except Exception:
            sensitive = True
        if sensitive:
            reason = (f"Blocked by shepherd prod-guard: internal error ({type(e).__name__}) "
                      f"while checking a call that involves Salesforce, so it is blocked "
                      f"(fail closed). {REASON_TAIL}")
    if reason:
        try:
            print(deny(reason))
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    try:
        main()
    except BaseException:
        pass
    sys.exit(0)
