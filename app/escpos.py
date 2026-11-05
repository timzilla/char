"""Char: the printer side. Turns a document (a list of simple blocks, or Markdown) into ESC/POS bytes for an
80 mm receipt printer (576 dots, 48 columns in font A), talks to the printer over raw TCP port 9100, and asks
it for status without printing anything. Written against the generic Xprinter-class boards: plain ESC/POS,
GS v 0 raster images, GS ( k QR codes, CP437 text."""
import base64, io, re, socket, time
from PIL import Image, ImageOps

ESC, GS, DLE = b"\x1b", b"\x1d", b"\x10"
WIDTH_DOTS = 576          # 80 mm head
COLS = 48                 # font A, 12 dots wide
COLS_SMALL = 64           # font B, 9 dots wide

# ---------------------------------------------------------------- the printer

class PrinterStatus(dict):
    @property
    def ok(self): return self.get("online") and not self.get("paper_out") and not self.get("cover_open") and not self.get("error")


def status(host, port=9100, timeout=3.0):
    """DLE EOT queries. Never prints. Returns a PrinterStatus; 'reachable' False when the socket won't open."""
    st = PrinterStatus(reachable=False, online=False, paper_out=False, paper_low=False, cover_open=False, error=False, raw={})
    try:
        s = socket.create_connection((host, port), timeout=timeout)
    except OSError as e:
        st["detail"] = str(e); return st
    st["reachable"] = True
    try:
        s.settimeout(2.0)
        for n in (1, 2, 3, 4):
            s.sendall(DLE + b"\x04" + bytes([n]))
            try:
                b = s.recv(1)
            except OSError:
                b = b""
            st["raw"][n] = b[0] if b else None
        s.close()
    except OSError as e:
        st["detail"] = str(e); return st
    r = st["raw"]
    if r.get(1) is not None:
        st["online"] = not (r[1] & 0x08)
    if r.get(2) is not None:
        st["cover_open"] = bool(r[2] & 0x04); st["paper_out"] = st["paper_out"] or bool(r[2] & 0x20)
    if r.get(3) is not None:
        st["error"] = bool(r[3] & 0x08) or bool(r[3] & 0x20) or bool(r[3] & 0x40)
    if r.get(4) is not None:
        st["paper_low"] = bool(r[4] & 0x0C); st["paper_out"] = st["paper_out"] or bool(r[4] & 0x60)
    if r.get(1) is None and r.get(4) is None:   # a board that answers nothing: assume fine, the print will tell
        st["online"] = True; st["silent"] = True
    return st


def send(host, data, port=9100, timeout=10.0, chunk=4096):
    """Push bytes to the printer in small chunks so a cheap board's buffer never overflows."""
    s = socket.create_connection((host, port), timeout=timeout)
    try:
        for i in range(0, len(data), chunk):
            s.sendall(data[i:i + chunk]); time.sleep(0.01)
        time.sleep(0.2)
    finally:
        s.close()


# ---------------------------------------------------------------- text helpers

SUBS = {"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "--", "…": "...", " ": " ",
        "•": "\xf9", "→": "->", "←": "<-", "↑": "^", "↓": "v", "°": "\xf8", "✓": "v", "✔": "v", "×": "x", "−": "-",
        "★": "*", "☆": "*", "♥": "\x03", "½": "1/2", "¼": "1/4", "¾": "3/4", "⅓": "1/3", "⅔": "2/3", "⅛": "1/8"}

def enc(s):
    s = "".join(SUBS.get(c, c) for c in str(s))
    return s.encode("cp437", errors="replace")


def wrap(text, cols, indent=0, first_indent=None):
    """Word-wrap to cols, honouring explicit newlines; returns lines (strings)."""
    out = []
    fi = indent if first_indent is None else first_indent
    for para in str(text).split("\n"):
        words = para.split(" ")
        line, ind = "", fi if not out else indent
        if not para.strip():
            out.append(""); continue
        for w in words:
            while len(w) > cols - ind:                       # a word longer than the line
                if line: out.append(" " * ind + line); line = ""; ind = indent
                out.append(" " * ind + w[:cols - ind]); w = w[cols - ind:]
            if not line: line = w
            elif len(line) + 1 + len(w) <= cols - ind: line += " " + w
            else: out.append(" " * ind + line); line = w; ind = indent
        out.append(" " * ind + line)
    return out


# ---------------------------------------------------------------- the builder

class Builder:
    """Accumulates ESC/POS bytes. Blocks keep state simple: every block resets size and alignment."""
    def __init__(self):
        self.b = bytearray(ESC + b"@" + ESC + b"t\x00" + ESC + b"R\x00")   # init, CP437, USA charset
        self.lines = 0

    # low level
    def raw(self, x): self.b += x; return self
    def align(self, a): self.b += ESC + b"a" + bytes([{"left": 0, "center": 1, "right": 2}.get(a, 0)]); return self
    def bold(self, on): self.b += ESC + b"E" + (b"\x01" if on else b"\x00"); return self
    def size(self, w=1, h=1): self.b += GS + b"!" + bytes([((w - 1) << 4) | (h - 1)]); return self
    def font(self, small): self.b += ESC + b"M" + (b"\x01" if small else b"\x00"); return self
    def underline(self, on): self.b += ESC + b"-" + (b"\x01" if on else b"\x00"); return self
    def invert(self, on): self.b += GS + b"B" + (b"\x01" if on else b"\x00"); return self
    def feed(self, n=1): self.b += ESC + b"d" + bytes([max(0, min(255, n))]); return self
    def nl(self, n=1): self.b += b"\n" * n; self.lines += n; return self
    def reset(self): self.size(1, 1); self.bold(False); self.font(False); self.underline(False); self.invert(False); self.align("left"); return self

    def text_inline(self, s):
        """**bold** and _underline_ markers inside a line."""
        for part in re.split(r"(\*\*[^*]+\*\*|__[^_]+__)", s):
            if part.startswith("**") and part.endswith("**"): self.bold(True); self.b += enc(part[2:-2]); self.bold(False)
            elif part.startswith("__") and part.endswith("__"): self.underline(True); self.b += enc(part[2:-2]); self.underline(False)
            else: self.b += enc(part)
        return self

    def plain(self, s): self.b += enc(s); return self

    # blocks
    def title(self, s):      # big, bold, centred, two lines tall
        self.reset().align("center").bold(True).size(2, 2)
        for ln in wrap(s, COLS // 2): self.plain(ln).nl()
        return self.reset()

    def heading(self, s):    # bold, double height
        self.reset().bold(True).size(1, 2)
        for ln in wrap(s, COLS): self.plain(ln).nl()
        return self.reset()

    def subheading(self, s):
        self.reset().bold(True).underline(True)
        for ln in wrap(s.upper(), COLS): self.plain(ln).nl()
        return self.reset()

    def para(self, s, align="left", small=False):
        self.reset().align(align).font(small)
        for ln in wrap(s, COLS_SMALL if small else COLS): self.text_inline(ln).nl()
        return self.reset()

    def quote(self, s):
        self.reset().font(True)
        for ln in wrap(s, COLS_SMALL - 3, indent=3, first_indent=3): self.plain('"' + ln[3:] if False else ln).nl()
        return self.reset()

    def small(self, s, align="left"): return self.para(s, align, small=True)

    def hr(self, ch="-"): return self.reset().plain(ch * COLS).nl()

    def kv(self, rows, small=False):
        """Two columns: key left, value right, dot leaders when there's room."""
        self.reset().font(small); cols = COLS_SMALL if small else COLS
        for k, v in rows:
            k, v = str(k), str(v)
            if len(k) + len(v) + 2 <= cols:
                self.text_inline(k + " " + "." * (cols - len(k) - len(v) - 2) + " ").bold(True).plain(v).bold(False).nl()
            else:
                self.text_inline(k).nl(); self.align("right").bold(True).plain(v).bold(False).nl().align("left")
        return self.reset()

    def table(self, rows, header=True, small=True):
        """Columns sized to content, squeezed to fit; small font by default so more fits."""
        if not rows: return self
        self.reset().font(small); cols = COLS_SMALL if small else COLS
        n = max(len(r) for r in rows); rows = [[str(c) for c in r] + [""] * (n - len(r)) for r in rows]
        w = [max(len(r[i]) for r in rows) for i in range(n)]
        gap = 1; total = sum(w) + gap * (n - 1)
        while total > cols:                       # trim the widest column
            i = w.index(max(w)); w[i] -= 1; total -= 1
        for ri, r in enumerate(rows):
            line = (" " * gap).join((c[:w[i]].ljust(w[i]) if i == 0 else c[:w[i]].rjust(w[i])) for i, c in enumerate(r))
            if header and ri == 0: self.bold(True).plain(line).bold(False).nl(); self.plain("-" * min(cols, total)).nl()
            else: self.plain(line).nl()
        return self.reset()

    def bullets(self, items, box=False, numbered=False):
        self.reset()
        for i, it in enumerate(items, 1):
            mark = f"{i:>2}. " if numbered else ("[ ] " if box else "\xf9 ")   # cp437 bullet
            lines = wrap(str(it), COLS - len(mark))
            self.plain(mark).text_inline(lines[0]).nl()
            for ln in lines[1:]: self.plain(" " * len(mark)).text_inline(ln).nl()
        return self

    def qr(self, data, size=6, label=None):
        data = str(data).encode("utf-8")[:700]
        self.reset().align("center")
        self.b += GS + b"(k\x04\x00\x31\x41\x32\x00"                        # model 2
        self.b += GS + b"(k\x03\x00\x31\x43" + bytes([max(2, min(12, size))])  # module size
        self.b += GS + b"(k\x03\x00\x31\x45\x31"                             # error correction M
        n = len(data) + 3
        self.b += GS + b"(k" + bytes([n & 255, n >> 8]) + b"\x31\x50\x30" + data   # store
        self.b += GS + b"(k\x03\x00\x31\x51\x30"                             # print
        self.nl()
        if label: self.small(label, "center")
        return self.reset()

    def image(self, im, width=WIDTH_DOTS, align="center", dither=True):
        """A PIL image (or bytes/base64 of one) as GS v 0 raster, scaled to width, Floyd-Steinberg dithered."""
        if isinstance(im, (bytes, bytearray)): im = Image.open(io.BytesIO(im))
        elif isinstance(im, str): im = Image.open(io.BytesIO(base64.b64decode(im.split(",", 1)[-1])))
        im = ImageOps.exif_transpose(im)
        if im.mode in ("RGBA", "LA", "P"):                       # flatten transparency onto white
            bg = Image.new("RGB", im.size, (255, 255, 255)); bg.paste(im.convert("RGBA"), mask=im.convert("RGBA").split()[-1]); im = bg
        width = min(WIDTH_DOTS, int(width))
        im = im.convert("L")
        if im.width != width: im = im.resize((width, max(1, round(im.height * width / im.width))), Image.LANCZOS)
        im = ImageOps.autocontrast(im, cutoff=1)
        bw = im.convert("1", dither=Image.FLOYDSTEINBERG if dither else Image.NONE)
        bw = ImageOps.invert(bw.convert("L")).convert("1", dither=Image.NONE)   # 1 = black for the printer
        wb = (bw.width + 7) // 8
        self.reset().align(align)
        for y0 in range(0, bw.height, 200):                      # bands, so the board's buffer keeps up
            band = bw.crop((0, y0, bw.width, min(bw.height, y0 + 200)))
            h = band.height
            self.b += GS + b"v0\x00" + bytes([wb & 255, wb >> 8, h & 255, h >> 8]) + band.tobytes()
        return self.reset()

    def cut(self, feed=3): self.feed(feed); self.b += GS + b"V\x42\x00"; return self   # partial cut with feed

    def bytes(self): return bytes(self.b)


# ---------------------------------------------------------------- documents

def render(doc):
    """doc = {"title": str?, "blocks": [...], "cut": bool, "feed": int}. Block types:
    title, h1 (heading), h2 (subheading), p/text (text, align, small), small, quote, hr (ch), kv (rows), table (rows, header),
    list (items), checklist (items), numbered (items), qr (data, label, size), image (b64|png bytes, width), feed (n), raw (b64)."""
    B = Builder()
    if doc.get("title"): B.title(doc["title"])
    for blk in doc.get("blocks", []):
        t = blk.get("t") or blk.get("type")
        if t == "title": B.title(blk["text"])
        elif t == "h1": B.heading(blk["text"])
        elif t == "h2": B.subheading(blk["text"])
        elif t in ("p", "text"): B.para(blk["text"], blk.get("align", "left"), blk.get("small", False))
        elif t == "small": B.small(blk["text"], blk.get("align", "left"))
        elif t == "quote": B.quote(blk["text"])
        elif t == "hr": B.hr(blk.get("ch", "-"))
        elif t == "kv": B.kv(blk["rows"], blk.get("small", False))
        elif t == "table": B.table(blk["rows"], blk.get("header", True), blk.get("small", True))
        elif t == "list": B.bullets(blk["items"])
        elif t == "checklist": B.bullets(blk["items"], box=True)
        elif t == "numbered": B.bullets(blk["items"], numbered=True)
        elif t == "qr": B.qr(blk["data"], blk.get("size", 6), blk.get("label"))
        elif t == "image": B.image(blk.get("b64") or blk.get("png"), blk.get("width", WIDTH_DOTS), blk.get("align", "center"), blk.get("dither", True))
        elif t == "feed": B.feed(int(blk.get("n", 1)))
        elif t == "raw": B.raw(base64.b64decode(blk["b64"]))
    if doc.get("cut", True): B.cut(doc.get("feed", 3))
    else: B.feed(doc.get("feed", 1))
    return B.bytes()


def markdown_to_blocks(md):
    """A small Markdown: # title, ## heading, ### subheading, paragraphs, - bullets, - [ ] checklists, 1. numbered,
    > quotes, --- rules, | tables |, `qr: data` lines, `kv:` lines of  key: value  pairs, ![alt](data:image/...;base64,...)."""
    blocks, para, lst, kind, table, kv = [], [], [], None, [], []
    def flush():
        nonlocal para, lst, kind, table, kv
        if para: blocks.append({"t": "p", "text": " ".join(para)}); para = []
        if lst: blocks.append({"t": kind, "items": lst}); lst, kind = [], None
        if table: blocks.append({"t": "table", "rows": table}); table = []
        if kv: blocks.append({"t": "kv", "rows": kv}); kv = []
    for raw in str(md).replace("\r", "").split("\n"):
        ln = raw.rstrip()
        s = ln.strip()
        if not s: flush(); continue
        m = re.match(r"^(#{1,3})\s+(.*)", s)
        if m: flush(); blocks.append({"t": {1: "title", 2: "h1", 3: "h2"}[len(m.group(1))], "text": m.group(2)}); continue
        if re.match(r"^(-{3,}|\*{3,}|={3,})$", s): flush(); blocks.append({"t": "hr", "ch": "=" if s[0] == "=" else "-"}); continue
        m = re.match(r"^qr:\s*(.+?)(?:\s*\|\s*(.+))?$", s, re.I)
        if m: flush(); blocks.append({"t": "qr", "data": m.group(1), "label": m.group(2)}); continue
        m = re.match(r"^!\[[^\]]*\]\((data:image/[^)]+)\)", s)
        if m: flush(); blocks.append({"t": "image", "b64": m.group(1)}); continue
        if s.startswith("|") and s.endswith("|"):
            cells = [c.strip() for c in s.strip("|").split("|")]
            if all(re.match(r"^:?-{2,}:?$", c) for c in cells): continue      # the separator row
            if para or lst or kv: flush()
            table.append(cells); continue
        m = re.match(r"^- \[([ xX])\]\s+(.*)", s)
        if m:
            if kind not in (None, "checklist"): flush()
            if para or table or kv: flush()
            kind = "checklist"; lst.append(("[x] " if m.group(1).strip() else "") + m.group(2)); continue
        m = re.match(r"^[-*]\s+(.*)", s)
        if m:
            if kind not in (None, "list"): flush()
            if para or table or kv: flush()
            kind = "list"; lst.append(m.group(1)); continue
        m = re.match(r"^\d+[.)]\s+(.*)", s)
        if m:
            if kind not in (None, "numbered"): flush()
            if para or table or kv: flush()
            kind = "numbered"; lst.append(m.group(1)); continue
        if s.startswith(">"): flush(); blocks.append({"t": "quote", "text": s.lstrip("> ").strip()}); continue
        m = re.match(r"^([^:]{1,24}):\s{2,}(.+)$", s)      # "Tide:   6.2 ft" with two+ spaces = a key/value row
        if m:
            if para or lst or table: flush()
            kv.append([m.group(1), m.group(2)]); continue
        if lst or table or kv: flush()
        para.append(s)
    flush()
    return blocks


def markdown(md, cut=True):
    return render({"blocks": markdown_to_blocks(md), "cut": cut})


def preview_text(doc):
    """A plain-text approximation of a document, for the web UI's preview. Images and QR codes become placeholders."""
    out = []
    if doc.get("title"): out += [ln.center(COLS) for ln in wrap(doc["title"].upper(), COLS // 2)] + [""]
    for blk in doc.get("blocks", []):
        t = blk.get("t") or blk.get("type")
        if t == "title": out += [ln.center(COLS) for ln in wrap(blk["text"].upper(), COLS // 2)]
        elif t == "h1": out += wrap(blk["text"].upper(), COLS)
        elif t == "h2": out += wrap(blk["text"].upper(), COLS)
        elif t in ("p", "text", "small", "quote"):
            lines = wrap(re.sub(r"\*\*|__", "", blk["text"]), COLS_SMALL if (t != "p" and t != "text") or blk.get("small") else COLS)
            a = blk.get("align", "left"); out += [ln.center(COLS) if a == "center" else ln.rjust(COLS) if a == "right" else ln for ln in lines]
        elif t == "hr": out.append(blk.get("ch", "-") * COLS)
        elif t == "kv": out += [f"{k} {'.' * max(1, COLS - len(str(k)) - len(str(v)) - 2)} {v}" for k, v in blk["rows"]]
        elif t == "table": out += [" | ".join(str(c) for c in r) for r in blk["rows"]]
        elif t in ("list", "checklist", "numbered"):
            for i, it in enumerate(blk["items"], 1): out += wrap(("%d. " % i if t == "numbered" else "[ ] " if t == "checklist" else "- ") + str(it), COLS, indent=3)
        elif t == "qr": out += ["[QR] " + str(blk["data"])[:40]] + ([str(blk["label"])] if blk.get("label") else [])
        elif t == "image": out.append("[image]")
        elif t == "feed": out += [""] * int(blk.get("n", 1))
    return "\n".join(out)
