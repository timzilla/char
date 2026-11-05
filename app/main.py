"""Char: a home print service. One container that owns a receipt printer and offers it to everything else:
an installable web app, an HTTP API for your other apps, an MCP endpoint so Claude can print, a Home Assistant
event bridge for automations, and an Android share target. See README.md."""
import asyncio, base64, contextlib, datetime as dt, hmac, json, os, time, traceback
from fastapi import FastAPI, Request, Form, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, RedirectResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import httpx
from mcp.server.fastmcp import FastMCP

import config, escpos, jobs, modules

HERE = os.path.dirname(os.path.abspath(__file__))
HA = config.HA_URL
modules.load_plugins()
tpl = Jinja2Templates(directory=os.path.join(HERE, "templates"))
tpl.env.filters["ago"] = lambda ts: _ago(ts)
tpl.env.filters["clock"] = lambda ts: dt.datetime.fromtimestamp(ts).strftime("%a %-I:%M %p") if ts else ""


def _ago(ts):
    if not ts: return ""
    m = (time.time() - ts) / 60
    return "just now" if m < 1 else f"{m:.0f} min ago" if m < 60 else f"{m / 60:.0f} h ago" if m < 36 * 60 else f"{m / 1440:.0f} days ago"


def who(request: Request):
    """Who printed it, when an auth proxy in front says so (Cloudflare Access, or a Remote-User style header); else blank."""
    if not request: return ""
    h = request.headers
    return h.get("cf-access-authenticated-user-email") or h.get("remote-user") or h.get("x-forwarded-user") or ""


# ---------------------------------------------------------------- MCP: tools for Claude

mcp = FastMCP("char", instructions="Print to the home receipt printer (80 mm thermal). Slips are 48 characters wide; keep lines short, use headings and lists. "
              "Use print_markdown for anything written, print_module for the ready-made slips (list_modules shows them), print_image for pictures.",
              stateless_http=True, json_response=True, streamable_http_path="/")


@mcp.tool()
def print_markdown(markdown: str, title: str = "") -> str:
    """Print Markdown on the receipt printer. Supports # title, ## heading, ### subheading, paragraphs with **bold**, - bullets,
    - [ ] tick boxes, 1. numbered steps, > quotes, --- rules, | tables |, 'key:  value' rows (two spaces), and 'qr: <link> | caption'."""
    jid = jobs.submit({"blocks": escpos.markdown_to_blocks(markdown)}, title or markdown.strip().split("\n")[0].lstrip("# ")[:60], "claude", "claude")
    return f"queued as job {jid}; it prints within a few seconds if the printer is ready"


@mcp.tool()
def print_image(png_base64: str, caption: str = "", width: int = 576) -> str:
    """Print a picture (PNG or JPEG, base64) dithered to black and white, up to 576 pixels wide, with an optional caption."""
    blocks = [{"t": "image", "b64": png_base64, "width": width}]
    if caption: blocks.append({"t": "small", "text": caption, "align": "center"})
    return f"queued as job {jobs.submit({'blocks': blocks}, caption or 'image', 'claude', 'claude')}"


@mcp.tool()
def print_module(name: str, params: dict | None = None) -> str:
    """Print one of the ready-made slips. Call list_modules for the names and their parameters."""
    m = modules.visible().get(name)
    if not m: return "no such module; call list_modules"
    p = m.clean(params or {})
    return f"queued as job {jobs.submit(m.build(p), m.title, 'claude', 'claude', name, p)}"


@mcp.tool()
def list_modules() -> list:
    """The ready-made slips and the parameters each takes."""
    return [m.describe() for m in modules.visible().values()]


@mcp.tool()
def printer_status() -> dict:
    """Is the printer reachable, online, and does it have paper. Plus the last few jobs."""
    return {"printer": jobs.printer_status(), "recent": jobs.recent(8)}


# ---------------------------------------------------------------- Home Assistant bridge

def ha_token():
    return config.HA_TOKEN if HA else ""


async def ha_bridge():
    """Subscribe to the Home Assistant event `char_print`. An automation fires it with data like
    {"markdown": "# Door\\nSomeone at the door"} or {"module": "tide", "params": {"days": "1"}} or {"text": "plain"}."""
    import websockets
    url = HA.replace("http", "ws", 1) + "/api/websocket"
    while True:
        try:
            async with websockets.connect(url, max_size=None, ping_interval=30) as ws:
                await ws.recv()
                await ws.send(json.dumps({"type": "auth", "access_token": ha_token()}))
                if json.loads(await ws.recv())["type"] != "auth_ok": print("char: HA auth failed"); await asyncio.sleep(300); continue
                await ws.send(json.dumps({"id": 1, "type": "subscribe_events", "event_type": "char_print"}))
                print("char: listening for Home Assistant char_print events")
                async for raw in ws:
                    msg = json.loads(raw)
                    if msg.get("type") != "event": continue
                    d = msg["event"].get("data") or {}
                    try:
                        if d.get("module"):
                            m = modules.REGISTRY[d["module"]]; p = m.clean(d.get("params") or {}); jobs.submit(m.build(p), d.get("title") or m.title, "home-assistant", "", d["module"], p)
                        elif d.get("markdown") or d.get("text"):
                            jobs.submit({"blocks": escpos.markdown_to_blocks(d.get("markdown") or d.get("text"))}, d.get("title") or "Home Assistant", "home-assistant")
                    except Exception:
                        traceback.print_exc()
        except Exception as e:
            print("char: HA bridge", e)
        await asyncio.sleep(30)


@contextlib.asynccontextmanager
async def lifespan(app):
    jobs.start()
    task = asyncio.create_task(ha_bridge()) if ha_token() else None
    async with mcp.session_manager.run():
        yield
    if task: task.cancel()


app = FastAPI(title="Char", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=os.path.join(HERE, "static")), name="static")
app.mount("/mcp", mcp.streamable_http_app())


OPEN_PATHS = ("/static/", "/login", "/manifest.webmanifest", "/sw.js", "/health")


@app.middleware("http")
async def token_gate(request: Request, call_next):
    """With CHAR_TOKEN set, everything but the login page and static files needs the token: an
    `Authorization: Bearer <token>` header (apps, MCP clients) or the cookie the login page sets (browsers)."""
    if not config.TOKEN or request.url.path.startswith(OPEN_PATHS) or request.url.path == "/login":
        return await call_next(request)
    given = request.headers.get("authorization", "").removeprefix("Bearer ").strip() or request.cookies.get("char_token", "")
    if given and hmac.compare_digest(given, config.TOKEN): return await call_next(request)
    if request.method == "GET" and "text/html" in request.headers.get("accept", ""): return RedirectResponse("/login", status_code=303)
    return JSONResponse({"error": "Char needs its token (Authorization: Bearer ...)"}, status_code=401)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, bad: int = 0):
    return tpl.TemplateResponse(request, "login.html", {"bad": bad, "printer": {"reachable": True, "online": True}, "me": ""})


@app.post("/login")
async def login(request: Request, token: str = Form("")):
    if not config.TOKEN: return RedirectResponse("/", status_code=303)
    if not hmac.compare_digest(token.strip(), config.TOKEN): return RedirectResponse("/login?bad=1", status_code=303)
    r = RedirectResponse("/", status_code=303)
    r.set_cookie("char_token", config.TOKEN, max_age=400 * 86400, httponly=True, samesite="lax", secure=request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https")
    return r


@app.middleware("http")
async def mcp_slash(request: Request, call_next):
    """Serve the MCP endpoint at /mcp as well as /mcp/ (the mount would otherwise answer with a redirect MCP clients won't follow)."""
    if request.scope["path"] == "/mcp":
        request.scope["path"] = "/mcp/"; request.scope["raw_path"] = b"/mcp/"
    return await call_next(request)


# ---------------------------------------------------------------- the web app

def grouped():
    g = {"write": [], "house": [], "play": [], "utility": []}
    for m in modules.visible().values(): g.setdefault(m.group, []).append(m)
    return g


@app.get("/", response_class=HTMLResponse)
def home(request: Request, job: int = 0):
    return tpl.TemplateResponse(request, "index.html", {"groups": grouped(), "printer": jobs.printer_status(), "jobs": jobs.recent(12), "job": job, "me": who(request)})


@app.get("/m/{name}", response_class=HTMLResponse)
def module_page(request: Request, name: str):
    m = modules.visible().get(name)
    if not m: raise HTTPException(404)
    return tpl.TemplateResponse(request, "module.html", {"m": m.describe(), "printer": jobs.printer_status(), "me": who(request)})


@app.get("/jobs", response_class=HTMLResponse)
def jobs_page(request: Request):
    return tpl.TemplateResponse(request, "jobs.html", {"jobs": jobs.recent(100), "printer": jobs.printer_status(), "me": who(request)})


@app.post("/m/{name}/print")
async def module_print(request: Request, name: str):
    m = modules.visible().get(name)
    if not m: raise HTTPException(404)
    form = await request.form()
    try:
        p = m.clean(dict(form)); doc = m.build(p)
        jid = jobs.submit(doc, m.title, "web", who(request), name, p)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return JSONResponse({"job": jid})


@app.post("/api/preview")
async def preview(request: Request):
    body = await request.json()
    tok = modules.PREVIEW.set(True)    # previews never call Claude
    try:
        doc = await asyncio.to_thread(_doc_from, body)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    finally:
        modules.PREVIEW.reset(tok)
    imgs = [{"b64": b["b64"] if isinstance(b.get("b64"), str) else base64.b64encode(b.get("png") or b"").decode(), "width": b.get("width") or 576}
            for b in doc.get("blocks", []) if b.get("t") == "image"]
    return {"text": escpos.preview_text(doc), "bytes": len(escpos.render(doc)), "images": imgs}


def _doc_from(body):
    if body.get("module"):
        m = modules.visible()[body["module"]]; return m.build(m.clean(body.get("params") or {}))
    if body.get("doc"): return body["doc"]
    if body.get("markdown") is not None: return {"blocks": escpos.markdown_to_blocks(body["markdown"]), "cut": body.get("cut", True)}
    if body.get("text") is not None: return {"blocks": [{"t": "p", "text": body["text"]}], "cut": body.get("cut", True)}
    raise ValueError("send markdown, text, doc or module")


@app.post("/api/print")
async def api_print(request: Request):
    """The app-facing endpoint. JSON: {"markdown": ...} | {"text": ...} | {"doc": {...}} | {"module": name, "params": {...}}, plus optional title, source."""
    body = await request.json()
    try:
        doc = _doc_from(body)
        jid = jobs.submit(doc, body.get("title") or body.get("module") or (body.get("markdown") or body.get("text") or "").strip().split("\n")[0][:60], body.get("source") or "api", who(request), body.get("module", ""), body.get("params"))
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return {"job": jid}


@app.post("/api/jobs/{jid}/reprint")
def api_reprint(jid: int):
    try: return {"job": jobs.reprint(jid)}
    except KeyError: raise HTTPException(404)


@app.get("/api/jobs")
def api_jobs(n: int = 40): return jobs.recent(n)


@app.get("/api/jobs/{jid}")
def api_job(jid: int):
    j = jobs.get(jid)
    if not j: raise HTTPException(404)
    j.pop("doc", None); return j


@app.get("/api/printer/status")
def api_status(force: int = 0): return {**jobs.printer_status(force=bool(force)), "current": jobs.state["current"]}


@app.get("/api/modules")
def api_modules(): return [m.describe() for m in modules.visible().values()]


@app.post("/share")
async def share(request: Request, title: str = Form(""), text: str = Form(""), url: str = Form(""), image: UploadFile | None = File(None)):
    """Android share target: a link prints as a note with a QR code, text as a note, a photo dithered."""
    blocks = []
    if image is not None and image.filename:
        data = await image.read()
        blocks.append({"t": "image", "b64": base64.b64encode(data).decode()})
        if title or text: blocks.append({"t": "small", "text": title or text, "align": "center"})
    else:
        link = url or (text if text.startswith("http") else "")
        body = text if text and text != link else ""
        if title: blocks.append({"t": "h1", "text": title})
        if body: blocks += escpos.markdown_to_blocks(body)
        if link: blocks.append({"t": "qr", "data": link, "size": 6, "label": link[:60]})
    if not blocks: return RedirectResponse("/", status_code=303)
    blocks += modules.footer("shared from " + (who(request).split("@")[0] or "the phone"))
    jid = jobs.submit({"blocks": blocks}, title or text[:60] or url[:60] or "photo", "share", who(request))
    return RedirectResponse(f"/?job={jid}", status_code=303)


@app.get("/manifest.webmanifest")
def manifest(): return FileResponse(os.path.join(HERE, "static", "manifest.webmanifest"), media_type="application/manifest+json")


@app.get("/sw.js")
def sw(): return FileResponse(os.path.join(HERE, "static", "sw.js"), media_type="application/javascript", headers={"Service-Worker-Allowed": "/"})


@app.get("/health")
def health(): return {"ok": True, "printer": jobs.printer_status(), "queue": jobs.state["current"]}
