"""The Brief (morning / evening) and the taffy joke. Registered as Char modules.

Brief = the weather and sun, the next tides, a few local and national headlines (Claude picks and tightens them when a
key is set; otherwise the raw titles print), and a heads-up of events in the next few days from your calendars.
Where each part comes from is in SOURCES and EVENT_KINDS, so a plugin can swap in its own:
  SOURCES["weather"](edition, now) -> [(label, text), ...]      default: Open-Meteo for [place] latitude/longitude
  SOURCES["sun"](date) -> (sunrise, sunset) aware datetimes      default: Open-Meteo
  SOURCES["tides"](start, hours) -> [(when, height, "H"|"L")]   default: NOAA CO-OPS predictions for [place] tide_station
  EVENT_KINDS["ics"](url, name) -> [{date, time, title, where}]  add your own kinds for pages without a calendar feed
Feeds and calendars are listed in char.toml under [brief]; see char.example.toml.
"""
import datetime as dt, html, json, os, re, time, xml.etree.ElementTree as ET
import httpx
import config
from modules import Module, Field, register, footer, now, stamp, claude_ok

UA = "Mozilla/5.0 (compatible; Char/1.0; a home receipt-printer service)"
DATA = "/data" if os.path.isdir("/data") else os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
PLACE, BRIEF = config.PLACE, config.BRIEF
FEEDS = {
    "local": [(f.get("name", ""), f["url"]) for f in BRIEF.get("local_feeds", []) if f.get("url")],
    "national": [(f.get("name", ""), f["url"]) for f in BRIEF.get("national_feeds", [])] or [("NPR", "https://feeds.npr.org/1001/rss.xml")],
}
CALENDARS = [(c.get("name", ""), c["url"], c.get("kind", "ics")) for c in BRIEF.get("calendars", []) if c.get("url")]
MONTHS = {m: i for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"], 1)}


def key(): return claude_ok()


def metric(): return str(PLACE.get("units", "us")).lower().startswith("m")


def get(url, timeout=20):
    r = httpx.get(url, headers={"User-Agent": UA}, timeout=timeout, follow_redirects=True); r.raise_for_status(); return r


# ---------------------------------------------------------------- weather, sun, tides (default sources)

WMO = {0: "Clear", 1: "Mostly clear", 2: "Partly cloudy", 3: "Cloudy", 45: "Fog", 48: "Freezing fog", 51: "Light drizzle", 53: "Drizzle", 55: "Heavy drizzle",
       56: "Freezing drizzle", 57: "Freezing drizzle", 61: "Light rain", 63: "Rain", 65: "Heavy rain", 66: "Freezing rain", 67: "Freezing rain",
       71: "Light snow", 73: "Snow", 75: "Heavy snow", 77: "Snow grains", 80: "Showers", 81: "Showers", 82: "Heavy showers", 85: "Snow showers",
       86: "Heavy snow showers", 95: "Thunderstorms", 96: "Thunderstorms with hail", 99: "Thunderstorms with hail"}
_cache = {}


def open_meteo():
    """Three days of daily weather and sun times, cached for half an hour (only ever fetched when a slip is asked for)."""
    lat, lon = PLACE.get("latitude"), PLACE.get("longitude")
    if lat in (None, "") or lon in (None, ""): raise ValueError("set [place] latitude and longitude in char.toml")
    c = _cache.get("om")
    if c and time.time() - c[0] < 1800: return c[1]
    q = {"latitude": lat, "longitude": lon, "timezone": "auto", "forecast_days": 3,
         "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,wind_speed_10m_max,sunrise,sunset"}
    if not metric(): q.update(temperature_unit="fahrenheit", wind_speed_unit="mph")
    d = httpx.get("https://api.open-meteo.com/v1/forecast", params=q, timeout=15).json()["daily"]
    _cache["om"] = (time.time(), d); return d


def om_weather(edition, t):
    d = open_meteo(); deg = "°C" if metric() else "°F"; spd = "km/h" if metric() else "mph"
    def day(i, label, night=False):
        words = WMO.get(d["weather_code"][i], "")
        bits = [f"low {round(d['temperature_2m_min'][i + 1 if night and i + 1 < len(d['time']) else i])}{deg}" if night else f"high {round(d['temperature_2m_max'][i])}{deg}"]
        if (d["precipitation_probability_max"][i] or 0) >= 20: bits.append(f"{d['precipitation_probability_max'][i]}% chance of rain")
        if (d["wind_speed_10m_max"][i] or 0) >= 10: bits.append(f"wind to {round(d['wind_speed_10m_max'][i])} {spd}")
        return (label, (words + " \xb7 " if words and not night else "") + ", ".join(bits))
    return [day(0, "Today"), day(0, "Tonight", night=True)] if edition == "morning" else [day(0, "Tonight", night=True), day(1, "Tomorrow")]


def om_sun(day):
    d = open_meteo(); tz = now().tzinfo
    i = d["time"].index(day.isoformat())
    return tuple(dt.datetime.fromisoformat(d[k][i]).replace(tzinfo=tz) for k in ("sunrise", "sunset"))


def noaa_tides(start, hours):
    """High and low tide predictions from NOAA CO-OPS (US stations; https://tidesandcurrents.noaa.gov). Times are the station's local time."""
    st = PLACE.get("tide_station")
    if not st: raise ValueError("set [place] tide_station in char.toml")
    r = httpx.get("https://api.tidesandcurrents.noaa.gov/api/prod/datagetter", timeout=15, params={
        "product": "predictions", "application": "char", "begin_date": start.strftime("%Y%m%d %H:%M"), "range": int(hours) + 1,
        "datum": "MLLW", "station": st, "time_zone": "lst_ldt", "interval": "hilo", "units": "metric" if metric() else "english", "format": "json"}).json()
    if "error" in r: raise ValueError(r["error"].get("message", "NOAA error"))
    tz = now().tzinfo
    return [(dt.datetime.fromisoformat(x["t"]).replace(tzinfo=tz), float(x["v"]), x["type"]) for x in r.get("predictions", [])]


SOURCES = {"weather": om_weather, "sun": om_sun, "tides": noaa_tides}


# ---------------------------------------------------------------- news

def strip_html(s):
    return html.unescape(re.sub(r"\s+", " ", re.sub(r"(?s)<[^>]+>", " ", s or ""))).strip()


def rss(url, hours=36):
    """[{title, summary, link, when}] from an RSS or Atom feed, newest first, within `hours`."""
    root = ET.fromstring(get(url).content)
    ns = {"a": "http://www.w3.org/2005/Atom"}
    out, cutoff = [], dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)
    items = root.findall(".//item") or root.findall(".//a:entry", ns)
    for it in items:
        t = (it.findtext("title") or it.findtext("a:title", namespaces=ns) or "").strip()
        d = it.findtext("description") or it.findtext("a:summary", namespaces=ns) or ""
        link = (it.findtext("link") or (it.find("a:link", ns).get("href") if it.find("a:link", ns) is not None else "") or "").strip()
        p = it.findtext("pubDate") or it.findtext("a:updated", namespaces=ns) or it.findtext("a:published", namespaces=ns) or ""
        when = None
        for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S %Z", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ"):
            try: when = dt.datetime.strptime(p.strip().replace("GMT", "+0000"), fmt); break
            except ValueError: pass
        if when and when.tzinfo is None: when = when.replace(tzinfo=dt.timezone.utc)
        if when and when < cutoff: continue
        if t: out.append({"title": strip_html(t), "summary": strip_html(d)[:300], "link": link, "when": when.isoformat() if when else ""})
    return out


def headlines(kind, n):
    items, errs = [], []
    for name, url in FEEDS[kind]:
        try:
            for it in rss(url)[:25]: it["source"] = name; items.append(it)
        except Exception as e: errs.append(f"{name}: {e.__class__.__name__}")
    return items, errs


def pick(local, national, n_local, n_national):
    """Claude chooses the stories worth a kitchen printer and writes each as one line. Falls back to raw titles."""
    if not key() or not (local or national):
        return [i["title"] for i in local[:n_local]], [i["title"] for i in national[:n_national]]
    import anthropic
    from pydantic import BaseModel
    class Picks(BaseModel):
        local: list[str]
        national: list[str]
    def block(items): return "\n".join(f"- [{i['source']}] {i['title']} :: {i['summary'][:160]}" for i in items)
    client = anthropic.Anthropic()
    r = client.messages.parse(
        model="claude-opus-5-5", max_tokens=2000, output_config={"effort": "low"},
        system=(f"You edit a short printed news brief for a household in {PLACE.get('region') or PLACE.get('name') or 'the United States'}. From the candidate stories pick the "
                f"{n_local} most useful local stories and the {n_national} most important national/world stories. Rewrite each as ONE plain line of at most "
                "80 characters that states the news itself (what happened), not a teaser. No sports scores, no celebrity, no opinion pieces, no duplicates. "
                "Skip a category's slots if there is nothing worth printing. Keep it calm and factual."),
        messages=[{"role": "user", "content": f"LOCAL CANDIDATES\n{block(local)}\n\nNATIONAL CANDIDATES\n{block(national)}"}],
        output_format=Picks)
    if r.stop_reason == "refusal" or r.parsed_output is None:
        return [i["title"] for i in local[:n_local]], [i["title"] for i in national[:n_national]]
    return r.parsed_output.local[:n_local], r.parsed_output.national[:n_national]


# ---------------------------------------------------------------- events

def parse_ics(text, source):
    out = []
    for ev in re.findall(r"(?s)BEGIN:VEVENT(.*?)END:VEVENT", text):
        m = re.search(r"DTSTART[^:]*:(\d{8})(?:T(\d{4}))?", ev); s = re.search(r"SUMMARY[^:]*:(.*)", ev); loc = re.search(r"LOCATION[^:]*:(.*)", ev)
        if not (m and s): continue
        d = f"{m.group(1)[:4]}-{m.group(1)[4:6]}-{m.group(1)[6:]}"
        t = ""
        if m.group(2):
            h, mi = int(m.group(2)[:2]), m.group(2)[2:]; t = f"{(h % 12) or 12}:{mi} {'am' if h < 12 else 'pm'}".replace(":00", "")
        title = s.group(1).strip().replace("\\,", ",").replace("\\;", ";")
        where = (loc.group(1).strip().replace("\\,", ",").split(",")[0] if loc and loc.group(1).strip() else source)
        out.append({"date": d, "time": t, "title": title[:70], "where": where})
    return out


def events(refresh=False):
    """All known upcoming events from the calendars in char.toml, cached in data/events.json for 20 hours."""
    path = os.path.join(DATA, "events.json")
    try:
        c = json.load(open(path))
        if not refresh and now().timestamp() - c.get("fetched", 0) < 20 * 3600 and c.get("sources") == [u for _, u, _ in CALENDARS]: return c["events"], c.get("errors", [])
    except Exception: pass
    found, errs = [], []
    for name, url, kind in CALENDARS:
        try: found += EVENT_KINDS[kind](url, name)
        except KeyError: errs.append(f"{name}: no reader for kind '{kind}'")
        except Exception as e: errs.append(f"{name}: {e.__class__.__name__}")
    found.sort(key=lambda e: (e["date"], e["time"]))
    try: json.dump({"fetched": now().timestamp(), "events": found, "errors": errs, "sources": [u for _, u, _ in CALENDARS]}, open(path, "w"))
    except Exception: pass
    return found, errs


EVENT_KINDS = {"ics": lambda url, name: parse_ics(get(url).text, name)}


def upcoming(days, start=None):
    start = start or now().date(); end = start + dt.timedelta(days=days)
    ev, errs = events()
    return [e for e in ev if start <= dt.date.fromisoformat(e["date"]) <= end], errs


def daylabel(d, today):
    d = dt.date.fromisoformat(d) if isinstance(d, str) else d
    if d == today: return "Today"
    if d == today + dt.timedelta(days=1): return "Tomorrow"
    return d.strftime("%a %-d") if (d - today).days < 7 else d.strftime("%b %-d")


# ---------------------------------------------------------------- the brief

@register
class Brief(Module):
    name = "brief"; title = "Brief"; blurb = "Morning or evening: the weather, sun, next tides, a few local and national headlines, and what's on nearby soon."
    icon = "news"; group = "house"
    fields = [Field("edition", "Edition", "select", default="auto", options=[["auto", "By the clock"], ["morning", "Morning"], ["evening", "Evening"]]),
              Field("local", "Local headlines", "number", default="4"), Field("national", "National headlines", "number", default="4"),
              Field("days", "Events heads-up, days ahead", "number", default="7"), Field("tide", "Include the tide", "checkbox", default=True)]

    def build(self, p):
        t = now(); today = t.date()
        edition = p["edition"] if p["edition"] != "auto" else ("morning" if t.hour < 14 else "evening")
        blocks = [{"t": "title", "text": "Morning" if edition == "morning" else "Evening"},
                  {"t": "p", "text": t.strftime("%A, %B %-d"), "align": "center"}]
        notes = []
        # weather
        try:
            w = SOURCES["weather"](edition, t)
            if w: blocks += [{"t": "h2", "text": "Weather"}] + [{"t": "p", "text": f"**{label}.** {text}"} for label, text in w]
        except Exception as e: notes.append(f"weather: {str(e)[:70]}")
        # sun and tides
        srow = []
        try:
            sun_day = today if edition == "morning" else today + dt.timedelta(days=1)
            rise, sset = SOURCES["sun"](sun_day)
            if rise: srow.append(["Sunrise" + ("" if edition == "morning" else " tomorrow"), rise.strftime("%-I:%M %p")])
            if sset and edition == "morning": srow.append(["Sunset", sset.strftime("%-I:%M %p")])
        except Exception as e: notes.append(f"sun: {str(e)[:60]}")
        if p["tide"] and (PLACE.get("tide_station") or SOURCES["tides"] is not noaa_tides):
            try:
                unit = "m" if metric() else "ft"
                for when, v, k in [x for x in SOURCES["tides"](t - dt.timedelta(hours=1), 36) if x[0] > t][:3]:
                    srow.append([("High" if k == "H" else "Low") + " tide", f"{when.strftime('%-I:%M %p')} \xb7 {v:.1f} {unit}"])
            except Exception as e: notes.append(f"tides: {str(e)[:60]}")
        if srow: blocks.append({"t": "kv", "rows": srow, "small": True})
        # headlines
        nl = int(p["local"] or 0) if FEEDS["local"] else 0; nn = int(p["national"] or 0)
        if nl or nn:
            local, e1 = headlines("local", nl) if nl else ([], []); national, e2 = headlines("national", nn) if nn else ([], [])
            notes += e1 + e2
            try: L, N = pick(local, national, nl, nn)
            except Exception as e: notes.append(f"claude: {e.__class__.__name__}"); L, N = [i["title"] for i in local[:nl]], [i["title"] for i in national[:nn]]
            if L: blocks += [{"t": "hr"}, {"t": "h2", "text": BRIEF.get("local_heading") or "Around town"}, {"t": "list", "items": L}]
            if N: blocks += [{"t": "hr"}, {"t": "h2", "text": BRIEF.get("national_heading") or "The wider world"}, {"t": "list", "items": N}]
        # events
        days = int(p["days"] or 0)
        if days and CALENDARS:
            start = today if edition == "morning" else today + dt.timedelta(days=1)
            ev, e3 = upcoming(days, start); notes += e3
            if ev:
                blocks += [{"t": "hr"}, {"t": "h2", "text": BRIEF.get("events_heading") or "Coming up nearby"}]
                blocks.append({"t": "kv", "rows": [[daylabel(e["date"], today) + (" " + e["time"] if e["time"] else ""), e["title"] + (f" ({e['where'].split(',')[0]})" if e["where"] else "")] for e in ev[:8]], "small": True})
        if notes: blocks.append({"t": "small", "text": "skipped: " + "; ".join(notes)})
        return {"blocks": blocks + footer("Char")}


# ---------------------------------------------------------------- the taffy joke

OFFLINE_JOKES = [
    ("What do you call a sleeping dinosaur?", "A dino-snore."), ("Why did the cookie go to the doctor?", "It felt crummy."),
    ("What do you call cheese that isn't yours?", "Nacho cheese."), ("Why can't you give Elsa a balloon?", "She'll let it go."),
    ("What did the ocean say to the beach?", "Nothing, it just waved."), ("Why did the banana go to the doctor?", "It wasn't peeling well."),
    ("What do you call a fish with no eyes?", "A fsh."), ("How does a penguin build a house?", "Igloos it together."),
    ("What's orange and sounds like a parrot?", "A carrot."), ("Why did the scarecrow win an award?", "He was outstanding in his field."),
    ("What do you call a bear with no teeth?", "A gummy bear."), ("Why don't eggs tell jokes?", "They'd crack each other up."),
    ("What do you call a pile of cats?", "A meow-ntain."), ("Why was the math book sad?", "It had too many problems."),
    ("What do clouds wear under their pants?", "Thunderwear."), ("What kind of tree fits in your hand?", "A palm tree."),
    ("Why did the bicycle fall over?", "It was two tired."), ("What do you call a snowman in July?", "A puddle."),
    ("How do you make a tissue dance?", "Put a little boogie in it."), ("What do you call a dog magician?", "A labracadabrador."),
    ("Why did the tomato blush?", "It saw the salad dressing."), ("What's a whale's favorite game?", "Swallow the leader."),
    ("What do you call a boomerang that won't come back?", "A stick."), ("Why are ghosts bad liars?", "You can see right through them."),
    ("What has four wheels and flies?", "A garbage truck."), ("What did one wall say to the other?", "Meet you at the corner."),
    ("Why did the golfer bring two pairs of pants?", "In case he got a hole in one."), ("What do you call an alligator in a vest?", "An investigator."),
    ("Why can't a nose be twelve inches long?", "Then it would be a foot."), ("What do you give a sick lemon?", "Lemon aid."),
    ("What did the big flower say to the little flower?", "Hi, bud."), ("Why did the orca cross the sound?", "To get to the other tide."),
]


@register
class Joke(Module):
    name = "joke"; title = "Taffy joke"; blurb = "A groaner on a wrapper: question on top, answer below. Claude writes a fresh one; a topic is optional."
    icon = "joke"; group = "play"
    fields = [Field("topic", "About (optional)", "text", placeholder="whales, pancakes, the dog, Dad"),
              Field("count", "How many", "select", default="1", options=[["1", "One"], ["2", "Two"], ["3", "Three"]]),
              Field("fresh", "Ask Claude for new ones", "checkbox", default=True)]

    def build(self, p):
        n = int(p["count"]); jokes = []
        if p["fresh"] and key():
            try:
                import anthropic
                from pydantic import BaseModel
                class J(BaseModel):
                    question: str
                    answer: str
                class Js(BaseModel):
                    jokes: list[J]
                topic = f" about {p['topic']}" if p["topic"] else ""
                r = anthropic.Anthropic().messages.parse(
                    model="claude-opus-5-5", max_tokens=1000, output_config={"effort": "low"},
                    system="You write candy-wrapper jokes for kids: a short question and a pun answer that makes a grown-up groan. "
                           "Clean, kind, no brands, no politics, no gross-out. Each question under 60 characters, each answer under 40. Original, not the famous ones.",
                    messages=[{"role": "user", "content": f"Write {n} wrapper joke{'s' if n > 1 else ''}{topic}."}], output_format=Js)
                if r.parsed_output: jokes = [(j.question, j.answer) for j in r.parsed_output.jokes][:n]
            except Exception: jokes = []
        if not jokes:
            import random
            jokes = random.sample(OFFLINE_JOKES, n)
        blocks = []
        for q, a in jokes:
            blocks += [{"t": "hr", "ch": "="}, {"t": "small", "text": "T A F F Y   J O K E", "align": "center"}, {"t": "hr", "ch": "="},
                       {"t": "p", "text": "**Q:** " + q}, {"t": "feed", "n": 1}, {"t": "p", "text": "**A:** " + a, "align": "right"}, {"t": "hr", "ch": "="}]
        return {"blocks": blocks + [{"t": "small", "text": stamp(), "align": "center"}]}
