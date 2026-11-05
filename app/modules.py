"""Char modules. Each module is a small class: a name, a blurb, a list of fields (the web UI builds the form from
them, the API and the MCP tool validate against them) and build(params) -> document dict for escpos.render.
Adding a module is adding a class here, in a sibling file, or in a plugin file (see load_plugins) that calls register().
A plugin module with the same name as a built-in one replaces it."""
import contextvars, datetime as dt, glob, importlib.util, json, os, platform, shutil, socket, time, traceback, urllib.parse
import httpx
import config

REGISTRY = {}
TZ = dt.timezone(dt.timedelta(hours=-8))   # replaced at runtime by the local zone (see main.py)


def register(cls):
    REGISTRY[cls.name] = cls(); return cls


# Previews never call Claude: the preview pane rebuilds a slip as you type, and Claude should only run when someone presses Print.
PREVIEW = contextvars.ContextVar("preview", default=False)


def claude_ok():
    """True when a module may call Claude right now: a key is set and this is a real print, not a preview."""
    return bool(os.environ.get("ANTHROPIC_API_KEY")) and not PREVIEW.get()


def now():
    return dt.datetime.now().astimezone()


def stamp(d=None):
    return (d or now()).strftime("%a %b %-d, %-I:%M %p")


class Field(dict):
    """name, label, kind (text|textarea|number|select|checkbox|url), default, options, help, required."""
    def __init__(self, name, label, kind="text", default="", options=None, help="", required=False, placeholder=""):
        super().__init__(name=name, label=label, kind=kind, default=default, options=options or [], help=help, required=required, placeholder=placeholder)


class Module:
    name = "module"; title = "Module"; blurb = ""; icon = "slip"; fields = []; group = "write"
    def hidden(self): return False      # True hides the module everywhere (e.g. it needs a setting that isn't there)
    def clean(self, params):
        out = {}
        for f in self.fields:
            v = params.get(f["name"], f["default"])
            if f["kind"] == "checkbox": v = str(v).lower() in ("1", "true", "on", "yes")
            elif f["kind"] == "number":
                try: v = float(v) if v not in ("", None) else f["default"]
                except ValueError: v = f["default"]
            else: v = ("" if v is None else str(v)).strip()
            if f["required"] and v in ("", None): raise ValueError(f"{f['label']} is required")
            out[f["name"]] = v
        return out
    def build(self, p): raise NotImplementedError
    def describe(self):
        return {"name": self.name, "title": self.title, "blurb": self.blurb, "icon": self.icon, "group": self.group, "fields": self.fields}


def footer(source):
    return [{"t": "feed", "n": 1}, {"t": "small", "text": f"{source} \xb7 {stamp()}", "align": "center"}]


# ---------------------------------------------------------------- write

@register
class Note(Module):
    name = "note"; title = "Note"; blurb = "Type something, print it. Markdown works: # title, - bullets, - [ ] boxes, > quotes, qr: a link."
    icon = "pen"; group = "write"
    fields = [Field("text", "Text", "textarea", required=True, placeholder="# Shopping\n- [ ] eggs\n- [ ] milk\n\nqr: https://example.com | a link"),
              Field("signed", "Sign and date it", "checkbox", default=True)]
    def build(self, p):
        from escpos import markdown_to_blocks
        blocks = markdown_to_blocks(p["text"])
        if p["signed"]: blocks += footer("Char")
        return {"blocks": blocks}


@register
class QR(Module):
    name = "qr"; title = "QR code"; blurb = "A link, Wi-Fi login, phone number or any text as a scannable code, with a caption."
    icon = "qr"; group = "write"
    fields = [Field("kind", "What is it", "select", default="url", options=[["url", "Link"], ["text", "Plain text"], ["wifi", "Wi-Fi network"], ["tel", "Phone number"], ["sms", "Text message"], ["mail", "Email address"]]),
              Field("data", "Link, text, SSID, number or address", "text", required=True),
              Field("extra", "Wi-Fi password / message body", "text", help="Only for Wi-Fi and text message"),
              Field("label", "Caption", "text"), Field("size", "Size", "select", default="7", options=[["5", "Small"], ["7", "Medium"], ["10", "Large"]])]
    def build(self, p):
        k, d, x = p["kind"], p["data"], p["extra"]
        payload = {"url": d if "://" in d else "https://" + d, "text": d, "wifi": f"WIFI:T:WPA;S:{d};P:{x};;", "tel": "tel:" + d, "sms": f"SMSTO:{d}:{x}", "mail": "mailto:" + d}[k]
        blocks = []
        if p["label"]: blocks.append({"t": "h1", "text": p["label"]})
        blocks.append({"t": "qr", "data": payload, "size": int(float(p["size"])), "label": d if k != "text" else None})
        if k == "wifi": blocks.append({"t": "kv", "rows": [["Network", d], ["Password", x]]})
        return {"blocks": blocks, "feed": 2}


@register
class Markdown(Module):
    name = "markdown"; title = "Document"; blurb = "The full Markdown treatment: headings, tables, key: value rows, images. What apps and Claude send."
    icon = "doc"; group = "write"
    fields = [Field("text", "Markdown", "textarea", required=True), Field("cut", "Cut after", "checkbox", default=True)]
    def build(self, p):
        from escpos import markdown_to_blocks
        return {"blocks": markdown_to_blocks(p["text"]), "cut": p["cut"]}


@register
class Ask(Module):
    name = "ask"; title = "Ask"; blurb = "Ask Claude for a slip: a list, a plan, a poem, a fact, an answer. It writes for the printer and Char prints it."
    icon = "ask"; group = "write"
    fields = [Field("prompt", "Ask for", "textarea", required=True, placeholder="A packing list for two nights of camping in November\nA limerick about the dog\nHow long to roast a 5 lb chicken at 425"),
              Field("length", "Length", "select", default="short", options=[["short", "Short, one hand-span"], ["medium", "Medium"], ["long", "Long, the whole story"]]),
              Field("signed", "Sign and date it", "checkbox", default=True)]
    MODEL = "claude-opus-5-5"
    SYSTEM = (
        "You write short documents for a kitchen receipt printer (80 mm thermal paper, 48 characters per line, black text only). "
        "Reply with only the document, no preamble and no closing remark. Use this small Markdown and nothing else: "
        "'# title' once at the top (short, it prints double-size), '## heading' for sections, plain paragraphs, '- bullet', '- [ ] checklist item', "
        "'1. numbered step', '> a quote', '---' rule, '| a | b |' tables with a header row (narrow: 2-3 columns, short cells), "
        "'Key:  value' rows with two spaces after the colon for facts and figures, and 'qr: https://...' on its own line only when a real link helps. "
        "No images, no links in text, no HTML, no emoji, no nested lists, no **bold** inside headings. Keep lines short; the paper is narrow. "
        "Lists and checklists beat paragraphs. Be concrete and useful; if the request is a question, answer it first, then add what helps. "
        "Length: {length}."
    )
    LENGTHS = {"short": "about 60-120 words, one hand-span of paper", "medium": "about 150-300 words", "long": "up to 600 words"}
    def build(self, p):
        if not os.environ.get("ANTHROPIC_API_KEY"): raise ValueError("Ask needs an Anthropic API key (ANTHROPIC_API_KEY)")
        if PREVIEW.get():
            return {"blocks": [{"t": "title", "text": "Ask"}, {"t": "p", "text": "Claude writes this slip when you press Print. Previews don't ask, so they cost nothing."},
                               {"t": "small", "text": "Asked: " + p["prompt"].strip().replace("\n", " ")[:120]}]}
        import anthropic
        from escpos import markdown_to_blocks
        client = anthropic.Anthropic(max_retries=2, timeout=120)
        r = client.beta.messages.create(
            model=self.MODEL, max_tokens=4000, output_config={"effort": "low"},
            betas=["server-side-fallback-2026-07-01"], fallbacks="default",
            system=self.SYSTEM.format(length=self.LENGTHS.get(p["length"], self.LENGTHS["short"])),
            messages=[{"role": "user", "content": p["prompt"]}])
        if r.stop_reason == "refusal": raise ValueError("Claude declined to write that one")
        text = "\n".join(b.text for b in r.content if b.type == "text").strip()
        if not text: raise ValueError("Claude sent nothing back")
        blocks = markdown_to_blocks(text)
        if p["signed"]: blocks += [{"t": "feed", "n": 1}, {"t": "small", "text": "Asked: " + p["prompt"].strip().replace("\n", " ")[:120]}] + footer("Claude \xb7 " + r.model)
        return {"blocks": blocks}


# ---------------------------------------------------------------- around the house

@register
class Recipe(Module):
    name = "recipe"; title = "Recipe"; blurb = "A recipe from Pixel Pantry: ingredients with grams, the steps, a code back to the page."
    icon = "pan"; group = "house"
    fields = [Field("slug", "Recipe", "select", required=True, options=[]), Field("servings", "Servings", "number", default="", help="Blank keeps the recipe's own"),
              Field("steps", "Include the steps", "checkbox", default=True), Field("grams", "Show grams", "checkbox", default=True)]
    KITCHEN, PUBLIC = config.PANTRY_URL, config.PANTRY_PUBLIC
    def hidden(self): return not self.KITCHEN
    def options(self):
        try:
            r = httpx.get(self.KITCHEN + "/api/recipes", timeout=5)
            if r.status_code == 200: return [[x["slug"], x["title"]] for x in r.json()]
        except Exception: pass
        return []
    def describe(self):
        d = super().describe(); d["fields"] = [dict(f, options=self.options()) if f["name"] == "slug" else f for f in self.fields]; return d
    @staticmethod
    def qty(q):
        if q is None: return ""
        for n, s in ((0.25, "1/4"), (0.333, "1/3"), (0.5, "1/2"), (0.667, "2/3"), (0.75, "3/4")):
            if abs(q - int(q) - n) < 0.02: return (str(int(q)) + " " if int(q) else "") + s
        return str(int(q)) if abs(q - round(q)) < 0.01 else f"{q:.2g}"
    def build(self, p):
        r = httpx.get(f"{self.KITCHEN}/r/{p['slug']}.json", timeout=10); r.raise_for_status(); R = r.json()
        base = R.get("servings") or 0; want = p["servings"] or base; k = (want / base) if (base and want) else 1
        rows = []
        for i in R.get("ingredients", []):
            q = i.get("qty"); amt = (self.qty(q * k) + " " + (i.get("unit") or "")).strip() if q is not None else ""
            g = f"{round(i['grams'] * k)} g" if (p["grams"] and i.get("grams")) else ""
            name = i.get("name") or i.get("text", ""); note = f" ({i['note']})" if i.get("note") else ""
            rows.append([f"[ ] {name}{note}", " ".join(x for x in (amt, g) if x)])
        meta = [[k2, str(v)] for k2, v in (("Serves", int(want) if want else None), ("Prep", f"{R['prep']} min" if R.get("prep") else None), ("Cook", f"{R['cook']} min" if R.get("cook") else None)) if v]
        blocks = [{"t": "title", "text": R["title"]}]
        if meta: blocks.append({"t": "kv", "rows": meta, "small": True})
        blocks += [{"t": "hr"}, {"t": "h2", "text": "Ingredients"}, {"t": "kv", "rows": rows}]   # name left, amount right; long names drop the amount to the next line
        if p["steps"]:
            blocks += [{"t": "hr"}, {"t": "h2", "text": "Steps"}, {"t": "numbered", "items": R.get("steps", [])}]
        if R.get("source"): blocks += [{"t": "feed", "n": 1}, {"t": "qr", "data": f"{self.PUBLIC}/r/{p['slug']}", "size": 5, "label": "cook mode on the phone"}]
        blocks += footer("Pixel Pantry")
        return {"blocks": blocks}


@register
class Tide(Module):
    name = "tide"; title = "Tide slip"; blurb = "Highs and lows for the next few days from a NOAA tide station, with sunrise and sunset."
    icon = "wave"; group = "house"
    fields = [Field("days", "Days", "select", default="2", options=[["1", "Today"], ["2", "Today and tomorrow"], ["3", "Three days"], ["7", "The week"]])]
    def hidden(self): return not config.PLACE.get("tide_station")
    def build(self, p):
        import brief
        days = int(float(p["days"])); today = now().date()
        tides = brief.SOURCES["tides"](now().replace(hour=0, minute=0, second=0, microsecond=0), days * 24)
        unit = "m" if brief.metric() else "ft"
        blocks = [{"t": "title", "text": config.PLACE.get("tide_name") or config.PLACE.get("name") or "Tides"}, {"t": "hr"}]
        for n in range(days):
            day = today + dt.timedelta(days=n)
            rows = [[("High" if k == "H" else "Low") + " " + w.strftime("%-I:%M %p"), f"{v:.1f} {unit}"] for w, v, k in tides if w.date() == day]
            blocks.append({"t": "h2", "text": day.strftime("%A %b %-d") if n else "Today"})
            if rows: blocks.append({"t": "kv", "rows": rows})
            try:
                rise, sset = brief.SOURCES["sun"](day)
                if rise: blocks.append({"t": "small", "text": f"Sunrise {rise.strftime('%-I:%M %p')} \xb7 sunset {sset.strftime('%-I:%M %p')}"})
            except Exception: pass
        blocks += [{"t": "small", "text": f"NOAA {config.PLACE['tide_station']} \xb7 predictions, not observations", "align": "center"}] + footer("Char")
        return {"blocks": blocks}


@register
class Todo(Module):
    name = "todo"; title = "House list"; blurb = "A Home Assistant to-do list (shopping, chores) as a tick-box slip."
    icon = "check"; group = "house"
    fields = [Field("entity", "List", "select", required=True, options=[]), Field("title", "Title", "text", help="Blank uses the list's name")]
    HA = config.HA_URL
    def hidden(self): return not (self.HA and config.HA_TOKEN)
    def token(self): return config.HA_TOKEN
    def options(self):
        try:
            r = httpx.get(self.HA + "/api/states", headers={"Authorization": "Bearer " + self.token()}, timeout=5)
            return [[s["entity_id"], s["attributes"].get("friendly_name", s["entity_id"])] for s in r.json() if s["entity_id"].startswith("todo.")]
        except Exception: return []
    def describe(self):
        d = super().describe(); d["fields"] = [dict(f, options=self.options()) if f["name"] == "entity" else f for f in self.fields]; return d
    def build(self, p):
        h = {"Authorization": "Bearer " + self.token(), "Content-Type": "application/json"}
        r = httpx.post(self.HA + "/api/services/todo/get_items?return_response", headers=h, json={"entity_id": p["entity"], "status": "needs_action"}, timeout=10); r.raise_for_status()
        items = [i["summary"] for i in r.json().get("service_response", {}).get(p["entity"], {}).get("items", [])]
        name = p["title"] or httpx.get(self.HA + "/api/states/" + p["entity"], headers=h, timeout=5).json()["attributes"].get("friendly_name", "List")
        return {"blocks": [{"t": "title", "text": name}, {"t": "checklist", "items": items or ["(nothing on the list)"]}] + footer("Home Assistant")}


# ---------------------------------------------------------------- utility

@register
class System(Module):
    name = "system"; title = "System slip"; blurb = "Char itself: printer status, host, disk, uptime. Handy when something's off."
    icon = "gear"; group = "utility"; fields = []
    def build(self, p):
        from escpos import status
        import jobs
        st = jobs.printer_status(force=True)
        du = shutil.disk_usage("/data") if os.path.exists("/data") else shutil.disk_usage("/")
        up = time.time() - float(open("/proc/uptime").read().split()[0]) if os.path.exists("/proc/uptime") else None
        rows = [["Printer", (config.PRINTER + ":" + str(config.PRINTER_PORT)) if not config.NO_PRINTER else "none set"], ["Reachable", "yes" if st["reachable"] else "no"], ["Online", "yes" if st.get("online") else "no"],
                ["Paper", "out" if st.get("paper_out") else "low" if st.get("paper_low") else "ok"], ["Host", socket.gethostname()], ["Python", platform.python_version()],
                ["Data free", f"{du.free / 1e9:.1f} GB"], ["Booted", stamp(dt.datetime.fromtimestamp(up).astimezone()) if up else "-"]]
        return {"blocks": [{"t": "title", "text": "Char"}, {"t": "kv", "rows": rows}, ] + ([{"t": "feed", "n": 1}, {"t": "qr", "data": config.URL, "size": 5, "label": config.URL.split("://")[-1]}] if config.URL else []) + footer("Char")}


@register
class Test(Module):
    name = "test"; title = "Test pattern"; blurb = "Every text style, a table, a code and a dithered gradient. Prints once to check the head and the cut."
    icon = "gear"; group = "utility"; fields = []
    def build(self, p):
        from PIL import Image
        im = Image.linear_gradient("L").rotate(90, expand=True).resize((576, 80))
        import base64, io; buf = io.BytesIO(); im.save(buf, "PNG")
        return {"blocks": [{"t": "title", "text": "Test pattern"}, {"t": "h1", "text": "Heading, double height"}, {"t": "h2", "text": "Subheading"},
                           {"t": "p", "text": "Paragraph with **bold** and __underline__, wrapped at forty-eight columns like this sentence should be."},
                           {"t": "small", "text": "Small font, sixty-four columns: " + "0123456789" * 4},
                           {"t": "kv", "rows": [["Tide", "6.2 ft"], ["Current", "1.6 kt ebb"], ["A much longer key than the others", "value"]]},
                           {"t": "table", "rows": [["Time", "Ht", "Kind"], ["6:52 PM", "4.2", "Low"], ["11:59 PM", "6.8", "High"]]},
                           {"t": "checklist", "items": ["eggs", "a very long item that has to wrap around onto the next line neatly"]},
                           {"t": "qr", "data": config.URL or "https://example.com", "size": 6, "label": "a QR code"},
                           {"t": "image", "b64": base64.b64encode(buf.getvalue()).decode()}, {"t": "small", "text": "gradient: dithering check", "align": "center"}] + footer("Char")}


# sibling module files register themselves on import
import brief, puzzles  # noqa: E402,F401


def load_plugins(folder=None):
    """Import every *.py in the plugins folder (default /config/plugins) in name order. A plugin registers modules with
    @register (same name = replaces the built-in) and may swap the Brief's sources in brief.SOURCES. Errors are printed, not fatal."""
    folder = folder or config.PLUGINS
    for path in sorted(glob.glob(os.path.join(folder, "*.py"))):
        try:
            spec = importlib.util.spec_from_file_location("char_plugin_" + os.path.basename(path)[:-3], path)
            spec.loader.exec_module(importlib.util.module_from_spec(spec)); print("char: plugin", os.path.basename(path))
        except Exception:
            print("char: plugin failed", path); traceback.print_exc()


def visible():
    return {k: m for k, m in REGISTRY.items() if not m.hidden()}
