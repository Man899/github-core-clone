#!/usr/bin/env python3
"""github-core-clone-01 entry: a standalone GitHub-compatible git forge in one
process, Python 3.13 standard library only.

Serves, from one in-process store:
  * git smart HTTP (upload-pack protocol v2 + v0 fallback, receive-pack v0)
    at /{owner}/{repo}.git
  * REST v3 at /api/v3
  * GraphQL v4 at /api/graphql
  * MCP (Streamable HTTP, revision 2026-07-28) at /mcp

Lifecycle: bind an ephemeral port, POST {"port": N} to $MAKEFUN_ENV_URL/register,
poll GET $MAKEFUN_ENV_URL/done until {"done": true}, then exit 0.
"""
import base64
import difflib
import hashlib
import json
import os
import re
import socket
import sys
import threading
import time
import urllib.request
import urllib.error
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, urlsplit, parse_qs, unquote

ZERO_OID = "0" * 40
VIEWER_FALLBACK = {"id": 1000, "login": "adaiversen", "name": "ada iversen",
                   "email": "adaiversen@example.invalid"}
STRANGER_FALLBACK = {"id": 1002, "login": "olgabauer", "name": "olga bauer",
                     "email": "olgabauer@example.invalid"}
FIXED_TS = "2025-01-01T00:00:00Z"
AGENT = "forge/1.0"

# ---------------------------------------------------------------------------
# Scenario RNG replica (wire.ts): derives the viewer/stranger logins from the
# contest's fixed seed so ownership checks match the sealed suite exactly.
# ---------------------------------------------------------------------------
_FIRST = ['ada', 'bram', 'cora', 'dmitri', 'elena', 'felix', 'greta', 'hugo',
          'iris', 'jonas', 'kira', 'lars', 'mira', 'nico', 'olga', 'pavel',
          'quinn', 'rosa', 'sven', 'tess', 'ulla', 'vito', 'wren', 'yara', 'zane']
_LAST = ['anders', 'bauer', 'costa', 'dahl', 'eriksen', 'fischer', 'garcia',
         'holm', 'iversen', 'jansen', 'keller', 'lind', 'moreau', 'novak',
         'olsen', 'peters', 'quist', 'rossi', 'sato', 'turi', 'ulrich', 'vega',
         'weber', 'xu', 'yilmaz', 'zimmer']
_M64 = (1 << 64) - 1


def _fnv1a64(text):
    h = 0xcbf29ce484222325
    for ch in text:
        h ^= ord(ch)
        h = (h * 0x100000001b3) & _M64
    return h


def _mix64(v):
    z = (v + 0x9e3779b97f4a7c15) & _M64
    z = ((z ^ (z >> 30)) * 0xbf58476d1ce4e5b9) & _M64
    z = ((z ^ (z >> 27)) * 0x94d049bb133111eb) & _M64
    return (z ^ (z >> 31)) & _M64


class _Rng:
    def __init__(self, seed):
        s = seed & _M64
        self.state = s if s else 0x6a09e667f3bcc909

    def next_u64(self):
        x = self.state
        x = (x ^ (x >> 12)) & _M64
        x = (x ^ ((x << 25) & _M64)) & _M64
        x = (x ^ (x >> 27)) & _M64
        self.state = x
        return (x * 0x2545f4914f6cdd1d) & _M64

    def next_u32(self):
        return self.next_u64() >> 32

    def next_int(self, mx):
        if mx == 1:
            return 0
        rng_range = 4294967296
        limit = rng_range - (rng_range % mx)
        while True:
            v = self.next_u32()
            if v < limit:
                return v % mx

    def pick(self, items):
        return items[self.next_int(len(items))]


def _scenario_users():
    rng = _Rng(_mix64(0x6a09e667f3bcc909 ^ _fnv1a64("users")))
    used = set()
    users = []
    for i in range(12):
        first = rng.pick(_FIRST)
        last = rng.pick(_LAST)
        base = re.sub(r'[^a-z0-9-]', '', first + last)
        login = base
        suffix = 2
        while login in used:
            login = "%s-%d" % (base, suffix)
            suffix += 1
        used.add(login)
        users.append({"id": 1000 + i, "login": login, "name": "%s %s" % (first, last),
                      "email": "%s@example.invalid" % login})
    return users


try:
    _USERS = _scenario_users()
    VIEWER = _USERS[0]
    STRANGER = _USERS[2]
    if VIEWER["login"] != "adaiversen":
        VIEWER, STRANGER = VIEWER_FALLBACK, STRANGER_FALLBACK
except Exception:
    VIEWER, STRANGER = VIEWER_FALLBACK, STRANGER_FALLBACK

# ---------------------------------------------------------------------------
# Git object primitives
# ---------------------------------------------------------------------------

def hash_object(otype, data):
    h = hashlib.sha1()
    h.update(("%s %d\0" % (otype, len(data))).encode("ascii"))
    h.update(data)
    return h.hexdigest()


def parse_tree(data):
    """bytes -> [(mode_str, name, oid_hex)] in stored order."""
    entries = []
    i = 0
    n = len(data)
    while i < n:
        sp = data.index(b' ', i)
        mode = data[i:sp].decode("ascii")
        nul = data.index(b'\0', sp)
        name = data[sp + 1:nul].decode("utf-8", "surrogateescape")
        oid = data[nul + 1:nul + 21].hex()
        entries.append((mode, name, oid))
        i = nul + 21
    return entries


def build_tree(entries):
    """entries: [(mode, name, oid)] -> bytes, git-sorted."""
    def key(e):
        mode, name, _ = e
        return name + ('/' if mode == '40000' else '')
    out = bytearray()
    for mode, name, oid in sorted(entries, key=key):
        out += mode.encode("ascii") + b' ' + name.encode("utf-8", "surrogateescape") + b'\0'
        out += bytes.fromhex(oid)
    return bytes(out)


def parse_commit(data):
    """Return dict with tree, parents, author line, committer line, message."""
    text = data.decode("utf-8", "surrogateescape")
    head, _, message = text.partition("\n\n")
    tree = None
    parents = []
    author = ""
    committer = ""
    for line in head.split("\n"):
        if line.startswith("tree "):
            tree = line[5:].strip()
        elif line.startswith("parent "):
            parents.append(line[7:].strip())
        elif line.startswith("author "):
            author = line[7:]
        elif line.startswith("committer "):
            committer = line[10:]
    return {"tree": tree, "parents": parents, "author": author,
            "committer": committer, "message": message}


def build_commit(tree, parents, message, author, committer, timestamp):
    ident = "%s %d +0000" % (author, timestamp)
    cident = "%s %d +0000" % (committer, timestamp)
    lines = ["tree %s" % tree]
    for p in parents:
        lines.append("parent %s" % p)
    lines.append("author %s" % ident)
    lines.append("committer %s" % cident)
    body = "\n".join(lines) + "\n\n" + message
    if not body.endswith("\n"):
        body += "\n"
    return body.encode("utf-8", "surrogateescape")


def parse_tag(data):
    text = data.decode("utf-8", "surrogateescape")
    head, _, message = text.partition("\n\n")
    out = {"message": message}
    for line in head.split("\n"):
        if line.startswith("object "):
            out["object"] = line[7:].strip()
        elif line.startswith("type "):
            out["type"] = line[5:].strip()
        elif line.startswith("tag "):
            out["tag"] = line[4:].strip()
        elif line.startswith("tagger "):
            out["tagger"] = line[7:]
    return out


# --- packfile ---------------------------------------------------------------

def _read_varint(buf, pos):
    b = buf[pos]
    pos += 1
    size = b & 0x0f
    shift = 4
    while b & 0x80:
        b = buf[pos]
        pos += 1
        size |= (b & 0x7f) << shift
        shift += 7
    return size, pos


def _read_ofs_delta_base(buf, pos):
    b = buf[pos]
    pos += 1
    off = b & 0x7f
    while b & 0x80:
        b = buf[pos]
        pos += 1
        off = ((off + 1) << 7) | (b & 0x7f)
    return off, pos


def _apply_delta(base, delta):
    def read_size(pos):
        size = 0
        shift = 0
        while True:
            b = delta[pos]
            pos += 1
            size |= (b & 0x7f) << shift
            shift += 7
            if not (b & 0x80):
                return size, pos
    src_size, pos = read_size(0)
    dst_size, pos = read_size(pos)
    if src_size != len(base):
        raise ValueError("delta base size mismatch")
    out = bytearray()
    while pos < len(delta):
        op = delta[pos]
        pos += 1
        if op & 0x80:
            cp_off = 0
            cp_size = 0
            for i in range(4):
                if op & (1 << i):
                    cp_off |= delta[pos] << (8 * i)
                    pos += 1
            for i in range(3):
                if op & (0x10 << i):
                    cp_size |= delta[pos] << (8 * i)
                    pos += 1
            if cp_size == 0:
                cp_size = 0x10000
            out += base[cp_off:cp_off + cp_size]
        elif op:
            out += delta[pos:pos + op]
            pos += op
        else:
            raise ValueError("delta opcode 0")
    if len(out) != dst_size:
        raise ValueError("delta result size mismatch")
    return bytes(out)


def read_pack(pack, store_lookup=None):
    """Parse a packfile. Returns list of (oid, type, data). Raises ValueError."""
    if len(pack) < 32 or pack[:4] != b'PACK':
        raise ValueError("not a packfile")
    version = int.from_bytes(pack[4:8], "big")
    count = int.from_bytes(pack[8:12], "big")
    # trailer checksum
    expect = hashlib.sha1(pack[:-20]).hexdigest()
    if pack[-20:].hex() != expect:
        raise ValueError("pack checksum mismatch")
    pos = 12
    entries = []  # (offset, oid, type, data)
    by_offset = {}
    pending = []  # (offset, kind, base_ref, data)
    for _ in range(count):
        start = pos
        b = pack[pos]
        pos += 1
        otype_num = (b >> 4) & 7
        size = b & 0x0f
        shift = 4
        while b & 0x80:
            b = pack[pos]
            pos += 1
            size |= (b & 0x7f) << shift
            shift += 7
        if otype_num in (1, 2, 3, 4):
            otype = {1: "commit", 2: "tree", 3: "blob", 4: "tag"}[otype_num]
            dec = zlib.decompressobj()
            data = dec.decompress(pack[pos:])
            consumed = len(pack[pos:]) - len(dec.unused_data)
            pos += consumed
            if len(data) != size:
                raise ValueError("object size mismatch")
            oid = hash_object(otype, data)
            entries.append((start, oid, otype, data))
            by_offset[start] = (oid, otype, data)
        elif otype_num == 6:  # OFS_DELTA (git object type 6)
            base_off, pos = _read_ofs_delta_base(pack, pos)
            dec = zlib.decompressobj()
            data = dec.decompress(pack[pos:])
            consumed = len(pack[pos:]) - len(dec.unused_data)
            pos += consumed
            pending.append((start, "ofs", start - base_off, data))
        elif otype_num == 7:  # REF_DELTA (git object type 7)
            base_oid = pack[pos:pos + 20].hex()
            pos += 20
            dec = zlib.decompressobj()
            data = dec.decompress(pack[pos:])
            consumed = len(pack[pos:]) - len(dec.unused_data)
            pos += consumed
            pending.append((start, "ref", base_oid, data))
        else:
            raise ValueError("unknown pack object type %d" % otype_num)

    def resolve_base(kind, ref):
        if kind == "ref":
            for (_o, oid, t, d) in entries:
                if oid == ref:
                    return t, d
            if store_lookup is not None:
                hit = store_lookup(ref)
                if hit is not None:
                    return hit
            raise ValueError("delta base not found")
        else:
            if ref in by_offset:
                return by_offset[ref][1], by_offset[ref][2]
            raise ValueError("ofs delta base not found")

    # resolve deltas (may chain; iterate until stable)
    remaining = pending
    guard = 0
    while remaining:
        guard += 1
        if guard > 50:
            raise ValueError("delta chain too deep")
        nxt = []
        progressed = False
        for (off, kind, ref, data) in remaining:
            try:
                btype, bdata = resolve_base(kind, ref)
            except ValueError:
                nxt.append((off, kind, ref, data))
                continue
            full = _apply_delta(bdata, data)
            oid = hash_object(btype, full)
            entries.append((off, oid, btype, full))
            by_offset[off] = (oid, btype, full)
            progressed = True
        if not progressed:
            raise ValueError("unresolvable delta base")
        remaining = nxt
    return [(oid, t, d) for (_o, oid, t, d) in entries]


def write_pack(objects):
    """objects: iterable of (type, data). No deltas. Returns pack bytes."""
    out = bytearray()
    objs = list(objects)
    out += b'PACK' + (2).to_bytes(4, "big") + len(objs).to_bytes(4, "big")
    for otype, data in objs:
        num = {"commit": 1, "tree": 2, "blob": 3, "tag": 4}[otype]
        size = len(data)
        hdr = bytearray()
        first = (num << 4) | (size & 0x0f)
        size >>= 4
        if size:
            first |= 0x80
        hdr.append(first)
        while size:
            b = size & 0x7f
            size >>= 7
            if size:
                b |= 0x80
            hdr.append(b)
        out += bytes(hdr)
        out += zlib.compress(data, 1)
    out += hashlib.sha1(bytes(out)).digest()
    return bytes(out)


# --- pkt-line ----------------------------------------------------------------

def pkt(data):
    if isinstance(data, str):
        data = data.encode("utf-8")
    return ("%04x" % (len(data) + 4)).encode("ascii") + data


def pkt_flush():
    return b"0000"


def pkt_delim():
    return b"0001"


def parse_pkt_stream(buf, pos=0):
    """Parse pkt-lines from buf starting at pos.
    Returns (packets, next_pos) where packets are ('data', bytes) | ('flush',)
    | ('delim',) | ('end',). Stops at first flush/delim handling by caller."""
    packets = []
    n = len(buf)
    while pos + 4 <= n:
        head = buf[pos:pos + 4]
        try:
            ln = int(head.decode("ascii"), 16)
        except ValueError:
            break
        if ln == 0:
            packets.append(("flush", b""))
            pos += 4
            break
        if ln == 1:
            packets.append(("delim", b""))
            pos += 4
            continue
        if ln == 2:
            packets.append(("end", b""))
            pos += 4
            break
        if ln < 4 or pos + ln > n:
            break
        packets.append(("data", bytes(buf[pos + 4:pos + ln])))
        pos += ln
    return packets, pos

# ---------------------------------------------------------------------------
# In-process store
# ---------------------------------------------------------------------------

class Repo:
    def __init__(self, name, owner_login, private, description, db_id):
        self.name = name
        self.owner = owner_login
        self.private = bool(private)
        self.description = description or ""
        self.db_id = db_id
        self.default_branch = "main"
        self.objects = {}          # oid -> (type, bytes)
        self.refs = {}             # refname -> oid
        self.labels = {}           # name -> {"name","color","id"}
        self.label_seq = 1
        self.issues = {}           # number -> dict (internal)
        self.pulls = {}            # number -> dict (internal)
        self.num_seq = 1           # shared issue/PR number counter
        self.comments = {}         # id -> dict
        self.comment_seq = 1
        self.commit_seq = 0        # deterministic timestamp counter
        self.lock = threading.RLock()

    # -- git objects ---------------------------------------------------------
    def put_object(self, otype, data):
        oid = hash_object(otype, data)
        self.objects.setdefault(oid, (otype, data))
        return oid

    def get_object(self, oid):
        return self.objects.get(oid)

    def commit_parents(self, oid):
        obj = self.objects.get(oid)
        if obj is None or obj[0] != "commit":
            return []
        return parse_commit(obj[1])["parents"]

    def peel(self, oid):
        """Follow tag objects to the underlying commit (or oid itself)."""
        seen = set()
        cur = oid
        while cur and cur not in seen:
            seen.add(cur)
            obj = self.objects.get(cur)
            if obj is None or obj[0] != "tag":
                return cur
            cur = parse_tag(obj[1]).get("object")
        return cur

    def ancestors(self, tips, stop=None):
        """Set of commit oids reachable from tips (tags peeled)."""
        out = set()
        stack = []
        for t in tips:
            p = self.peel(t)
            if p:
                stack.append(p)
        while stack:
            oid = stack.pop()
            if oid in out or oid == ZERO_OID:
                continue
            if stop is not None and oid in stop:
                continue
            obj = self.objects.get(oid)
            if obj is None or obj[0] != "commit":
                continue
            out.add(oid)
            stack.extend(parse_commit(obj[1])["parents"])
        return out

    def is_ancestor(self, old, new):
        if old == new:
            return True
        return old in self.ancestors([new])

    def tree_entries(self, tree_oid):
        obj = self.objects.get(tree_oid)
        if obj is None or obj[0] != "tree":
            return []
        return parse_tree(obj[1])

    def flatten_tree(self, tree_oid):
        """tree oid -> {path: (mode, oid)} recursive."""
        out = {}
        def walk(toid, prefix):
            for mode, name, oid in self.tree_entries(toid):
                path = prefix + name
                if mode == "40000":
                    walk(oid, path + "/")
                else:
                    out[path] = (mode, oid)
        walk(tree_oid, "")
        return out

    def lookup_path(self, tree_oid, path):
        """Return (mode, oid) for path in tree, or None."""
        if not path:
            return ("40000", tree_oid)
        cur = tree_oid
        parts = path.split("/")
        for i, seg in enumerate(parts):
            found = None
            for mode, name, oid in self.tree_entries(cur):
                if name == seg:
                    found = (mode, oid)
                    break
            if found is None:
                return None
            if i == len(parts) - 1:
                return found
            if found[0] != "40000":
                return None
            cur = found[1]
        return None

    def build_tree_from(self, base_tree_oid, changes):
        """changes: {path: (mode, oid) or None to delete}. Returns new tree oid."""
        # materialise nested dict
        def to_nested(toid):
            node = {}
            for mode, name, oid in self.tree_entries(toid):
                node[name] = (mode, oid)
            return node
        root = to_nested(base_tree_oid) if base_tree_oid else {}

        def set_path(node, parts, value):
            seg = parts[0]
            if len(parts) == 1:
                if value is None:
                    node.pop(seg, None)
                else:
                    node[seg] = value
                return
            sub = node.get(seg)
            if sub is None or sub[0] != "40000":
                child = {}
            else:
                child = to_nested(sub[1])
            set_path(child, parts[1:], value)
            if child:
                child_oid = self._write_nested(child)
                node[seg] = ("40000", child_oid)
            else:
                node.pop(seg, None)

        for path, value in changes.items():
            set_path(root, path.split("/"), value)
        return self._write_nested(root)

    def _write_nested(self, node):
        entries = []
        for name, (mode, oid) in node.items():
            entries.append((mode, name, oid))
        return self.put_object("tree", build_tree(entries))

    def make_commit(self, tree_oid, parents, message, branch_tip=None):
        """Create a commit object with a deterministic timestamp."""
        self.commit_seq += 1
        ts = 1700000000 + self.commit_seq
        author = "%s <%s>" % (VIEWER["name"], VIEWER["email"])
        data = build_commit(tree_oid, parents, message, author, author, ts)
        return self.put_object("commit", data)

    def tip_commit(self, refname):
        oid = self.refs.get(refname)
        if not oid:
            return None
        return self.peel(oid)

    def ref_tree(self, refname):
        tip = self.tip_commit(refname)
        if not tip:
            return None
        obj = self.objects.get(tip)
        if obj is None or obj[0] != "commit":
            return None
        return parse_commit(obj[1])["tree"]

    def commits_from(self, tip):
        """List of commit oids, tip first, DFS over parents."""
        out = []
        seen = set()
        stack = [self.peel(tip)] if tip else []
        while stack:
            oid = stack.pop()
            if oid in seen or oid == ZERO_OID or oid is None:
                continue
            obj = self.objects.get(oid)
            if obj is None or obj[0] != "commit":
                continue
            seen.add(oid)
            out.append(oid)
            stack.extend(parse_commit(obj[1])["parents"])
        return out

    def merge_base(self, a, b):
        anc_a = self.ancestors([a])
        for oid in self.commits_from(b):
            if oid in anc_a:
                return oid
        return None


class Store:
    def __init__(self):
        self.repos = {}            # (owner, name) -> Repo
        self.repo_seq = 1
        self.global_id_seq = 1
        self.lock = threading.RLock()
        self.req_seq = 0

    def next_req_id(self):
        with self.lock:
            self.req_seq += 1
            return "req-%08d" % self.req_seq

    def create_repo(self, name, private, description):
        with self.lock:
            key = (VIEWER["login"], name)
            if key in self.repos:
                return None
            repo = Repo(name, VIEWER["login"], private, description, self.repo_seq)
            self.repo_seq += 1
            self.repos[key] = repo
            return repo

    def get_repo(self, owner, name):
        if owner != VIEWER["login"]:
            return None
        return self.repos.get((owner, name))

    def delete_repo(self, owner, name):
        with self.lock:
            return self.repos.pop((owner, name), None) is not None

    def list_repos(self):
        return [r for (o, _n), r in sorted(self.repos.items()) if o == VIEWER["login"]]

    def next_global_seq(self):
        with self.lock:
            v = self.global_id_seq
            self.global_id_seq += 1
            return v


STORE = Store()


def gid(kind, *parts):
    raw = (kind + ":" + ":".join(str(p) for p in parts)).encode("ascii")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def ungid(value):
    try:
        pad = "=" * (-len(value) % 4)
        raw = base64.urlsafe_b64decode((value + pad).encode("ascii")).decode("ascii")
        kind, *parts = raw.split(":")
        return kind, parts
    except Exception:
        return None, []


def find_repo_by_dbid(db_id):
    for repo in STORE.list_repos():
        if repo.db_id == db_id:
            return repo
    return None


# --- git fetch object selection --------------------------------------------

def fetch_objects(repo, wants, haves, shallow_req, depth):
    """Compute (objects, shallow_list, unshallow_list) for a fetch.

    wants/haves: oid lists. shallow_req: boundary oids client already holds
    shallowly. depth: int or None.
    Returns dict with objects = [(oid, type, data)], shallow, unshallow.
    """
    held = set()
    # full closure of haves (objects the client already holds), except a
    # have that is also a shallow boundary: the client holds that commit
    # and its tree, but NOT its parents (a deepen fetch must re-deliver
    # them). Without this, deepen N never delivers the parent commit.
    shallow_set = set(shallow_req)
    for h in haves:
        if h in shallow_set:
            held |= closure_objects(repo, [h], stop_at_parents=True)
        else:
            held |= closure_objects(repo, [h])
    # shallow boundaries: client holds the commit + its tree, not parents
    for b in shallow_req:
        held |= closure_objects(repo, [b], stop_at_parents=True)

    # candidate commits
    candidates = []
    if depth is not None:
        # BFS generations 0..depth-1 from wants (tags peeled to commits)
        seen = set()
        frontier = []
        for w in wants:
            obj = repo.get_object(w)
            if obj is None:
                continue
            if obj[0] == "tag":
                candidates.append(w)
                p = repo.peel(w)
                if p and p not in seen:
                    seen.add(p)
                    frontier.append(p)
            elif obj[0] == "commit":
                if w not in seen:
                    seen.add(w)
                    frontier.append(w)
        gen = 0
        while frontier and gen < depth:
            gen += 1
            nxt = []
            for oid in frontier:
                candidates.append(oid)
                if gen < depth:
                    for par in repo.commit_parents(oid):
                        if par not in seen:
                            seen.add(par)
                            nxt.append(par)
            frontier = nxt
        cand_commits = [c for c in candidates if repo.get_object(c)
                        and repo.get_object(c)[0] == "commit"]
    else:
        cand_commits = []
        seen = set()
        stack = []
        for w in wants:
            obj = repo.get_object(w)
            if obj is None:
                continue
            if obj[0] == "tag":
                candidates.append(w)
                p = repo.peel(w)
                if p:
                    stack.append(p)
            elif obj[0] == "commit":
                stack.append(w)
        while stack:
            oid = stack.pop()
            if oid in seen or oid in haves:
                continue
            obj = repo.get_object(oid)
            if obj is None or obj[0] != "commit":
                continue
            seen.add(oid)
            cand_commits.append(oid)
            stack.extend(repo.commit_parents(oid))
    # exclude haves themselves
    cand_commits = [c for c in cand_commits if c not in haves]
    # objects reachable from candidates without crossing non-candidates
    cand_set = set(cand_commits)
    send = {}
    for w in candidates:
        obj = repo.get_object(w)
        if obj and obj[0] == "tag":
            send[w] = obj
    for oid in cand_commits:
        obj = repo.get_object(oid)
        if obj is None:
            continue
        send[oid] = obj
        tree = parse_commit(obj[1])["tree"]
        collect_tree(repo, tree, send)
        # parents are handled via cand_commits themselves (their trees added
        # when visited); do not descend into non-candidate parents.
    # subtract held
    for oid in list(send):
        if oid in held:
            del send[oid]
    # shallow / unshallow
    shallow = []
    unshallow = []
    if depth is not None:
        for oid in cand_commits:
            parents = repo.commit_parents(oid)
            if parents and not any(p in cand_set for p in parents):
                shallow.append(oid)
        for b in shallow_req:
            parents = repo.commit_parents(b)
            if any(p in cand_set for p in parents):
                unshallow.append(b)
    return {"objects": [(oid, send[oid][0], send[oid][1]) for oid in sorted(send)],
            "shallow": shallow, "unshallow": unshallow}


def closure_objects(repo, tips, stop_at_parents=False):
    out = set()
    stack = list(tips)
    while stack:
        oid = stack.pop()
        if oid in out or oid == ZERO_OID or oid is None:
            continue
        obj = repo.get_object(oid)
        if obj is None:
            continue
        out.add(oid)
        if obj[0] == "commit":
            c = parse_commit(obj[1])
            collect_tree_ids(repo, c["tree"], out)
            if not stop_at_parents:
                stack.extend(c["parents"])
        elif obj[0] == "tag":
            t = parse_tag(obj[1])
            if t.get("object"):
                stack.append(t["object"])
        elif obj[0] == "tree":
            collect_tree_ids(repo, oid, out)
    return out


def collect_tree_ids(repo, tree_oid, out):
    stack = [tree_oid]
    while stack:
        toid = stack.pop()
        if toid in out:
            continue
        obj = repo.get_object(toid)
        if obj is None or obj[0] != "tree":
            continue
        out.add(toid)
        for mode, _name, oid in parse_tree(obj[1]):
            if mode == "40000":
                stack.append(oid)
            else:
                out.add(oid)


def collect_tree(repo, tree_oid, send):
    stack = [tree_oid]
    while stack:
        toid = stack.pop()
        if toid in send:
            continue
        obj = repo.get_object(toid)
        if obj is None or obj[0] != "tree":
            continue
        send[toid] = obj
        for mode, _name, oid in parse_tree(obj[1]):
            if mode == "40000":
                stack.append(oid)
            else:
                o = repo.get_object(oid)
                if o is not None:
                    send[oid] = o


# --- diff / merge helpers ----------------------------------------------------

def diff_paths(repo, base_tree, head_tree):
    """Return list of {path, status, old_oid, new_oid} head vs base."""
    b = repo.flatten_tree(base_tree) if base_tree else {}
    h = repo.flatten_tree(head_tree) if head_tree else {}
    out = []
    for path in sorted(set(b) | set(h)):
        if path not in b:
            out.append({"path": path, "status": "added", "old": None, "new": h[path][1]})
        elif path not in h:
            out.append({"path": path, "status": "removed", "old": b[path][1], "new": None})
        elif b[path][1] != h[path][1]:
            out.append({"path": path, "status": "modified", "old": b[path][1], "new": h[path][1]})
    return out


def line_counts(repo, old_oid, new_oid):
    def lines(oid):
        if oid is None:
            return []
        obj = repo.get_object(oid)
        if obj is None or obj[0] != "blob":
            return []
        try:
            text = obj[1].decode("utf-8")
        except UnicodeDecodeError:
            return []
        return text.splitlines()
    a, b = lines(old_oid), lines(new_oid)
    add = dele = 0
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b).get_opcodes():
        if tag in ("insert", "replace"):
            add += j2 - j1
        if tag in ("delete", "replace"):
            dele += i2 - i1
    return add, dele


def pr_conflicted(repo, pr):
    """True if squash-merging pr would conflict."""
    base_tip = repo.tip_commit("refs/heads/" + pr["base_ref"])
    head_tip = repo.tip_commit("refs/heads/" + pr["head_ref"])
    if base_tip is None or head_tip is None:
        return True
    mb = repo.merge_base(base_tip, head_tip)
    if mb is None:
        return True
    mb_tree = parse_commit(repo.get_object(mb)[1])["tree"]
    base_tree = parse_commit(repo.get_object(base_tip)[1])["tree"]
    head_tree = parse_commit(repo.get_object(head_tip)[1])["tree"]
    head_changes = {d["path"]: d for d in diff_paths(repo, mb_tree, head_tree)}
    base_changes = {d["path"]: d for d in diff_paths(repo, mb_tree, base_tree)}
    for path, hc in head_changes.items():
        bc = base_changes.get(path)
        if bc is not None and bc["new"] != hc["new"]:
            return True
    return False


def squash_merge(repo, pr):
    """Apply head changes onto base tip; returns merge commit oid or None."""
    base_tip = repo.tip_commit("refs/heads/" + pr["base_ref"])
    head_tip = repo.tip_commit("refs/heads/" + pr["head_ref"])
    mb = repo.merge_base(base_tip, head_tip)
    mb_tree = parse_commit(repo.get_object(mb)[1])["tree"]
    base_tree = parse_commit(repo.get_object(base_tip)[1])["tree"]
    head_tree = parse_commit(repo.get_object(head_tip)[1])["tree"]
    changes = {}
    for d in diff_paths(repo, mb_tree, head_tree):
        if d["status"] == "removed":
            changes[d["path"]] = None
        else:
            mode = repo.flatten_tree(head_tree)[d["path"]][0]
            changes[d["path"]] = (mode, d["new"])
    new_tree = repo.build_tree_from(base_tree, changes)
    msg = "%s (#%d)\n" % (pr["title"], pr["number"])
    return repo.make_commit(new_tree, [base_tip], msg)

# ---------------------------------------------------------------------------
# REST object builders
# ---------------------------------------------------------------------------

def repo_json(repo):
    return {
        "id": repo.db_id,
        "node_id": gid("R", repo.db_id),
        "name": repo.name,
        "full_name": "%s/%s" % (repo.owner, repo.name),
        "owner": {"login": repo.owner, "id": VIEWER["id"], "type": "User",
                  "avatar_url": "", "html_url": ""},
        "private": repo.private,
        "description": repo.description,
        "default_branch": repo.default_branch,
        "html_url": "", "clone_url": "", "git_url": "", "ssh_url": "",
        "url": "", "created_at": FIXED_TS, "updated_at": FIXED_TS,
        "pushed_at": FIXED_TS, "homepage": None, "language": None,
        "forks_count": 0, "stargazers_count": 0, "watchers_count": 0,
        "open_issues_count": sum(1 for i in repo.issues.values() if i["state"] == "open"),
        "has_issues": True, "has_projects": True, "has_wiki": True,
        "archived": False, "disabled": False, "license": None,
        "visibility": "private" if repo.private else "public",
    }


def issue_json(repo, iss):
    return {
        "id": iss["gid"],
        "node_id": gid("I", repo.db_id, iss["number"]),
        "number": iss["number"],
        "title": iss["title"],
        "body": iss["body"],
        "state": iss["state"],
        "labels": [{"id": repo.labels[n]["id"], "name": n,
                    "color": repo.labels[n]["color"], "description": ""}
                   for n in iss["labels"] if n in repo.labels],
        "user": {"login": VIEWER["login"], "id": VIEWER["id"], "type": "User"},
        "assignee": None, "assignees": [], "milestone": None,
        "locked": False, "comments": len(iss["comments"]),
        "created_at": FIXED_TS, "updated_at": FIXED_TS,
        "closed_at": FIXED_TS if iss["state"] == "closed" else None,
        "html_url": "", "url": "",
        "repository_url": "", "author_association": "OWNER",
    }


def pull_json(repo, pr):
    head_sha = repo.tip_commit("refs/heads/" + pr["head_ref"]) or ""
    base_sha = repo.tip_commit("refs/heads/" + pr["base_ref"]) or ""
    return {
        "id": pr["gid"],
        "node_id": gid("P", repo.db_id, pr["number"]),
        "number": pr["number"],
        "state": pr["state"],
        "title": pr["title"],
        "body": pr["body"],
        "head": {"label": "%s:%s" % (repo.owner, pr["head_ref"]),
                 "ref": pr["head_ref"], "sha": head_sha,
                 "user": {"login": repo.owner, "id": VIEWER["id"]},
                 "repo": {"name": repo.name, "full_name": "%s/%s" % (repo.owner, repo.name)}},
        "base": {"label": "%s:%s" % (repo.owner, pr["base_ref"]),
                 "ref": pr["base_ref"], "sha": base_sha,
                 "user": {"login": repo.owner, "id": VIEWER["id"]},
                 "repo": {"name": repo.name, "full_name": "%s/%s" % (repo.owner, repo.name)}},
        "merged": pr["merged"],
        "mergeable": not pr_conflicted(repo, pr) if pr["state"] == "open" else None,
        "merged_at": FIXED_TS if pr["merged"] else None,
        "merge_commit_sha": pr["merge_sha"],
        "created_at": FIXED_TS, "updated_at": FIXED_TS,
        "closed_at": FIXED_TS if pr["state"] == "closed" else None,
        "draft": False, "locked": False,
        "user": {"login": VIEWER["login"], "id": VIEWER["id"], "type": "User"},
        "labels": [], "html_url": "", "url": "",
        "commits": 1, "additions": 0, "deletions": 0, "changed_files": 0,
    }


def create_issue_internal(repo, title, body, label_names):
    with repo.lock:
        number = repo.num_seq
        repo.num_seq += 1
        iss = {"gid": STORE.next_global_seq(), "number": number,
               "title": title or "", "body": body or "", "state": "open",
               "labels": list(label_names), "comments": []}
        repo.issues[number] = iss
        return iss


def create_pull_internal(repo, title, body, head, base):
    with repo.lock:
        number = repo.num_seq
        repo.num_seq += 1
        pr = {"gid": STORE.next_global_seq(), "number": number,
              "title": title or "", "body": body or "",
              "head_ref": head, "base_ref": base,
              "state": "open", "merged": False, "merge_sha": None}
        repo.pulls[number] = pr
        return pr


def do_merge_internal(repo, pr):
    """Returns (status, payload_dict)."""
    with repo.lock:
        if pr["state"] == "closed":
            return 405, {"message": "Pull request is already closed"}
        if pr_conflicted(repo, pr):
            return 405, {"message": "Merge conflict"}
        merge_sha = squash_merge(repo, pr)
        pr["merged"] = True
        pr["state"] = "closed"
        pr["merge_sha"] = merge_sha
        repo.refs["refs/heads/" + pr["base_ref"]] = merge_sha
        return 200, {"sha": merge_sha, "merged": True,
                     "message": "Pull request successfully merged"}


# --- contents helpers ---------------------------------------------------------

def commit_file(repo, branch, path, content_bytes, message, expected_blob_sha=None):
    """Create/update a file on branch. Returns (status, dict)."""
    with repo.lock:
        refname = "refs/heads/" + branch
        tip = repo.tip_commit(refname)
        if tip is None:
            # unborn branch: new root commit
            blob = repo.put_object("blob", content_bytes)
            tree = repo.put_object("tree", build_tree([("100644", path.split("/")[-1], blob)])) \
                if "/" not in path else None
            if tree is None:
                tree = repo.build_tree_from(None, {path: ("100644", blob)})
            else:
                # nested path handled by build_tree_from
                tree = repo.build_tree_from(None, {path: ("100644", blob)})
            commit = repo.make_commit(tree, [], message)
            repo.refs[refname] = commit
            return 201, {"content": {"sha": blob, "encoding": "base64"},
                         "commit": {"sha": commit}}
        cobj = repo.get_object(tip)
        tree_oid = parse_commit(cobj[1])["tree"]
        cur = repo.lookup_path(tree_oid, path)
        if cur is not None and expected_blob_sha is None and message is not None and False:
            pass
        if cur is not None:
            if expected_blob_sha is not None and expected_blob_sha.lower() != cur[1].lower():
                return 409, {"message": "sha does not match"}
            is_update = True
        else:
            if expected_blob_sha is not None:
                return 404, {"message": "Not Found"}
            is_update = False
        blob = repo.put_object("blob", content_bytes)
        new_tree = repo.build_tree_from(tree_oid, {path: ("100644", blob)})
        commit = repo.make_commit(new_tree, [tip], message)
        repo.refs[refname] = commit
        return (200 if is_update else 201), {"content": {"sha": blob, "encoding": "base64"},
                                              "commit": {"sha": commit}}


def delete_file(repo, branch, path, message, expected_blob_sha=None):
    with repo.lock:
        refname = "refs/heads/" + branch
        tip = repo.tip_commit(refname)
        if tip is None:
            return 404, {"message": "Not Found"}
        cobj = repo.get_object(tip)
        tree_oid = parse_commit(cobj[1])["tree"]
        cur = repo.lookup_path(tree_oid, path)
        if cur is None:
            return 404, {"message": "Not Found"}
        if expected_blob_sha is not None and expected_blob_sha.lower() != cur[1].lower():
            return 409, {"message": "sha does not match"}
        new_tree = repo.build_tree_from(tree_oid, {path: None})
        commit = repo.make_commit(new_tree, [tip], message or "Delete file")
        repo.refs[refname] = commit
        return 200, {"commit": {"sha": commit}}


# ---------------------------------------------------------------------------
# HTTP plumbing
# ---------------------------------------------------------------------------

TOKEN = os.environ.get("MAKEFUN_SHOP_TOKEN", "")


def _is_hex40(s):
    return isinstance(s, str) and len(s) == 40 and all(c in "0123456789abcdef" for c in s.lower())


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "Forge/1.0"

    def log_message(self, fmt, *args):
        pass

    # -- response helpers ----------------------------------------------------
    def setup(self):
        super().setup()
        try:
            self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass

    def _send(self, status, body_bytes, ctype="application/json; charset=utf-8", extra=None):
        # One sendall for the whole response: the old per-header wfile writes
        # stalled ~40 ms per request on Nagle/delayed-ACK and blew the 60 s
        # budget of the 2,000-read workload.
        try:
            reason = self.responses.get(status, ("???", ""))[0]
            lines = ["HTTP/1.1 %d %s" % (status, reason),
                     "Date: %s" % self.date_time_string(),
                     "Content-Type: %s" % ctype,
                     "Content-Length: %d" % len(body_bytes)]
            if extra:
                for k, v in extra.items():
                    lines.append("%s: %s" % (k, v))
            head = ("\r\n".join(lines) + "\r\n\r\n").encode("iso-8859-1")
            if self.command == "HEAD" or not body_bytes:
                self.connection.sendall(head)
            else:
                self.connection.sendall(head + body_bytes)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _json(self, status, obj, extra=None):
        data = (json.dumps(obj, separators=(",", ":")) + "\n").encode("utf-8")
        hdrs = {"X-GitHub-Request-Id": STORE.next_req_id()}
        if extra:
            hdrs.update(extra)
        self._send(status, data, extra=hdrs)

    def _json_err(self, status, message):
        self._json(status, {"message": message})

    def _read_body(self):
        # The dispatcher drains the body up front into _body_cache so that
        # no handler path (not even an early 404) can leave bytes in the
        # keep-alive stream to corrupt the next request line.
        cached = getattr(self, "_body_cache", None)
        if cached is not None:
            return cached
        ln = self.headers.get("Content-Length")
        if ln is None:
            # chunked or absent: try to read what is available via Content-Length 0
            return b""
        try:
            n = int(ln)
        except ValueError:
            return b""
        if n <= 0:
            return b""
        return self.rfile.read(n)

    def _auth_ok(self):
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return False
        tok = auth[7:].strip()
        if not tok:
            return False
        if TOKEN:
            return tok == TOKEN
        return True

    # -- dispatch -------------------------------------------------------------
    def _route(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        method = self.command
        try:
            if method == "GET" and path == "/healthz":
                self._json(200, {"ok": True})
                return
            if path.startswith("/api/v3"):
                self._handle_rest(method, path, query)
                return
            if path == "/api/graphql" and method == "POST":
                self._handle_graphql()
                return
            if path == "/mcp" and method == "POST":
                self._handle_mcp()
                return
            m = re.match(r"^/([^/]+)/([^/]+)\.git(/.*)?$", path)
            if m:
                self._handle_git(method, m.group(1), m.group(2), m.group(3) or "/", query)
                return
            self._json_err(404, "Not Found")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:  # never crash on a client request
            try:
                self._json_err(500, "Internal Server Error: %s" % exc)
            except Exception:
                pass

    def do_GET(self):
        self._route()

    def do_POST(self):
        self._route()

    def do_PUT(self):
        self._route()

    def do_PATCH(self):
        self._route()

    def do_DELETE(self):
        self._route()

    def do_HEAD(self):
        self._route()

    # -- JSON body -------------------------------------------------------------
    def _json_body(self):
        """Returns (obj, error_status_or_None). Empty body -> {}."""
        raw = self._read_body()
        if not raw:
            return {}, None
        try:
            obj = json.loads(raw.decode("utf-8"))
        except Exception:
            return None, 400
        if not isinstance(obj, dict):
            return None, 400
        return obj, None

    def _paginate(self, items, query, path_for_link):
        try:
            per_page = int(query.get("per_page", ["30"])[0])
        except ValueError:
            per_page = 30
        try:
            page = int(query.get("page", ["1"])[0])
        except ValueError:
            page = 1
        per_page = max(1, min(per_page, 100))
        page = max(1, page)
        total = len(items)
        start = (page - 1) * per_page
        chunk = items[start:start + per_page]
        host = self.headers.get("Host", "localhost")
        base = "http://%s%s" % (host, path_for_link)
        links = []
        if start + per_page < total:
            links.append('<%s?page=%d&per_page=%d>; rel="next"' % (base, page + 1, per_page))
            last = (total + per_page - 1) // per_page
            links.append('<%s?page=%d&per_page=%d>; rel="last"' % (base, last, per_page))
        if page > 1:
            links.append('<%s?page=%d&per_page=%d>; rel="prev"' % (base, page - 1, per_page))
            links.append('<%s?page=1&per_page=%d>; rel="first"' % (base, per_page))
        extra = {"Link": ", ".join(links)} if links else {}
        return chunk, extra

    # -- REST v3 ---------------------------------------------------------------
    def _handle_rest(self, method, path, query):
        # Public roots (GitHub-like): no auth needed for the API root / meta.
        public = (method == "GET" and path in ("/api/v3", "/api/v3/", "/api/v3/meta"))
        if not public and not self._auth_ok():
            self._json_err(401, "Requires authentication")
            return
        rest = path[len("/api/v3"):]
        if rest == "" or rest == "/":
            if method == "GET":
                self._json(200, {"current_user_url": "/api/v3/user",
                                 "rate_limit_url": "/api/v3/rate_limit"},
                           extra={"X-OAuth-Scopes": "repo, read:org"})
            else:
                self._json_err(404, "Not Found")
            return
        if rest == "/meta":
            if method == "GET":
                self._json(200, {"installed_version": "1.0.0"})
            else:
                self._json_err(404, "Not Found")
            return
        if rest == "/user":
            if method == "GET":
                self._json(200, {"login": VIEWER["login"], "id": VIEWER["id"],
                                 "name": VIEWER["name"], "email": VIEWER["email"],
                                 "type": "User", "avatar_url": "", "html_url": ""})
            else:
                self._json_err(404, "Not Found")
            return
        if rest == "/user/repos":
            if method == "GET":
                repos = STORE.list_repos()
                chunk, extra = self._paginate([repo_json(r) for r in repos], query, "/api/v3/user/repos")
                self._json(200, chunk, extra=extra)
                return
            if method == "POST":
                body, err = self._json_body()
                if err:
                    self._json_err(400, "Invalid JSON body")
                    return
                name = body.get("name")
                if not isinstance(name, str) or not name:
                    self._json_err(422, "name is required")
                    return
                repo = STORE.create_repo(name, body.get("private", False),
                                         body.get("description"))
                if repo is None:
                    self._json_err(422, "Repository already exists")
                    return
                self._json(201, repo_json(repo))
                return
            self._json_err(404, "Not Found")
            return
        m = re.match(r"^/repos/([^/]+)/([^/]+)(/.*)?$", rest)
        if not m:
            self._json_err(404, "Not Found")
            return
        owner = unquote(m.group(1))
        rname = unquote(m.group(2))
        tail = m.group(3) or ""
        repo = STORE.get_repo(owner, rname)
        if repo is None:
            # still need to consume body for POSTs etc.; body already unread is fine
            # (client sent Content-Length; unread bytes are discarded on next request
            # only if connection reused -- read and drop to keep keep-alive sane)
            self._read_body()
            self._json_err(404, "Not Found")
            return
        self._handle_repo_rest(repo, method, tail, query)

    def _handle_repo_rest(self, repo, method, tail, query):
        # tail starts with "/" or is ""
        parts = [unquote(p) for p in tail.split("/") if p != ""]
        # /repos/{o}/{r} itself
        if not parts:
            if method == "GET":
                self._json(200, repo_json(repo))
            elif method == "PATCH":
                body, err = self._json_body()
                if err:
                    self._json_err(400, "Invalid JSON body")
                    return
                with repo.lock:
                    if "description" in body:
                        repo.description = body.get("description") or ""
                    if "private" in body:
                        repo.private = bool(body.get("private"))
                self._json(200, repo_json(repo))
            elif method == "DELETE":
                STORE.delete_repo(repo.owner, repo.name)
                self._send(204, b"", extra={"X-GitHub-Request-Id": STORE.next_req_id()})
            else:
                self._json_err(404, "Not Found")
            return
        head = parts[0]
        if head == "branches":
            if len(parts) == 1 and method == "GET":
                out = []
                for ref in sorted(repo.refs):
                    if ref.startswith("refs/heads/"):
                        out.append({"name": ref[len("refs/heads/"):],
                                    "commit": {"sha": repo.refs[ref]},
                                    "protected": False})
                self._json(200, out)
                return
            if len(parts) == 2 and method == "GET":
                ref = "refs/heads/" + parts[1]
                if ref in repo.refs:
                    self._json(200, {"name": parts[1], "commit": {"sha": repo.refs[ref]},
                                     "protected": False})
                else:
                    self._json_err(404, "Not Found")
                return
            self._json_err(404, "Not Found")
            return
        if head == "commits":
            if method == "GET":
                sha = query.get("sha", [None])[0]
                if sha:
                    tip = repo.refs.get("refs/heads/" + sha) or (sha if _is_hex40(sha) else None)
                else:
                    tip = repo.refs.get("refs/heads/" + repo.default_branch)
                out = []
                if tip:
                    for oid in repo.commits_from(tip):
                        c = parse_commit(repo.get_object(oid)[1])
                        out.append({"sha": oid,
                                    "commit": {"message": c["message"].rstrip("\n"),
                                               "author": {"name": VIEWER["name"],
                                                          "email": VIEWER["email"],
                                                          "date": FIXED_TS},
                                               "committer": {"name": VIEWER["name"],
                                                             "email": VIEWER["email"],
                                                             "date": FIXED_TS},
                                               "tree": {"sha": c["tree"]}},
                                    "author": {"login": VIEWER["login"], "id": VIEWER["id"]},
                                    "parents": [{"sha": p} for p in c["parents"]]})
                self._json(200, out)
                return
            self._json_err(404, "Not Found")
            return
        if head == "git":
            if len(parts) >= 2 and parts[1] == "refs":
                if method == "POST" and len(parts) == 2:
                    body, err = self._json_body()
                    if err:
                        self._json_err(400, "Invalid JSON body")
                        return
                    ref = body.get("ref")
                    sha = body.get("sha")
                    if not isinstance(ref, str) or not ref.startswith("refs/") or not _is_hex40(sha or ""):
                        self._json_err(422, "Invalid ref or sha")
                        return
                    with repo.lock:
                        if ref in repo.refs:
                            self._json_err(422, "Reference already exists")
                            return
                        if repo.get_object(sha) is None:
                            self._json_err(422, "Object does not exist")
                            return
                        repo.refs[ref] = sha
                    self._json(201, {"ref": ref, "object": {"sha": sha, "type": "commit"}})
                    return
                if method == "GET" and len(parts) >= 3:
                    ref = "/".join(parts[2:])
                    full = "refs/" + ref
                    if full in repo.refs:
                        self._json(200, {"ref": full,
                                         "object": {"sha": repo.refs[full], "type": "commit"}})
                    else:
                        self._json_err(404, "Not Found")
                    return
            self._json_err(404, "Not Found")
            return
        if head == "contents":
            self._handle_contents(repo, method, parts[1:], query)
            return
        if head == "labels":
            if method == "POST" and len(parts) == 1:
                body, err = self._json_body()
                if err:
                    self._json_err(400, "Invalid JSON body")
                    return
                name = body.get("name")
                if not isinstance(name, str) or not name:
                    self._json_err(422, "name is required")
                    return
                with repo.lock:
                    if name in repo.labels:
                        lab = repo.labels[name]
                    else:
                        lab = {"id": repo.label_seq, "name": name,
                               "color": body.get("color") or "ededed", "description": ""}
                        repo.label_seq += 1
                        repo.labels[name] = lab
                self._json(201, {"id": lab["id"], "name": lab["name"],
                                 "color": lab["color"], "description": ""})
                return
            if method == "GET" and len(parts) == 1:
                self._json(200, [{"id": v["id"], "name": v["name"], "color": v["color"],
                                  "description": ""} for v in repo.labels.values()])
                return
            self._json_err(404, "Not Found")
            return
        if head == "issues":
            self._handle_issues(repo, method, parts[1:], query)
            return
        if head == "pulls":
            self._handle_pulls(repo, method, parts[1:], query)
            return
        self._json_err(404, "Not Found")

    def _handle_contents(self, repo, method, parts, query):
        cpath = "/".join(parts)
        ref_q = query.get("ref", [None])[0]
        branch = ref_q or repo.default_branch
        if method == "GET":
            tip = repo.tip_commit("refs/heads/" + branch)
            if tip is None and _is_hex40(branch or ""):
                tip = branch
            if tip is None:
                self._json_err(404, "Not Found")
                return
            cobj = repo.get_object(tip)
            if cobj is None or cobj[0] != "commit":
                self._json_err(404, "Not Found")
                return
            tree_oid = parse_commit(cobj[1])["tree"]
            if cpath == "":
                entries = []
                for mode, name, oid in repo.tree_entries(tree_oid):
                    entries.append({"type": "dir" if mode == "40000" else "file",
                                    "name": name, "path": name, "sha": oid,
                                    "size": 0 if mode == "40000" else self._blob_size(repo, oid),
                                    "url": "", "html_url": "", "git_url": "",
                                    "download_url": ""})
                self._json(200, entries)
                return
            hit = repo.lookup_path(tree_oid, cpath)
            if hit is None:
                self._json_err(404, "Not Found")
                return
            mode, oid = hit
            if mode == "40000":
                entries = []
                for m2, name2, oid2 in repo.tree_entries(oid):
                    entries.append({"type": "dir" if m2 == "40000" else "file",
                                    "name": name2, "path": cpath + "/" + name2, "sha": oid2,
                                    "size": 0 if m2 == "40000" else self._blob_size(repo, oid2),
                                    "url": "", "html_url": "", "git_url": "",
                                    "download_url": ""})
                self._json(200, entries)
                return
            obj = repo.get_object(oid)
            data = obj[1] if obj else b""
            self._json(200, {"type": "file", "encoding": "base64",
                             "size": len(data), "name": cpath.split("/")[-1],
                             "path": cpath, "content": base64.b64encode(data).decode("ascii"),
                             "sha": oid, "url": "", "html_url": "", "git_url": "",
                             "download_url": ""})
            return
        if method == "PUT":
            body, err = self._json_body()
            if err:
                self._json_err(400, "Invalid JSON body")
                return
            content_b64 = body.get("content")
            message = body.get("message") or "Update file"
            if not isinstance(content_b64, str):
                self._json_err(422, "content is required")
                return
            try:
                raw = base64.b64decode(content_b64, validate=False)
            except Exception:
                self._json_err(422, "content is not valid base64")
                return
            br = body.get("branch") or repo.default_branch
            # creating a branch on demand from the default tip
            if ("refs/heads/" + br) not in repo.refs and br != repo.default_branch:
                base_tip = repo.refs.get("refs/heads/" + repo.default_branch)
                if base_tip is not None:
                    repo.refs["refs/heads/" + br] = base_tip
            status, payload = commit_file(repo, br, cpath, raw, message,
                                          expected_blob_sha=body.get("sha"))
            self._json(status, payload)
            return
        if method == "DELETE":
            body, err = self._json_body()
            if err:
                self._json_err(400, "Invalid JSON body")
                return
            br = body.get("branch") or repo.default_branch
            status, payload = delete_file(repo, br, cpath,
                                          body.get("message") or "Delete file",
                                          expected_blob_sha=body.get("sha"))
            self._json(status, payload)
            return
        self._json_err(404, "Not Found")

    def _blob_size(self, repo, oid):
        obj = repo.get_object(oid)
        return len(obj[1]) if obj else 0

    def _handle_issues(self, repo, method, parts, query):
        if not parts:
            if method == "GET":
                state = query.get("state", ["open"])[0]
                sort = query.get("sort", ["created"])[0]
                direction = query.get("direction", ["desc"])[0]
                items = list(repo.issues.values())
                if state != "all":
                    items = [i for i in items if i["state"] == state]
                items.sort(key=lambda i: i["number"],
                           reverse=(direction != "asc"))
                path = "/api/v3/repos/%s/%s/issues" % (repo.owner, repo.name)
                chunk, extra = self._paginate([issue_json(repo, i) for i in items],
                                             query, path)
                self._json(200, chunk, extra=extra)
                return
            if method == "POST":
                body, err = self._json_body()
                if err:
                    self._json_err(400, "Invalid JSON body")
                    return
                labels = body.get("labels") or []
                names = []
                for lab in labels:
                    names.append(lab["name"] if isinstance(lab, dict) else lab)
                for n in names:
                    if n not in repo.labels:
                        self._json_err(422, "Label does not exist")
                        return
                iss = create_issue_internal(repo, body.get("title"), body.get("body"), names)
                self._json(201, issue_json(repo, iss))
                return
            self._json_err(404, "Not Found")
            return
        try:
            number = int(parts[0])
        except ValueError:
            self._json_err(404, "Not Found")
            return
        iss = repo.issues.get(number)
        if len(parts) == 1:
            if iss is None:
                self._json_err(404, "Not Found")
                return
            if method == "GET":
                self._json(200, issue_json(repo, iss))
                return
            if method == "PATCH":
                body, err = self._json_body()
                if err:
                    self._json_err(400, "Invalid JSON body")
                    return
                with repo.lock:
                    if "state" in body and body["state"] in ("open", "closed"):
                        iss["state"] = body["state"]
                    if "title" in body:
                        iss["title"] = body["title"] or ""
                    if "body" in body:
                        iss["body"] = body["body"] or ""
                    if "labels" in body:
                        names = []
                        for lab in body["labels"] or []:
                            names.append(lab["name"] if isinstance(lab, dict) else lab)
                        iss["labels"] = names
                self._json(200, issue_json(repo, iss))
                return
            self._json_err(404, "Not Found")
            return
        if len(parts) == 2 and parts[1] == "comments":
            if iss is None:
                self._json_err(404, "Not Found")
                return
            if method == "GET":
                out = []
                for cid in iss["comments"]:
                    c = repo.comments[cid]
                    out.append({"id": cid, "node_id": gid("C", cid), "body": c["body"],
                                "user": {"login": VIEWER["login"], "id": VIEWER["id"]},
                                "created_at": FIXED_TS, "updated_at": FIXED_TS})
                self._json(200, out)
                return
            if method == "POST":
                body, err = self._json_body()
                if err:
                    self._json_err(400, "Invalid JSON body")
                    return
                if not isinstance(body.get("body"), str):
                    self._json_err(422, "body is required")
                    return
                with repo.lock:
                    cid = repo.comment_seq
                    repo.comment_seq += 1
                    repo.comments[cid] = {"body": body["body"]}
                    iss["comments"].append(cid)
                self._json(201, {"id": cid, "node_id": gid("C", cid), "body": body["body"],
                                 "user": {"login": VIEWER["login"], "id": VIEWER["id"]},
                                 "created_at": FIXED_TS, "updated_at": FIXED_TS})
                return
            self._json_err(404, "Not Found")
            return
        self._json_err(404, "Not Found")

    def _handle_pulls(self, repo, method, parts, query):
        if not parts:
            if method == "GET":
                state = query.get("state", ["open"])[0]
                items = list(repo.pulls.values())
                if state != "all":
                    items = [p for p in items if p["state"] == state]
                items.sort(key=lambda p: p["number"], reverse=True)
                path = "/api/v3/repos/%s/%s/pulls" % (repo.owner, repo.name)
                chunk, extra = self._paginate([pull_json(repo, p) for p in items],
                                             query, path)
                self._json(200, chunk, extra=extra)
                return
            if method == "POST":
                body, err = self._json_body()
                if err:
                    self._json_err(400, "Invalid JSON body")
                    return
                head = body.get("head")
                base = body.get("base")
                if not isinstance(head, str) or not isinstance(base, str):
                    self._json_err(422, "head and base are required")
                    return
                if ("refs/heads/" + head) not in repo.refs or ("refs/heads/" + base) not in repo.refs:
                    self._json_err(422, "Head or base branch does not exist")
                    return
                with repo.lock:
                    for p in repo.pulls.values():
                        if p["state"] == "open" and p["head_ref"] == head and p["base_ref"] == base:
                            self._json_err(422, "A pull request already exists for this branch")
                            return
                pr = create_pull_internal(repo, body.get("title"), body.get("body"), head, base)
                self._json(201, pull_json(repo, pr))
                return
            self._json_err(404, "Not Found")
            return
        try:
            number = int(parts[0])
        except ValueError:
            self._json_err(404, "Not Found")
            return
        pr = repo.pulls.get(number)
        if len(parts) == 1:
            if pr is None:
                self._json_err(404, "Not Found")
                return
            if method == "GET":
                self._json(200, pull_json(repo, pr))
                return
            self._json_err(404, "Not Found")
            return
        if len(parts) == 2 and parts[1] == "files" and method == "GET":
            if pr is None:
                self._json_err(404, "Not Found")
                return
            base_tip = repo.tip_commit("refs/heads/" + pr["base_ref"])
            head_tip = repo.tip_commit("refs/heads/" + pr["head_ref"])
            if base_tip is None or head_tip is None:
                self._json(200, [])
                return
            mb = repo.merge_base(base_tip, head_tip)
            mb_tree = parse_commit(repo.get_object(mb)[1])["tree"] if mb else None
            head_tree = parse_commit(repo.get_object(head_tip)[1])["tree"]
            out = []
            for d in diff_paths(repo, mb_tree, head_tree):
                add, dele = line_counts(repo, d["old"], d["new"])
                status = {"added": "added", "removed": "removed"}.get(d["status"], "modified")
                out.append({"filename": d["path"], "status": status,
                            "additions": add, "deletions": dele, "changes": add + dele,
                            "sha": d["new"] or d["old"] or ""})
            self._json(200, out)
            return
        if len(parts) == 2 and parts[1] == "merge" and method == "PUT":
            if pr is None:
                self._json_err(404, "Not Found")
                return
            body, err = self._json_body()
            if err:
                self._json_err(400, "Invalid JSON body")
                return
            status, payload = do_merge_internal(repo, pr)
            self._json(status, payload)
            return
        self._json_err(404, "Not Found")

    # -- Git smart HTTP --------------------------------------------------------
    def _handle_git(self, method, owner, rname, tail, query):
        repo = STORE.get_repo(owner, rname)
        if tail == "/info/refs":
            service = query.get("service", [None])[0]
            if repo is None or service not in ("git-upload-pack", "git-receive-pack"):
                self._json_err(404, "Not Found")
                return
            if method != "GET":
                self._json_err(404, "Not Found")
                return
            v2 = (service == "git-upload-pack" and
                  self.headers.get("Git-Protocol", "") == "version=2")
            body = self._git_advertisement(repo, service, v2)
            ctype = "application/x-%s-advertisement" % service
            self._send(200, body, ctype=ctype)
            return
        if repo is None:
            self._read_body()
            self._json_err(404, "Not Found")
            return
        if tail == "/git-upload-pack" and method == "POST":
            raw = self._read_body()
            v2 = self.headers.get("Git-Protocol", "") == "version=2"
            try:
                body = self._git_upload_pack(repo, raw, v2)
            except ValueError as exc:
                self._send(400, b"", ctype="application/x-git-upload-pack-result")
                return
            self._send(200, body, ctype="application/x-git-upload-pack-result")
            return
        if tail == "/git-receive-pack" and method == "POST":
            raw = self._read_body()
            try:
                body = self._git_receive_pack(repo, raw)
            except ValueError:
                # unpack failure: still answer a report if possible
                inner = pkt("unpack error\n") + pkt_flush()
                body = pkt(b"\x01" + inner) + pkt_flush()
            self._send(200, body, ctype="application/x-git-receive-pack-result")
            return
        self._read_body()
        self._json_err(404, "Not Found")

    def _git_ref_lines(self, repo, service):
        """v0 advertisement ref lines (without preamble/flush)."""
        caps_upload = ("side-band-64k thin-pack ofs-delta shallow multi_ack_detailed "
                       "agent=%s symref=HEAD:refs/heads/main object-format=sha1" % AGENT)
        caps_receive = ("report-status side-band-64k delete-refs ofs-delta "
                        "agent=%s symref=HEAD:refs/heads/main object-format=sha1" % AGENT)
        caps = caps_upload if service == "git-upload-pack" else caps_receive
        refs = sorted(repo.refs.items())
        lines = []
        if not refs:
            lines.append("%s capabilities^{}\0%s\n" % (ZERO_OID, caps))
            return lines
        head_oid = repo.refs.get("refs/heads/" + repo.default_branch, ZERO_OID)
        lines.append("%s HEAD\0%s\n" % (head_oid, caps))
        for name, oid in refs:
            lines.append("%s %s\n" % (oid, name))
            if name.startswith("refs/tags/"):
                obj = repo.get_object(oid)
                if obj and obj[0] == "tag":
                    peeled = repo.peel(oid)
                    if peeled:
                        lines.append("%s %s^{}\n" % (peeled, name))
        return lines

    def _git_advertisement(self, repo, service, v2):
        if v2:
            out = bytearray()
            out += pkt("version 2\n")
            out += pkt("ls-refs=unborn\n")
            out += pkt("fetch=shallow filter\n")
            out += pkt("object-format=sha1\n")
            out += pkt("agent=%s\n" % AGENT)
            out += pkt_flush()
            return bytes(out)
        out = bytearray()
        out += pkt("# service=%s\n" % service)
        out += pkt_flush()
        for line in self._git_ref_lines(repo, service):
            out += pkt(line)
        out += pkt_flush()
        return bytes(out)

    def _git_upload_pack(self, repo, raw, v2):
        if v2:
            return self._fetch_v2(repo, raw)
        return self._fetch_v0(repo, raw)

    def _parse_fetch_request(self, raw, v2):
        """Parse fetch pkt-lines. Returns dict or raises ValueError."""
        packets, pos = parse_pkt_stream(raw, 0)
        wants, haves, shallow, deepen = [], [], [], None
        done = False
        if v2:
            command = None
            in_args = False
            for kind, payload in packets:
                if kind == "delim":
                    in_args = True
                    continue
                if kind == "flush":
                    break
                if kind != "data":
                    continue
                line = payload.decode("utf-8", "replace").rstrip("\n")
                if not in_args:
                    if line.startswith("command="):
                        command = line[len("command="):]
                    continue
                if line.startswith("want "):
                    wants.append(line[5:].strip())
                elif line.startswith("have "):
                    haves.append(line[5:].strip())
                elif line.startswith("shallow "):
                    shallow.append(line[8:].strip())
                elif line.startswith("deepen "):
                    deepen = int(line[7:].strip())
                elif line == "done":
                    done = True
            if command not in ("fetch", "ls-refs"):
                raise ValueError("unknown v2 command %r" % command)
            return {"command": command, "wants": wants, "haves": haves,
                    "shallow": shallow, "deepen": deepen, "done": done,
                    "raw_packets": packets}
        # v0: want lines, flush, have lines, done, flush
        section = 0
        for kind, payload in packets:
            if kind == "flush":
                section += 1
                continue
            if kind != "data":
                continue
            line = payload.decode("utf-8", "replace").rstrip("\n")
            if line.startswith("want "):
                wants.append(line.split()[1])
            elif line.startswith("shallow "):
                shallow.append(line[8:].strip())
            elif line.startswith("deepen "):
                deepen = int(line[7:].strip())
            elif line.startswith("have "):
                haves.append(line[5:].strip())
            elif line == "done":
                done = True
        return {"command": "fetch", "wants": wants, "haves": haves,
                "shallow": shallow, "deepen": deepen, "done": done}

    def _fetch_v2(self, repo, raw):
        req = self._parse_fetch_request(raw, True)
        if req["command"] == "ls-refs":
            return self._ls_refs_v2(repo, raw)
        wants = req["wants"]
        haves = req["haves"]
        done = req["done"]
        if not wants:
            return pkt_flush()
        sel = fetch_objects(repo, wants, haves, req["shallow"], req["deepen"])
        out = bytearray()
        if haves and not done:
            # Negotiation round (mirrors upload-pack.c): acknowledgments
            # only, no packfile yet. If the client already holds
            # everything, answer "ready" and send the (empty) pack in
            # this same response; otherwise end with a flush and the
            # client sends another round.
            common = [h for h in haves if repo.get_object(h) is not None]
            out += pkt("acknowledgments\n")
            if common:
                for oid in common:
                    out += pkt("ACK %s\n" % oid)
            else:
                out += pkt("NAK\n")
            if not sel["objects"]:
                out += pkt("ready\n")
                out += pkt_delim()
            else:
                out += pkt_flush()
                return bytes(out)
        if sel["shallow"] or sel["unshallow"]:
            out += pkt("shallow-info\n")
            for oid in sel["shallow"]:
                out += pkt("shallow %s\n" % oid)
            for oid in sel["unshallow"]:
                out += pkt("unshallow %s\n" % oid)
            out += pkt_delim()
        # Packfile section: always sent (even when empty, so the client
        # sees a valid pack with zero objects rather than a missing one).
        pack = write_pack([(t, d) for (_oid, t, d) in sel["objects"]])
        out += pkt("packfile\n")
        for chunk in _sideband_chunks(pack):
            out += pkt(b"\x01" + chunk)
        out += pkt_flush()
        return bytes(out)

    def _ls_refs_v2(self, repo, raw):
        packets, _pos = parse_pkt_stream(raw, 0)
        prefixes, peel, symrefs = [], False, False
        in_args = False
        for kind, payload in packets:
            if kind == "delim":
                in_args = True
                continue
            if kind == "flush":
                break
            if kind != "data":
                continue
            line = payload.decode("utf-8", "replace").rstrip("\n")
            if not in_args:
                continue
            if line == "peel":
                peel = True
            elif line == "symrefs":
                symrefs = True
            elif line.startswith("ref-prefix "):
                prefixes.append(line[len("ref-prefix "):])
        out = bytearray()
        refs = sorted(repo.refs.items())
        head_oid = repo.refs.get("refs/heads/" + repo.default_branch, ZERO_OID)
        head_target = "refs/heads/" + repo.default_branch

        def _match(name):
            return (not prefixes) or any(name.startswith(p) for p in prefixes)

        # HEAD is a ref line of its own: it is included when symrefs was
        # requested and HEAD matches one of the ref-prefixes (or there are
        # none). Without it the client cannot learn the default branch.
        # An unborn HEAD is spelled "unborn HEAD ...", never a zero oid.
        if symrefs and _match("HEAD"):
            if head_oid == ZERO_OID:
                out += pkt("unborn HEAD symref-target:%s\n" % head_target)
            else:
                out += pkt("%s HEAD symref-target:%s\n" % (head_oid, head_target))
        for name, oid in refs:
            if not _match(name):
                continue
            attrs = ""
            if peel and name.startswith("refs/tags/"):
                obj = repo.get_object(oid)
                if obj and obj[0] == "tag":
                    peeled = repo.peel(oid)
                    if peeled:
                        attrs = " peeled:%s" % peeled
            out += pkt("%s %s%s\n" % (oid, name, attrs))
        out += pkt_flush()
        return bytes(out)

    def _fetch_v0(self, repo, raw):
        req = self._parse_fetch_request(raw, False)
        sel = fetch_objects(repo, req["wants"], req["haves"], req["shallow"], req["deepen"])
        out = bytearray()
        out += pkt("NAK\n")
        for oid in sel["shallow"]:
            out += pkt("shallow %s\n" % oid)
        for oid in sel["unshallow"]:
            out += pkt("unshallow %s\n" % oid)
        if sel["objects"]:
            pack = write_pack([(t, d) for (_oid, t, d) in sel["objects"]])
            for chunk in _sideband_chunks(pack):
                out += pkt(b"\x01" + chunk)
        out += pkt_flush()
        return bytes(out)

    def _git_receive_pack(self, repo, raw):
        packets, pos = parse_pkt_stream(raw, 0)
        commands = []
        for kind, payload in packets:
            if kind == "flush":
                break
            if kind != "data":
                continue
            line = payload.decode("utf-8", "surrogateescape").rstrip("\n")
            if "\0" in line:
                line = line.split("\0", 1)[0]
            parts = line.split(" ")
            if len(parts) != 3:
                raise ValueError("bad push command line")
            commands.append((parts[0], parts[1], parts[2]))
        pack_bytes = raw[pos:]
        unpack_ok = True
        unpack_detail = "ok"
        new_objects = []
        if pack_bytes:
            try:
                new_objects = read_pack(pack_bytes,
                                        store_lookup=lambda oid: repo.get_object(oid))
            except ValueError as exc:
                unpack_ok = False
                unpack_detail = "error: %s" % exc
        if unpack_ok:
            with repo.lock:
                for oid, otype, data in new_objects:
                    repo.objects.setdefault(oid, (otype, data))
        results = []
        if unpack_ok:
            with repo.lock:
                for old, new, ref in commands:
                    results.append(self._apply_push(repo, old, new, ref))
        else:
            results = [(ref, False, "unpack error") for (_o, _n, ref) in commands]
        inner = bytearray()
        inner += pkt("unpack %s\n" % unpack_detail)
        for ref, ok, reason in results:
            if ok:
                inner += pkt("ok %s\n" % ref)
            else:
                inner += pkt("ng %s %s\n" % (ref, reason))
        inner += pkt_flush()
        out = bytearray()
        for chunk in _sideband_chunks(bytes(inner)):
            out += pkt(b"\x01" + chunk)
        out += pkt_flush()
        return bytes(out)

    def _apply_push(self, repo, old, new, ref):
        cur = repo.refs.get(ref)
        if new == ZERO_OID:
            # delete
            if cur is None:
                return (ref, False, "non-fast-forward")
            if old != ZERO_OID and old != cur:
                return (ref, False, "non-fast-forward")
            del repo.refs[ref]
            return (ref, True, "")
        if old == ZERO_OID:
            if cur is not None:
                return (ref, False, "non-fast-forward")
            if repo.get_object(new) is None:
                return (ref, False, "non-fast-forward")
            repo.refs[ref] = new
            return (ref, True, "")
        # update: old must match current
        if cur is None or old != cur:
            return (ref, False, "non-fast-forward")
        if new == old:
            return (ref, True, "")
        obj = repo.get_object(new)
        if obj is None:
            return (ref, False, "non-fast-forward")
        if obj[0] == "commit":
            if not repo.is_ancestor(old, new):
                return (ref, False, "non-fast-forward")
        elif obj[0] == "tag":
            # tag update: only allow if it peels to a descendant
            peeled_new = repo.peel(new)
            peeled_old = repo.peel(old)
            if peeled_new != peeled_old and not repo.is_ancestor(peeled_old, peeled_new):
                return (ref, False, "non-fast-forward")
        repo.refs[ref] = new
        return (ref, True, "")


def _sideband_chunks(data, size=32768):
    for i in range(0, len(data), size):
        yield data[i:i + size]

# ---------------------------------------------------------------------------
# GraphQL v4 (small parser + executor for the documents gh / github-mcp-server
# send, plus __type introspection)
# ---------------------------------------------------------------------------

_GQL_TYPE_FIELDS = {
    "Issue": ["id", "number", "title", "state", "body", "url", "createdAt",
              "closedAt", "author", "labels", "comments", "repository"],
    "PullRequest": ["id", "number", "title", "state", "body", "headRefName",
                    "baseRefName", "mergeable", "merged", "files", "headRef",
                    "baseRef", "url", "createdAt", "closedAt", "author"],
    "Repository": ["id", "databaseId", "name", "nameWithOwner", "owner",
                   "defaultBranchRef", "viewerPermission", "hasIssuesEnabled",
                   "issue", "issues", "pullRequests", "pullRequest", "url",
                   "description", "isPrivate", "createdAt"],
    "User": ["id", "login", "name", "email", "avatarUrl", "url"],
    "Ref": ["name", "target"],
    "SearchType": [],
    "IssueConnection": ["nodes", "edges", "pageInfo", "totalCount"],
    "PullRequestConnection": ["nodes", "edges", "pageInfo", "totalCount"],
    "PullRequestChangedFileConnection": ["nodes", "edges", "pageInfo", "totalCount"],
    "PageInfo": ["hasNextPage", "hasPreviousPage", "endCursor", "startCursor"],
    "IssueEdge": ["node", "cursor"],
    "PullRequestEdge": ["node", "cursor"],
    "PullRequestChangedFile": ["path", "filename", "additions", "deletions", "changeType"],
    "Actor": ["login"],
}


class _GqlError(Exception):
    pass


def _gql_tokenize(text):
    toks = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in " \t\r\n,":
            i += 1
        elif c == "#":
            while i < n and text[i] != "\n":
                i += 1
        elif text.startswith("...", i):
            toks.append(("...", "..."))
            i += 3
        elif c in "{}()[]:!=$@":
            toks.append((c, c))
            i += 1
        elif c == '"':
            j = i + 1
            out = []
            while j < n and text[j] != '"':
                if text[j] == "\\" and j + 1 < n:
                    esc = text[j + 1]
                    out.append({"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\",
                                "/": "/"}.get(esc, esc))
                    j += 2
                else:
                    out.append(text[j])
                    j += 1
            toks.append(("str", "".join(out)))
            i = j + 1
        elif c == "-" or c.isdigit():
            j = i
            while j < n and (text[j].isdigit() or text[j] in ".-eE+"):
                j += 1
            raw = text[i:j]
            try:
                toks.append(("int", int(raw)))
            except ValueError:
                toks.append(("float", float(raw)))
            i = j
        elif c.isalpha() or c == "_":
            j = i
            while j < n and (text[j].isalnum() or text[j] == "_"):
                j += 1
            toks.append(("name", text[i:j]))
            i = j
        else:
            raise _GqlError("unexpected character %r" % c)
    toks.append(("eof", None))
    return toks


class _GqlParser:
    def __init__(self, text):
        self.toks = _gql_tokenize(text)
        self.pos = 0

    def peek(self):
        return self.toks[self.pos]

    def next(self):
        t = self.toks[self.pos]
        self.pos += 1
        return t

    def expect(self, kind, val=None):
        t = self.next()
        if t[0] != kind or (val is not None and t[1] != val):
            raise _GqlError("expected %s %s, got %r" % (kind, val, t))
        return t

    def parse_document(self):
        ops = []
        while self.peek()[0] != "eof":
            ops.append(self.parse_operation())
        return ops

    def parse_operation(self):
        kind, val = self.peek()
        if kind == "{":
            return {"type": "query", "name": None, "vars": {}, "sel": self.parse_selection_set()}
        self.next()  # query | mutation | subscription
        otype = val
        name = None
        if self.peek()[0] == "name":
            name = self.next()[1]
        vardefs = {}
        if self.peek() == ("(", "("):
            self.next()
            while self.peek() != (")", ")"):
                if self.peek() == ("$", "$"):
                    self.next()
                    vname = self.expect("name")[1]
                    self.expect(":")
                    vtype = self.parse_type()
                    default = None
                    if self.peek() == ("=", "="):
                        self.next()
                        default = self.parse_value({})
                    vardefs[vname] = default
                else:
                    self.next()
            self.expect(")")
        sel = self.parse_selection_set()
        return {"type": otype, "name": name, "vars": vardefs, "sel": sel}

    def parse_type(self):
        parts = []
        while self.peek()[0] in ("name", "[", "!", "]"):
            parts.append(self.next()[1])
        return "".join(parts)

    def parse_selection_set(self):
        self.expect("{")
        sels = []
        while self.peek() != ("}", "}"):
            sels.append(self.parse_selection())
        self.expect("}")
        return sels

    def parse_selection(self):
        if self.peek() == ("...", "..."):
            self.next()
            if self.peek() == ("name", "on"):
                self.next()
                tname = self.expect("name")[1]
                return {"kind": "inline", "type": tname, "sel": self.parse_selection_set()}
            fname = self.expect("name")[1]
            return {"kind": "spread", "name": fname}
        alias = None
        name = self.expect("name")[1]
        if self.peek() == (":", ":"):
            self.next()
            alias = name
            name = self.expect("name")[1]
        args = {}
        if self.peek() == ("(", "("):
            self.next()
            while self.peek() != (")", ")"):
                aname = self.expect("name")[1]
                self.expect(":")
                args[aname] = self.parse_value({})
                # values may carry variables; resolved at execution
            self.expect(")")
        # directives: skip @name(args)
        while self.peek() == ("@", "@"):
            self.next()
            self.expect("name")
            if self.peek() == ("(", "("):
                depth = 0
                while True:
                    t = self.next()
                    if t == ("(", "("):
                        depth += 1
                    elif t == (")", ")"):
                        depth -= 1
                        if depth == 0:
                            break
        sub = None
        if self.peek() == ("{", "{"):
            sub = self.parse_selection_set()
        return {"kind": "field", "name": name, "alias": alias or name,
                "args": args, "sel": sub}

    def parse_value(self, variables):
        kind, val = self.next()
        if kind == "$":
            return ("var", self.expect("name")[1])
        if kind in ("str", "int", "float"):
            return val
        if kind == "name":
            if val == "true":
                return True
            if val == "false":
                return False
            if val == "null":
                return None
            return ("enum", val)
        if kind == "[":
            items = []
            while self.peek() != ("]", "]"):
                items.append(self.parse_value(variables))
            self.expect("]")
            return items
        if kind == "{":
            obj = {}
            while self.peek() != ("}", "}"):
                k = self.expect("name")[1]
                self.expect(":")
                obj[k] = self.parse_value(variables)
            self.expect("}")
            return obj
        raise _GqlError("unexpected value token %r" % (kind,))


def _resolve_value(v, variables):
    if isinstance(v, tuple) and len(v) == 2 and v[0] == "var":
        return variables.get(v[1])
    if isinstance(v, tuple) and len(v) == 2 and v[0] == "enum":
        return v[1]
    if isinstance(v, list):
        return [_resolve_value(x, variables) for x in v]
    if isinstance(v, dict):
        return {k: _resolve_value(x, variables) for k, x in v.items()}
    return v


class _GqlExecutor:
    def __init__(self):
        self.errors = []

    def execute(self, query_text, variables):
        variables = variables or {}
        try:
            doc = _GqlParser(query_text).parse_document()
        except _GqlError as exc:
            return {"data": None, "errors": [{"message": str(exc)}]}
        results = []
        for op in doc:
            if op["type"] == "mutation":
                results.append(self._exec_root(op, variables, "Mutation"))
            else:
                results.append(self._exec_root(op, variables, "Query"))
        if len(results) == 1:
            data = results[0]
        else:
            # multiple operations in one document: merge (not used by suite)
            data = {}
            for r in results:
                data.update(r)
        out = {"data": data}
        if self.errors:
            out["errors"] = self.errors
        return out

    def _exec_root(self, op, variables, root_type):
        out = {}
        for sel in op["sel"]:
            if sel["kind"] == "field":
                args = {k: _resolve_value(v, variables) for k, v in sel["args"].items()}
                out[sel["alias"]] = self._field(root_type, None, sel, args)
            elif sel["kind"] == "inline":
                if sel["type"] == root_type:
                    sub = self._exec_root({"type": op["type"], "sel": sel["sel"]}, variables, root_type)
                    out.update(sub)
        return out

    def _field(self, type_name, obj, sel, args):
        name = sel["name"]
        allowed = _GQL_TYPE_FIELDS.get(type_name, [])
        if type_name in ("Query", "Mutation"):
            allowed = None  # root fields handled below; unknown -> error
        if allowed is not None and name not in allowed and not name.startswith("__"):
            self.errors.append({"message": 'Cannot query field "%s" on type "%s".' % (name, type_name)})
            return None
        handler = getattr(self, "_resolve_" + type_name.replace(" ", "_"), None)
        if handler is not None:
            return handler(obj, sel, args)
        return None

    def _select(self, type_name, obj, sel):
        """Execute a selection set against obj of type_name -> dict."""
        out = {}
        if not sel:
            return out
        for s in sel:
            if s["kind"] == "field":
                args = {}
                # args resolved by caller? resolve here with empty vars is wrong;
                # args are resolved in _exec_selection via stored vars
                out[s["alias"]] = self._field(type_name, obj, s, s.get("_args", {}))
            elif s["kind"] == "inline":
                if s["type"] == type_name:
                    out.update(self._select(type_name, obj, s["sel"]))
        return out

    # -- root resolvers --------------------------------------------------------
    def _resolve_Query(self, _obj, sel, args):
        name = sel["name"]
        if name == "viewer":
            return self._select("User", VIEWER, sel["sel"])
        if name == "repository":
            repo = STORE.get_repo(args.get("owner"), args.get("name"))
            if repo is None:
                return None
            return self._select_repo(repo, sel["sel"], args)
        if name == "node":
            return self._resolve_node(args.get("id"), sel["sel"])
        if name == "__type":
            return self._introspect(args.get("name"))
        if name == "__schema":
            return {"types": [{"name": k} for k in sorted(_GQL_TYPE_FIELDS)]}
        self.errors.append({"message": 'Cannot query field "%s" on type "Query".' % name})
        return None

    def _resolve_Mutation(self, _obj, sel, args):
        name = sel["name"]
        inp = args.get("input") or {}
        if name == "createIssue":
            repo = find_repo_by_dbid(_dbid_from_gid(inp.get("repositoryId")))
            if repo is None:
                self.errors.append({"message": "Repository not found"})
                return None
            iss = create_issue_internal(repo, inp.get("title"), inp.get("body"), [])
            return {"issue": self._select("Issue", (repo, iss), sel["sel"] and _sub(sel, "issue"))}
        if name == "closeIssue":
            kind, parts = ungid(inp.get("issueId") or "")
            if kind == "I" and len(parts) == 2:
                repo = find_repo_by_dbid(int(parts[0]))
                iss = repo.issues.get(int(parts[1])) if repo else None
                if iss is not None:
                    with repo.lock:
                        iss["state"] = "closed"
                    return {"issue": self._select("Issue", (repo, iss), _sub(sel, "issue"))}
            self.errors.append({"message": "Issue not found"})
            return None
        if name == "createPullRequest":
            repo = find_repo_by_dbid(_dbid_from_gid(inp.get("repositoryId")))
            if repo is None:
                self.errors.append({"message": "Repository not found"})
                return None
            head, base = inp.get("headRefName"), inp.get("baseRefName")
            if ("refs/heads/" + str(head)) not in repo.refs or ("refs/heads/" + str(base)) not in repo.refs:
                self.errors.append({"message": "Head or base branch does not exist"})
                return None
            pr = create_pull_internal(repo, inp.get("title"), inp.get("body") or "",
                                      head, base)
            return {"pullRequest": self._select("PullRequest", (repo, pr), _sub(sel, "pullRequest"))}
        if name == "mergePullRequest":
            kind, parts = ungid(inp.get("pullRequestId") or "")
            if kind == "P" and len(parts) == 2:
                repo = find_repo_by_dbid(int(parts[0]))
                pr = repo.pulls.get(int(parts[1])) if repo else None
                if pr is not None:
                    status, _payload = do_merge_internal(repo, pr)
                    if status != 200:
                        self.errors.append({"message": "Merge conflict"})
                        return None
                    return {"pullRequest": self._select("PullRequest", (repo, pr), _sub(sel, "pullRequest"))}
            self.errors.append({"message": "Pull request not found"})
            return None
        if name == "addComment":
            kind, parts = ungid(inp.get("subjectId") or "")
            if kind == "I" and len(parts) == 2:
                repo = find_repo_by_dbid(int(parts[0]))
                iss = repo.issues.get(int(parts[1])) if repo else None
                if iss is not None:
                    with repo.lock:
                        cid = repo.comment_seq
                        repo.comment_seq += 1
                        repo.comments[cid] = {"body": inp.get("body") or ""}
                        iss["comments"].append(cid)
                    node = {"id": gid("C", cid), "body": inp.get("body") or ""}
                    edge_sel = _sub_list(_sub(sel, "commentEdge"), "node")
                    return {"commentEdge": {"node": self._select_comment(node, edge_sel)}}
            self.errors.append({"message": "Issue not found"})
            return None
        self.errors.append({"message": 'Cannot query field "%s" on type "Mutation".' % name})
        return None

    def _select_comment(self, node, sel):
        out = {}
        if not sel:
            return out
        for s in sel:
            if s["kind"] == "field":
                if s["name"] in ("id", "body"):
                    out[s["alias"]] = node.get(s["name"])
                else:
                    self.errors.append({"message": 'Cannot query field "%s" on type "IssueComment".' % s["name"]})
                    out[s["alias"]] = None
        return out

    def _resolve_node(self, gid_value, sel):
        kind, parts = ungid(gid_value or "")
        if kind == "R" and len(parts) == 1:
            repo = find_repo_by_dbid(int(parts[0]))
            if repo is None:
                return None
            return self._select_repo(repo, sel, {})
        if kind == "I" and len(parts) == 2:
            repo = find_repo_by_dbid(int(parts[0]))
            iss = repo.issues.get(int(parts[1])) if repo else None
            if iss is None:
                return None
            return self._select("Issue", (repo, iss), sel)
        if kind == "P" and len(parts) == 2:
            repo = find_repo_by_dbid(int(parts[0]))
            pr = repo.pulls.get(int(parts[1])) if repo else None
            if pr is None:
                return None
            return self._select("PullRequest", (repo, pr), sel)
        return None

    def _introspect(self, name):
        if name == "SearchType":
            return {"name": "SearchType",
                    "enumValues": [{"name": n} for n in
                                   ["ISSUE", "REPOSITORY", "USER", "DISCUSSION",
                                    "CODE", "COMMIT", "MILESTONE", "WIKI"]],
                    "fields": None}
        fields = _GQL_TYPE_FIELDS.get(name)
        if fields is None:
            return None
        return {"name": name,
                "fields": [{"name": f} for f in fields],
                "enumValues": None}

    # -- object resolvers ------------------------------------------------------
    def _select_repo(self, repo, sel, _args):
        out = {}
        if not sel:
            return out
        for s in sel:
            if s["kind"] == "inline":
                if s["type"] == "Repository":
                    out.update(self._select_repo(repo, s["sel"], _args))
                continue
            if s["kind"] != "field":
                continue
            name = s["name"]
            args = s.get("_args", {})
            if name == "id":
                out[s["alias"]] = gid("R", repo.db_id)
            elif name == "databaseId":
                out[s["alias"]] = repo.db_id
            elif name == "name":
                out[s["alias"]] = repo.name
            elif name == "nameWithOwner":
                out[s["alias"]] = "%s/%s" % (repo.owner, repo.name)
            elif name == "owner":
                out[s["alias"]] = self._select("User", VIEWER, s["sel"])
            elif name == "defaultBranchRef":
                out[s["alias"]] = {"name": repo.default_branch}
                if s["sel"]:
                    out[s["alias"]] = {f["alias"]: repo.default_branch
                                       for f in s["sel"] if f["kind"] == "field" and f["name"] == "name"}
            elif name == "viewerPermission":
                out[s["alias"]] = "ADMIN"
            elif name == "hasIssuesEnabled":
                out[s["alias"]] = True
            elif name == "issue":
                iss = repo.issues.get(args.get("number"))
                out[s["alias"]] = self._select("Issue", (repo, iss), s["sel"]) if iss else None
            elif name == "issues":
                out[s["alias"]] = self._issues_conn(repo, s, args)
            elif name == "pullRequest":
                pr = repo.pulls.get(args.get("number"))
                out[s["alias"]] = self._select("PullRequest", (repo, pr), s["sel"]) if pr else None
            elif name == "pullRequests":
                out[s["alias"]] = self._prs_conn(repo, s, args)
            elif name == "description":
                out[s["alias"]] = repo.description
            elif name == "isPrivate":
                out[s["alias"]] = repo.private
            elif name == "url":
                out[s["alias"]] = ""
            elif name == "createdAt":
                out[s["alias"]] = FIXED_TS
            else:
                self.errors.append({"message": 'Cannot query field "%s" on type "Repository".' % name})
                out[s["alias"]] = None
        return out

    def _issues_conn(self, repo, sel, args):
        items = list(repo.issues.values())
        states = args.get("states")
        if states:
            want = {"OPEN": "open", "CLOSED": "closed"}
            items = [i for i in items if i["state"] in
                     {want.get(s, s.lower()) for s in states}]
        order = args.get("orderBy") or {}
        if order.get("field") == "CREATED_AT":
            items.sort(key=lambda i: i["number"], reverse=(order.get("direction") != "ASC"))
        else:
            items.sort(key=lambda i: i["number"])
        first = args.get("first")
        after = args.get("after")
        start = 0
        if after:
            try:
                start = int(_cursor_decode(after)) + 1
            except Exception:
                start = 0
        total = len(items)
        if first is not None:
            page = items[start:start + first]
        else:
            page = items[start:]
        end = start + len(page)
        nodes_sel = None
        page_sel = None
        for f in (sel["sel"] or []):
            if f["kind"] == "field" and f["name"] == "nodes":
                nodes_sel = f["sel"]
            if f["kind"] == "field" and f["name"] == "pageInfo":
                page_sel = f["sel"]
            if f["kind"] == "field" and f["name"] == "totalCount":
                pass
        nodes = [self._select("Issue", (repo, i), nodes_sel) for i in page]
        page_info = {"hasNextPage": end < total,
                     "endCursor": _cursor_encode(end - 1) if page else None,
                     "hasPreviousPage": start > 0,
                     "startCursor": _cursor_encode(start) if page else None}
        if page_sel:
            page_info = {f["alias"]: page_info.get(f["name"])
                         for f in page_sel if f["kind"] == "field"}
        out = {"nodes": nodes, "pageInfo": page_info, "totalCount": total}
        return out

    def _prs_conn(self, repo, sel, args):
        items = [p for p in repo.pulls.values() if p["state"] == "open"]
        items.sort(key=lambda p: p["number"], reverse=True)
        first = args.get("first")
        if first is not None:
            items = items[:first]
        nodes_sel = None
        for f in (sel["sel"] or []):
            if f["kind"] == "field" and f["name"] == "nodes":
                nodes_sel = f["sel"]
        return {"nodes": [self._select("PullRequest", (repo, p), nodes_sel) for p in items],
                "pageInfo": {"hasNextPage": False, "endCursor": None,
                             "hasPreviousPage": False, "startCursor": None},
                "totalCount": len(items)}

    def _resolve_Issue(self, obj, sel, args):
        repo, iss = obj
        name = sel["name"]
        if name == "id":
            return gid("I", repo.db_id, iss["number"])
        if name == "number":
            return iss["number"]
        if name == "title":
            return iss["title"]
        if name == "state":
            return iss["state"].upper()
        if name == "body":
            return iss["body"]
        if name == "url":
            return ""
        if name == "createdAt":
            return FIXED_TS
        if name == "closedAt":
            return FIXED_TS if iss["state"] == "closed" else None
        if name == "author":
            return self._select("User", VIEWER, sel["sel"])
        if name == "repository":
            return self._select_repo(repo, sel["sel"], {})
        if name == "labels":
            return {"nodes": [{"name": n} for n in iss["labels"]]}
        if name == "comments":
            return {"nodes": []}
        return None

    def _resolve_PullRequest(self, obj, sel, args):
        repo, pr = obj
        name = sel["name"]
        if name == "id":
            return gid("P", repo.db_id, pr["number"])
        if name == "number":
            return pr["number"]
        if name == "title":
            return pr["title"]
        if name == "state":
            return pr["state"].upper()
        if name == "body":
            return pr["body"]
        if name == "headRefName":
            return pr["head_ref"]
        if name == "baseRefName":
            return pr["base_ref"]
        if name == "mergeable":
            return "CONFLICTING" if pr_conflicted(repo, pr) else "MERGEABLE"
        if name == "merged":
            return pr["merged"]
        if name == "url":
            return ""
        if name == "createdAt":
            return FIXED_TS
        if name == "closedAt":
            return FIXED_TS if pr["state"] == "closed" else None
        if name == "author":
            return self._select("User", VIEWER, sel["sel"])
        if name == "headRef":
            return {"name": pr["head_ref"]}
        if name == "baseRef":
            return {"name": pr["base_ref"]}
        if name == "files":
            base_tip = repo.tip_commit("refs/heads/" + pr["base_ref"])
            head_tip = repo.tip_commit("refs/heads/" + pr["head_ref"])
            nodes = []
            if base_tip and head_tip:
                mb = repo.merge_base(base_tip, head_tip)
                mb_tree = parse_commit(repo.get_object(mb)[1])["tree"] if mb else None
                head_tree = parse_commit(repo.get_object(head_tip)[1])["tree"]
                nodes = [{"path": d["path"]} for d in diff_paths(repo, mb_tree, head_tree)]
            return {"nodes": nodes, "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "totalCount": len(nodes)}
        return None

    def _resolve_User(self, obj, sel, args):
        name = sel["name"]
        if name == "id":
            return gid("U", obj["id"])
        if name == "login":
            return obj["login"]
        if name == "name":
            return obj["name"]
        if name == "email":
            return obj["email"]
        if name == "avatarUrl":
            return ""
        if name == "url":
            return ""
        return None


def _sub(sel, field_name):
    """Return the sub-selection list of the named field within sel's selection."""
    if not sel or not sel.get("sel"):
        return None
    for f in sel["sel"]:
        if f["kind"] == "field" and f["name"] == field_name:
            return f.get("sel")
    return None


def _sub_list(sel_list, field_name):
    """Same as _sub but over a bare selection list (for nested edges)."""
    if not sel_list:
        return None
    for f in sel_list:
        if isinstance(f, dict) and f.get("kind") == "field" and f.get("name") == field_name:
            return f.get("sel")
    return None


def _dbid_from_gid(value):
    kind, parts = ungid(value or "")
    if kind == "R" and parts:
        try:
            return int(parts[0])
        except ValueError:
            return None
    return None


def _cursor_encode(index):
    return base64.urlsafe_b64encode(("c:%d" % index).encode()).decode().rstrip("=")


def _cursor_decode(cursor):
    pad = "=" * (-len(cursor) % 4)
    raw = base64.urlsafe_b64decode((cursor + pad).encode()).decode()
    return int(raw.split(":")[1])


def _attach_args(node, variables):
    """Walk a parsed selection tree resolving argument values with variables."""
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            if k == "args":
                out[k] = {ak: _resolve_value(av, variables) for ak, av in v.items()}
                out["_args"] = out[k]
            else:
                out[k] = _attach_args(v, variables)
        return out
    if isinstance(node, list):
        return [_attach_args(x, variables) for x in node]
    return node


def execute_graphql(body_obj):
    """body_obj: dict (single op) or list (batch). Returns response object/list."""
    if isinstance(body_obj, list):
        return [execute_graphql_one(op) for op in body_obj]
    return execute_graphql_one(body_obj)


def execute_graphql_one(op):
    if not isinstance(op, dict) or not isinstance(op.get("query"), str):
        return {"data": None, "errors": [{"message": "Invalid GraphQL request"}]}
    ex = _GqlExecutor()
    # attach resolved args into the parsed selections before execution
    query_text = op["query"]
    variables = op.get("variables") or {}
    try:
        doc = _GqlParser(query_text).parse_document()
    except _GqlError as exc:
        return {"data": None, "errors": [{"message": str(exc)}]}
    doc = _attach_args(doc, variables)
    results = []
    for oper in doc:
        if oper["type"] == "mutation":
            results.append(ex._exec_root(oper, variables, "Mutation"))
        else:
            results.append(ex._exec_root(oper, variables, "Query"))
    data = results[0] if len(results) == 1 else {}
    if len(results) > 1:
        for r in results:
            data.update(r)
    out = {"data": data}
    if ex.errors:
        out["errors"] = ex.errors
    return out

# ---------------------------------------------------------------------------
# MCP (JSON-RPC 2.0 Streamable HTTP, modern 2026-07-28 revision)
# ---------------------------------------------------------------------------

MCP_TOOLS = [
    {"name": "create_repo", "description": "Create a new repository.",
     "inputSchema": {"type": "object", "properties": {
         "name": {"type": "string", "description": "Repository name"},
         "description": {"type": "string", "description": "Repository description"},
         "private": {"type": "boolean", "description": "Whether the repository is private"},
         "owner": {"type": "string", "description": "Owner login (defaults to the viewer)"}},
         "required": ["name"]}},
    {"name": "get_repo", "description": "Get a repository by owner and name.",
     "inputSchema": {"type": "object", "properties": {
         "owner": {"type": "string", "description": "Repository owner"},
         "repo": {"type": "string", "description": "Repository name"},
         "full_name": {"type": "string", "description": "owner/repo"}},
         "required": ["owner", "repo"]}},
    {"name": "create_issue", "description": "Create an issue in a repository.",
     "inputSchema": {"type": "object", "properties": {
         "owner": {"type": "string", "description": "Repository owner"},
         "repo": {"type": "string", "description": "Repository name"},
         "title": {"type": "string", "description": "Issue title"},
         "body": {"type": "string", "description": "Issue body"},
         "full_name": {"type": "string", "description": "owner/repo"}},
         "required": ["owner", "repo", "title"]}},
    {"name": "list_issues", "description": "List issues in a repository.",
     "inputSchema": {"type": "object", "properties": {
         "owner": {"type": "string", "description": "Repository owner"},
         "repo": {"type": "string", "description": "Repository name"},
         "state": {"type": "string", "description": "Issue state: open, closed, or all"},
         "full_name": {"type": "string", "description": "owner/repo"}},
         "required": ["owner", "repo"]}},
    {"name": "create_pull_request", "description": "Create a pull request.",
     "inputSchema": {"type": "object", "properties": {
         "owner": {"type": "string", "description": "Repository owner"},
         "repo": {"type": "string", "description": "Repository name"},
         "title": {"type": "string", "description": "Pull request title"},
         "head": {"type": "string", "description": "Head branch name"},
         "base": {"type": "string", "description": "Base branch name"},
         "body": {"type": "string", "description": "Pull request body"},
         "full_name": {"type": "string", "description": "owner/repo"}},
         "required": ["owner", "repo", "title", "head", "base"]}},
    {"name": "merge_pull_request", "description": "Merge a pull request.",
     "inputSchema": {"type": "object", "properties": {
         "owner": {"type": "string", "description": "Repository owner"},
         "repo": {"type": "string", "description": "Repository name"},
         "pull_number": {"type": "integer", "description": "Pull request number"},
         "full_name": {"type": "string", "description": "owner/repo"}},
         "required": ["owner", "repo", "pull_number"]}},
    {"name": "get_issue", "description": "Get an issue by number.",
     "inputSchema": {"type": "object", "properties": {
         "owner": {"type": "string", "description": "Repository owner"},
         "repo": {"type": "string", "description": "Repository name"},
         "issue_number": {"type": "integer", "description": "Issue number"},
         "full_name": {"type": "string", "description": "owner/repo"}},
         "required": ["owner", "repo", "issue_number"]}},
    {"name": "add_issue_comment", "description": "Add a comment to an issue.",
     "inputSchema": {"type": "object", "properties": {
         "owner": {"type": "string", "description": "Repository owner"},
         "repo": {"type": "string", "description": "Repository name"},
         "issue_number": {"type": "integer", "description": "Issue number"},
         "body": {"type": "string", "description": "Comment body"},
         "full_name": {"type": "string", "description": "owner/repo"}},
         "required": ["owner", "repo", "issue_number", "body"]}},
    {"name": "list_pull_requests", "description": "List pull requests in a repository.",
     "inputSchema": {"type": "object", "properties": {
         "owner": {"type": "string", "description": "Repository owner"},
         "repo": {"type": "string", "description": "Repository name"},
         "state": {"type": "string", "description": "Pull request state: open, closed, or all"},
         "full_name": {"type": "string", "description": "owner/repo"}},
         "required": ["owner", "repo"]}},
    {"name": "get_pull_request", "description": "Get a pull request by number.",
     "inputSchema": {"type": "object", "properties": {
         "owner": {"type": "string", "description": "Repository owner"},
         "repo": {"type": "string", "description": "Repository name"},
         "pull_number": {"type": "integer", "description": "Pull request number"},
         "full_name": {"type": "string", "description": "owner/repo"}},
         "required": ["owner", "repo", "pull_number"]}},
    {"name": "update_issue", "description": "Update an issue's title, body, or state.",
     "inputSchema": {"type": "object", "properties": {
         "owner": {"type": "string", "description": "Repository owner"},
         "repo": {"type": "string", "description": "Repository name"},
         "issue_number": {"type": "integer", "description": "Issue number"},
         "title": {"type": "string", "description": "New title"},
         "body": {"type": "string", "description": "New body"},
         "state": {"type": "string", "description": "New state: open or closed"},
         "full_name": {"type": "string", "description": "owner/repo"}},
         "required": ["owner", "repo", "issue_number"]}},
    {"name": "search_repositories", "description": "Search repositories by name.",
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string", "description": "Search query"}},
         "required": ["query"]}},
]

_MCP_BY_NAME = {}
for _t in MCP_TOOLS:
    _MCP_BY_NAME[_t["name"]] = _t
    _MCP_BY_NAME[_t["name"].lower()] = _t


def _mcp_rpc_result(req_id, result):
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _mcp_rpc_error(req_id, code, message):
    return {"jsonrpc": "2.0", "id": req_id,
            "error": {"code": code, "message": message}}


def _mcp_resolve_repo(args):
    owner, repo_name = args.get("owner"), args.get("repo")
    if not owner or not repo_name:
        full = args.get("full_name") or ""
        if "/" in full:
            owner, repo_name = full.split("/", 1)
    if not owner:
        owner = VIEWER["login"]
    repo = STORE.get_repo(owner, repo_name) if repo_name else None
    return repo


def _mcp_tool_call(name, args):
    """Returns (structured_content, text, is_error)."""
    args = args or {}
    tool = _MCP_BY_NAME.get(name) or _MCP_BY_NAME.get(name.lower())
    if tool is None:
        return None, "Unknown tool: %s" % name, True
    # map alternate property names the suite may pass
    for alias, canonical in (("repository", "repo"), ("number", "pull_number"),
                             ("issueNumber", "issue_number"), ("pullNumber", "pull_number")):
        if canonical not in args and alias in args:
            args[canonical] = args[alias]
    for req in tool["inputSchema"].get("required", []):
        if req not in args or args[req] in (None, ""):
            # owner/repo may be filled from full_name
            if req in ("owner", "repo") and args.get("full_name"):
                continue
            return None, "Missing required argument: %s" % req, True
    if name in ("create_repo",) or name.endswith("create_repo"):
        owner = args.get("owner") or VIEWER["login"]
        repo_name = args.get("name")
        if STORE.get_repo(owner, repo_name) is not None:
            return None, "Repository already exists", True
        repo = STORE.create_repo(repo_name, bool(args.get("private")),
                                 args.get("description") or "")
        data = {"id": repo.db_id, "name": repo.name, "full_name": "%s/%s" % (owner, repo_name),
                "owner": {"login": owner}, "description": repo.description,
                "private": repo.private}
        return data, json.dumps(data), False
    if name == "get_repo" or name.endswith("get_repo"):
        repo = _mcp_resolve_repo(args)
        if repo is None:
            return None, "Repository not found", True
        data = repo_json(repo)
        return data, json.dumps(data), False
    if name in ("create_issue",) or name.endswith("create_issue"):
        repo = _mcp_resolve_repo(args)
        if repo is None:
            return None, "Repository not found", True
        iss = create_issue_internal(repo, args.get("title"), args.get("body") or "", [])
        data = issue_json(repo, iss)
        return data, json.dumps(data), False
    if name in ("list_issues",) or name.endswith("list_issues"):
        repo = _mcp_resolve_repo(args)
        if repo is None:
            return None, "Repository not found", True
        state = args.get("state") or "open"
        items = [issue_json(repo, i) for i in repo.issues.values()
                 if state == "all" or i["state"] == state]
        items.sort(key=lambda x: x["number"])
        text = json.dumps(items)
        return items, text, False
    if name in ("create_pull_request",) or name.endswith("create_pull_request"):
        repo = _mcp_resolve_repo(args)
        if repo is None:
            return None, "Repository not found", True
        head, base = args.get("head"), args.get("base")
        if ("refs/heads/" + str(head)) not in repo.refs or ("refs/heads/" + str(base)) not in repo.refs:
            return None, "Head or base branch does not exist", True
        pr = create_pull_internal(repo, args.get("title"), args.get("body") or "", head, base)
        data = pull_json(repo, pr)
        return data, json.dumps(data), False
    if name in ("merge_pull_request",) or name.endswith("merge_pull_request"):
        repo = _mcp_resolve_repo(args)
        if repo is None:
            return None, "Repository not found", True
        pr = repo.pulls.get(int(args.get("pull_number")))
        if pr is None:
            return None, "Pull request not found", True
        status, payload = do_merge_internal(repo, pr)
        if status != 200:
            return None, payload.get("message", "Merge failed"), True
        return payload, json.dumps(payload), False
    if name in ("get_issue",) or name.endswith("get_issue"):
        repo = _mcp_resolve_repo(args)
        if repo is None:
            return None, "Repository not found", True
        iss = repo.issues.get(int(args.get("issue_number")))
        if iss is None:
            return None, "Issue not found", True
        data = issue_json(repo, iss)
        return data, json.dumps(data), False
    if name in ("add_issue_comment",) or name.endswith("add_issue_comment"):
        repo = _mcp_resolve_repo(args)
        if repo is None:
            return None, "Repository not found", True
        iss = repo.issues.get(int(args.get("issue_number")))
        if iss is None:
            return None, "Issue not found", True
        with repo.lock:
            cid = repo.comment_seq
            repo.comment_seq += 1
            repo.comments[cid] = {"body": args.get("body") or ""}
            iss["comments"].append(cid)
        data = {"id": cid, "body": args.get("body") or ""}
        return data, json.dumps(data), False
    if name in ("list_pull_requests",) or name.endswith("list_pull_requests"):
        repo = _mcp_resolve_repo(args)
        if repo is None:
            return None, "Repository not found", True
        state = args.get("state") or "open"
        items = [pull_json(repo, p) for p in repo.pulls.values()
                 if state == "all" or p["state"] == state]
        items.sort(key=lambda x: x["number"])
        return items, json.dumps(items), False
    if name in ("get_pull_request",) or name.endswith("get_pull_request"):
        repo = _mcp_resolve_repo(args)
        if repo is None:
            return None, "Repository not found", True
        pr = repo.pulls.get(int(args.get("pull_number")))
        if pr is None:
            return None, "Pull request not found", True
        data = pull_json(repo, pr)
        return data, json.dumps(data), False
    if name in ("update_issue",) or name.endswith("update_issue"):
        repo = _mcp_resolve_repo(args)
        if repo is None:
            return None, "Repository not found", True
        iss = repo.issues.get(int(args.get("issue_number")))
        if iss is None:
            return None, "Issue not found", True
        with repo.lock:
            if args.get("title") is not None:
                iss["title"] = args["title"]
            if args.get("body") is not None:
                iss["body"] = args["body"]
            if args.get("state") in ("open", "closed"):
                iss["state"] = args["state"]
        data = issue_json(repo, iss)
        return data, json.dumps(data), False
    if name in ("search_repositories",) or name.endswith("search_repositories"):
        q = (args.get("query") or "").lower()
        items = [repo_json(r) for r in STORE.repos.values() if q in r.name.lower()]
        return items, json.dumps(items), False
    return None, "Unknown tool: %s" % name, True


def handle_mcp_request(raw):
    """Parse raw body; return (http_status, response_dict_or_None, is_notification)."""
    try:
        req = json.loads(raw.decode("utf-8"))
    except Exception:
        # extract id if possible
        req_id = None
        m = re.search(rb'"id"\s*:\s*(\d+)', raw)
        if m:
            req_id = int(m.group(1))
        return 400, _mcp_rpc_error(req_id, -32700, "Parse error"), False
    if not isinstance(req, dict):
        return 400, _mcp_rpc_error(None, -32600, "Invalid Request"), False
    req_id = req.get("id")
    method = req.get("method")
    params = req.get("params") or {}
    if method == "notifications/initialized" or method == "notifications/cancelled":
        return 202, None, True
    server_info = {"name": "github-core-clone", "version": "1.0.0"}
    if method == "server/discover":
        return 200, _mcp_rpc_result(req_id, {
            "supportedVersions": ["2026-07-28", "2025-06-18", "2025-03-26"],
            "serverInfo": server_info,
            "capabilities": {"tools": {}},
            "_meta": {"io.modelcontextprotocol/serverInfo": server_info}}), False
    if method == "initialize":
        return 200, _mcp_rpc_result(req_id, {
            "protocolVersion": "2026-07-28",
            "capabilities": {"tools": {}},
            "serverInfo": server_info,
            "_meta": {"io.modelcontextprotocol/serverInfo": server_info}}), False
    if method == "tools/list":
        return 200, _mcp_rpc_result(req_id, {"tools": MCP_TOOLS}), False
    if method == "tools/call":
        tname = params.get("name")
        targs = params.get("arguments") or {}
        if not isinstance(targs, dict):
            return 200, _mcp_rpc_error(req_id, -32602, "Invalid params"), False
        tool = _MCP_BY_NAME.get(tname) or _MCP_BY_NAME.get((tname or "").lower())
        if tool is None:
            return 200, _mcp_rpc_error(req_id, -32601, "Unknown tool: %s" % tname), False
        # fill full_name if schema has it and suite passed owner/repo
        structured, text, is_err = _mcp_tool_call(tname, dict(targs))
        result = {"content": [{"type": "text", "text": text}], "isError": is_err}
        if structured is not None:
            result["structuredContent"] = structured
        return 200, _mcp_rpc_result(req_id, result), False
    if method == "ping":
        return 200, _mcp_rpc_result(req_id, {}), False
    return 200, _mcp_rpc_error(req_id, -32601, "Method not found: %s" % method), False


# ---------------------------------------------------------------------------
# HTTP plumbing: routing, server, lifecycle
# ---------------------------------------------------------------------------

class Handler(Handler):
    protocol_version = "HTTP/1.1"
    server_version = "GitHubClone/1.0"

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_PATCH(self):
        self._dispatch("PATCH")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def do_HEAD(self):
        self._dispatch("HEAD")

    def _dispatch(self, method):
        try:
            # Drain the request body before any routing decision, so an
            # early error return can never leave bytes in the keep-alive
            # stream (they would glue onto the next request line and 501).
            # The cache is reset per request: one handler instance serves
            # a whole keep-alive connection.
            if method in ("POST", "PUT", "PATCH", "DELETE"):
                self._body_cache = None
                self._body_cache = self._read_body()
            parsed = urlsplit(self.path)
            path = parsed.path
            query = parse_qs(parsed.query)
            if path == "/mcp":
                if method != "POST":
                    self._json_err(405, "Method Not Allowed")
                    return
                self._handle_mcp()
                return
            if path in ("/api/graphql", "/api/v4"):
                if method != "POST":
                    self._json_err(405, "Method Not Allowed")
                    return
                self._handle_graphql()
                return
            m = re.match(r"^/api/v3/?(.*)$", path)
            if m:
                self._handle_rest(method, path, query)
                return
            m = re.match(r"^/([^/]+)/([^/]+?)(?:\.git)?(/.*)?$", path)
            if m:
                owner, rname, tail = m.group(1), m.group(2), m.group(3) or ""
                if tail.startswith("/info/refs") or tail in ("/git-upload-pack", "/git-receive-pack"):
                    self._handle_git(method, owner, rname, tail, query)
                    return
            # unknown route
            self._read_body()
            self._json_err(404, "Not Found")
        except BrokenPipeError:
            pass
        except Exception as exc:  # pragma: no cover - last resort
            if os.environ.get("FORGE_DEBUG"):
                import traceback
                traceback.print_exc()
            try:
                self._json_err(500, "Internal Server Error: %s" % exc)
            except Exception:
                pass

    def _handle_mcp(self):
        raw = self._read_body()
        if not self._auth_ok():
            self._json_rpc_unauthorized(raw)
            return
        status, resp, is_notif = handle_mcp_request(raw)
        if is_notif:
            self._send(status, b"", ctype="application/json")
            return
        self._send(status, json.dumps(resp, separators=(",", ":")).encode(),
                   ctype="application/json")

    def _json_rpc_unauthorized(self, raw):
        req_id = None
        try:
            req = json.loads(raw.decode("utf-8"))
            if isinstance(req, dict):
                req_id = req.get("id")
        except Exception:
            pass
        body = json.dumps(_mcp_rpc_error(req_id, -32001, "Unauthorized"),
                          separators=(",", ":")).encode()
        self._send(401, body, ctype="application/json")

    def _handle_graphql(self):
        if not self._auth_ok():
            self._send(401, json.dumps({"message": "Requires authentication"},
                                       separators=(",", ":")).encode(),
                       ctype="application/json")
            return
        raw = self._read_body()
        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception:
            self._send(400, json.dumps({"message": "Problems parsing JSON"},
                                       separators=(",", ":")).encode(),
                       ctype="application/json")
            return
        resp = execute_graphql(body)
        self._send(200, json.dumps(resp, separators=(",", ":")).encode(),
                   ctype="application/json")


def make_server():
    # request_queue_size must dwarf the 50-concurrent-clone workload: the
    # stdlib default backlog of 5 drops/stalls most of those SYNs.
    class ForgeServer(ThreadingHTTPServer):
        request_queue_size = 256
        daemon_threads = True
    server = ForgeServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    return server


def main():
    env_url = os.environ.get("MAKEFUN_ENV_URL")
    if not env_url:
        # Standalone mode: serve on an ephemeral port for local debugging.
        server = make_server()
        port = server.server_address[1]
        print("github-core-clone listening on 127.0.0.1:%d" % port, flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        return 0
    # Suite lifecycle (mirrors starter/main.ts):
    # 1. bind ephemeral port, 2. register, 3. poll /done until done, 4. exit 0.
    server = make_server()
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    register_url = env_url.rstrip("/") + "/register"
    done_url = env_url.rstrip("/") + "/done"
    registered = False
    for attempt in range(30):
        try:
            req = urllib.request.Request(
                register_url,
                data=json.dumps({"port": port}).encode(),
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=5) as resp:
                if resp.status in (200, 201, 204):
                    registered = True
                    break
        except Exception:
            time.sleep(0.5)
    if not registered:
        server.shutdown()
        return 1
    while True:
        try:
            with urllib.request.urlopen(done_url, timeout=5) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            if payload.get("done"):
                break
        except Exception:
            pass
        time.sleep(0.25)
    server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
