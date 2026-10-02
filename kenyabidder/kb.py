"""Knowledge bases: admins add documents; agents are assigned KBs and search them via a tool.

Retrieval is dependency-free BM25 over paragraph chunks. Document text is *data*, never instructions —
the agent loop wraps every retrieved snippet as untrusted content.
"""
from __future__ import annotations

import html.parser
import ipaddress
import math
import os
import re
import socket
import uuid
from collections import Counter
from urllib.parse import urlparse

import httpx

from .errors import bad, conflict, not_found

MAX_DOC_CHARS = 500_000
MAX_FETCH_BYTES = 2_000_000
CHUNK_CHARS = 900
STOP = set("a an and are as at be but by for if in into is it no not of on or such that the their then there these they this to was will with".split())
ROLES = ["BIDDER", "SELLER"]


def tokenize(text: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9]+", text.lower()) if t not in STOP and len(t) > 1]


def chunk_text(text: str, size: int = CHUNK_CHARS) -> list[str]:
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks, cur = [], ""
    for p in paras:
        if len(p) > size:  # long paragraph: split on sentence-ish boundaries
            for piece in re.split(r"(?<=[.!?])\s+", p):
                while len(piece) > size:
                    chunks.append(piece[:size])
                    piece = piece[size:]
                if len(cur) + len(piece) + 1 > size and cur:
                    chunks.append(cur)
                    cur = ""
                cur = f"{cur} {piece}".strip()
            continue
        if len(cur) + len(p) + 2 > size and cur:
            chunks.append(cur)
            cur = ""
        cur = f"{cur}\n\n{p}".strip()
    if cur:
        chunks.append(cur)
    return chunks


class _Strip(html.parser.HTMLParser):
    SKIP = {"script", "style", "noscript", "head"}

    def __init__(self):
        super().__init__()
        self.out, self._skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        elif tag in ("p", "div", "br", "li", "h1", "h2", "h3", "tr"):
            self.out.append("\n\n" if tag != "br" else "\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.out.append(data)


def html_to_text(raw: str) -> str:
    p = _Strip()
    p.feed(raw)
    return re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]+", " ", "".join(p.out))).strip()


def _check_public_url(url: str) -> None:
    """SSRF guard: only http(s) to public addresses unless KENYABIDDER_ALLOW_PRIVATE_FETCH=1."""
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise bad("INVALID_URL", "only http(s) URLs are supported")
    if os.environ.get("KENYABIDDER_ALLOW_PRIVATE_FETCH") == "1":
        return
    try:
        infos = socket.getaddrinfo(u.hostname, u.port or (443 if u.scheme == "https" else 80), type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise bad("FETCH_FAILED", f"cannot resolve {u.hostname}") from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            raise bad("URL_BLOCKED", "URL resolves to a private/internal address")


class KnowledgeBaseService:
    def __init__(self, store, clock):
        self.store, self.clock = store, clock

    # ---------- KBs ----------

    def create(self, name: str, description: str = "", roles: list[str] | None = None) -> dict:
        if not name.strip():
            raise bad("INVALID_KB", "name is required")
        roles = roles or list(ROLES)
        if any(r not in ROLES for r in roles):
            raise bad("INVALID_KB", "roles must be a subset of BIDDER, SELLER")
        if any(k["name"].lower() == name.strip().lower() for k in self.store.kbs.values()):
            raise conflict("NAME_TAKEN", f"a knowledge base named {name!r} already exists")
        kb = {"id": str(uuid.uuid4()), "name": name.strip(), "description": description, "roles": roles,
              "docs": {}, "enabled": True, "created_at": self.clock.now()}
        self.store.kbs[kb["id"]] = kb
        return kb

    def update(self, kb_id: str, **patch) -> dict:
        kb = self.get(kb_id)
        for k in ("name", "description", "roles", "enabled"):
            if k in patch and patch[k] is not None:
                kb[k] = patch[k]
        return kb

    def remove(self, kb_id: str) -> None:
        self.get(kb_id)
        users = [a["agent_id"][:6] for a in self.store.agents.values() if kb_id in a.get("config", {}).get("kb_ids", [])]
        if users:
            raise conflict("IN_USE", f"knowledge base is assigned to agent(s): {', '.join(users)} — unassign it first")
        del self.store.kbs[kb_id]

    def get(self, kb_id: str) -> dict:
        kb = self.store.kbs.get(kb_id)
        if not kb:
            raise not_found("KB_NOT_FOUND", "knowledge base not found")
        return kb

    def list(self, *, role: str | None = None, enabled_only: bool = False) -> list[dict]:
        return [k for k in sorted(self.store.kbs.values(), key=lambda x: x["name"].lower())
                if (not role or role in k["roles"]) and (not enabled_only or k["enabled"])]

    # ---------- documents ----------

    def add_text(self, kb_id: str, title: str, text: str, source: str = "pasted") -> dict:
        kb = self.get(kb_id)
        text = (text or "").strip()
        if not title.strip():
            raise bad("INVALID_DOC", "title is required")
        if not text:
            raise bad("INVALID_DOC", "document is empty")
        if len(text) > MAX_DOC_CHARS:
            raise bad("DOC_TOO_LARGE", f"document exceeds {MAX_DOC_CHARS} characters")
        doc = {"id": str(uuid.uuid4()), "title": title.strip(), "source": source, "chars": len(text),
               "chunks": chunk_text(text), "added_at": self.clock.now()}
        kb["docs"][doc["id"]] = doc
        return doc

    def add_file(self, kb_id: str, filename: str, data: bytes) -> dict:
        name = filename or "upload"
        if name.lower().endswith(".pdf"):
            try:
                import io

                from pypdf import PdfReader  # optional dependency
            except ImportError as e:
                raise bad("PDF_UNSUPPORTED", "PDF support needs `pip install pypdf`; upload .txt/.md/.csv/.json or paste the text") from e
            text = "\n\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(data)).pages)
        else:
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                text = data.decode("latin-1")
            if name.lower().endswith((".html", ".htm")):
                text = html_to_text(text)
        return self.add_text(kb_id, name, text, source=f"file:{name}")

    async def add_url(self, kb_id: str, url: str, title: str = "", client: httpx.AsyncClient | None = None) -> dict:
        _check_public_url(url)
        own = client is None
        client = client or httpx.AsyncClient(timeout=15, follow_redirects=False)
        try:
            cur = url
            for _ in range(4):  # follow redirects manually so each hop is re-checked
                _check_public_url(cur)
                async with client.stream("GET", cur, headers={"user-agent": "KenyaBidder-KB/0.2"}) as r:
                    if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                        cur = str(httpx.URL(cur).join(r.headers["location"]))
                        continue
                    if r.status_code >= 400:
                        raise bad("FETCH_FAILED", f"HTTP {r.status_code}")
                    buf = bytearray()
                    async for c in r.aiter_bytes():
                        buf += c
                        if len(buf) > MAX_FETCH_BYTES:
                            raise bad("DOC_TOO_LARGE", "page exceeds 2MB")
                    ctype = r.headers.get("content-type", "")
                    break
            else:
                raise bad("FETCH_FAILED", "too many redirects")
        except httpx.HTTPError as e:
            raise bad("FETCH_FAILED", str(e)[:200]) from e
        finally:
            if own:
                await client.aclose()
        raw = bytes(buf).decode("utf-8", errors="replace")
        text = html_to_text(raw) if "html" in ctype or raw.lstrip().startswith("<") else raw
        return self.add_text(kb_id, title or url, text, source=url)

    def remove_doc(self, kb_id: str, doc_id: str) -> None:
        kb = self.get(kb_id)
        if doc_id not in kb["docs"]:
            raise not_found("DOC_NOT_FOUND", "document not found")
        del kb["docs"][doc_id]

    # ---------- retrieval (BM25) ----------

    def search(self, query: str, kb_ids: list[str] | None = None, k: int = 4) -> list[dict]:
        q = tokenize(query)
        if not q:
            return []
        entries = []
        for kb in self.store.kbs.values():
            if not kb["enabled"] or (kb_ids is not None and kb["id"] not in kb_ids):
                continue
            for doc in kb["docs"].values():
                for i, ch in enumerate(doc["chunks"]):
                    entries.append((kb, doc, i, ch, tokenize(ch)))
        if not entries:
            return []
        n = len(entries)
        avg = sum(len(e[4]) for e in entries) / n or 1
        df: Counter = Counter()
        for e in entries:
            df.update(set(e[4]))
        scored = []
        for kb, doc, i, ch, toks in entries:
            tf = Counter(toks)
            s = 0.0
            for term in set(q):
                if term not in tf:
                    continue
                idf = math.log(1 + (n - df[term] + 0.5) / (df[term] + 0.5))
                s += idf * tf[term] * 2.2 / (tf[term] + 1.2 * (0.25 + 0.75 * len(toks) / avg))
            if s > 0:
                scored.append((s, kb, doc, i, ch))
        scored.sort(key=lambda x: -x[0])
        return [{"kb_id": kb["id"], "kb": kb["name"], "doc_id": doc["id"], "title": doc["title"], "chunk": i,
                 "score": round(s, 3), "text": ch} for s, kb, doc, i, ch in scored[:k]]
