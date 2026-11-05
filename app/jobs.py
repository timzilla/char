"""The queue: every print becomes a job row in SQLite, a single worker thread feeds the printer one job at a time,
and a job that fails because the printer is off, out of paper or unreachable is retried for half an hour before
it is marked failed. Reprint re-renders the stored document."""
import json, os, queue, sqlite3, threading, time, traceback
import config, escpos

DB = os.environ.get("CHAR_DB", "/data/char.db")
PRINTER, PORT = config.PRINTER, config.PRINTER_PORT
RETRY_FOR = 30 * 60
RETRY_EVERY = 45

_q = queue.Queue()
_lock = threading.Lock()
state = {"printer": {}, "checked": 0, "current": None}


def db():
    con = sqlite3.connect(DB, check_same_thread=False); con.row_factory = sqlite3.Row
    con.execute("""create table if not exists jobs (id integer primary key autoincrement, created real, source text, who text, title text,
                   module text, params text, doc text, status text, error text, tries integer default 0, printed real, bytes integer)""")
    con.execute("create index if not exists jobs_created on jobs(created desc)")
    return con


def submit(doc, title="", source="web", who="", module="", params=None):
    """Render now (so a bad document fails fast), store, and queue. Returns the job id."""
    data = escpos.render(doc)
    with _lock, db() as con:
        cur = con.execute("insert into jobs (created, source, who, title, module, params, doc, status, bytes) values (?,?,?,?,?,?,?,?,?)",
                          (time.time(), source, who, title[:120], module, json.dumps(params or {}), json.dumps(doc), "queued", len(data)))
        jid = cur.lastrowid
    _q.put((jid, data, time.time()))
    return jid


def reprint(jid):
    with db() as con:
        row = con.execute("select * from jobs where id=?", (jid,)).fetchone()
    if not row: raise KeyError(jid)
    return submit(json.loads(row["doc"]), row["title"], "reprint", row["who"], row["module"], json.loads(row["params"] or "{}"))


def recent(n=40):
    with db() as con:
        return [dict(r) for r in con.execute("select id, created, source, who, title, module, status, error, tries, printed, bytes from jobs order by id desc limit ?", (n,))]


def get(jid):
    with db() as con:
        r = con.execute("select * from jobs where id=?", (jid,)).fetchone()
    return dict(r) if r else None


def set_status(jid, status, error=None, printed=False):
    with _lock, db() as con:
        con.execute("update jobs set status=?, error=?, tries=tries+1" + (", printed=?" if printed else "") + " where id=?",
                    (status, error, time.time(), jid) if printed else (status, error, jid))


def printer_status(force=False):
    if config.NO_PRINTER:   # no printer set: everything works, jobs are kept and can be previewed, nothing is sent
        return {"reachable": True, "online": True, "paper_out": False, "paper_low": False, "cover_open": False, "error": False, "simulated": True, "detail": "no printer set (CHAR_PRINTER)"}
    if force or time.time() - state["checked"] > 20:
        state["printer"] = escpos.status(PRINTER, PORT); state["checked"] = time.time()
    return state["printer"]


def _worker():
    while True:
        jid, data, first = _q.get()
        state["current"] = jid
        try:
            st = printer_status(force=True)
            if not st["reachable"] or st.get("paper_out") or st.get("cover_open") or not st.get("online"):
                why = ("printer unreachable" if not st["reachable"] else "out of paper" if st.get("paper_out") else "cover open" if st.get("cover_open") else "printer offline")
                if time.time() - first < RETRY_FOR:
                    set_status(jid, "waiting", why); time.sleep(RETRY_EVERY); _q.put((jid, data, first)); continue
                set_status(jid, "failed", why + " for 30 minutes"); continue
            if config.NO_PRINTER: set_status(jid, "printed", "no printer set; kept, not sent", printed=True); continue
            escpos.send(PRINTER, data, PORT)
            set_status(jid, "printed", None, printed=True)
            time.sleep(0.5 + len(data) / 20000)   # let the head finish before the next job's status check
        except Exception as e:
            traceback.print_exc()
            if time.time() - first < RETRY_FOR:
                set_status(jid, "waiting", str(e)[:200]); time.sleep(RETRY_EVERY); _q.put((jid, data, first))
            else:
                set_status(jid, "failed", str(e)[:200])
        finally:
            state["current"] = None
            _q.task_done()


def start():
    """Re-queue anything left queued/waiting from before a restart, then start the worker."""
    with db() as con:
        rows = con.execute("select id, doc from jobs where status in ('queued','waiting') order by id").fetchall()
    for r in rows:
        try: _q.put((r["id"], escpos.render(json.loads(r["doc"])), time.time()))
        except Exception: set_status(r["id"], "failed", "could not re-render after restart")
    threading.Thread(target=_worker, daemon=True, name="char-worker").start()
