"""Settings. Simple things come from environment variables (handy in a compose file); lists (news feeds, calendars) and
anything longer come from an optional TOML file, /config/char.toml by default (CHAR_CONFIG to move it).
An environment variable always wins over the file. See char.example.toml for every setting."""
import os, tomllib

PATH = os.environ.get("CHAR_CONFIG", "/config/char.toml")
try:
    with open(PATH, "rb") as f: FILE = tomllib.load(f)
except OSError:
    FILE = {}
except tomllib.TOMLDecodeError as e:
    print(f"char: could not read {PATH}: {e}"); FILE = {}


def get(section, key, env=None, default=""):
    """env var > [section] key in the file > default."""
    if env and os.environ.get(env, "") != "": return os.environ[env]
    v = FILE.get(section, {}).get(key)
    return default if v in (None, "") else v


def _secret(env, section, key, file_path=None):
    """A token from a mounted secret file, the environment, or the config file, in that order."""
    if file_path:
        try: return open(file_path).read().strip()
        except OSError: pass
    return str(get(section, key, env, ""))


PRINTER = str(get("printer", "host", "CHAR_PRINTER", ""))          # "" or "none" = no printer: jobs are kept, nothing is sent
PRINTER_PORT = int(get("printer", "port", "CHAR_PRINTER_PORT", 9100))
URL = str(get("char", "url", "CHAR_URL", "")).rstrip("/")          # this Char as people reach it (QR codes on the System slip)
TOKEN = _secret("CHAR_TOKEN", "char", "token")                     # optional: require it for the web app, the API and MCP
PLUGINS = str(get("char", "plugins", "CHAR_PLUGINS", os.path.join(os.path.dirname(PATH), "plugins")))
HA_URL = str(get("home_assistant", "url", "CHAR_HA", "")).rstrip("/")
HA_TOKEN = _secret("HA_TOKEN", "home_assistant", "token", "/run/secrets/ha_token")
PANTRY_URL = str(get("pixel_pantry", "url", "CHAR_KITCHEN", "")).rstrip("/")                 # how Char reaches Pixel Pantry
PANTRY_PUBLIC = str(get("pixel_pantry", "public_url", "CHAR_KITCHEN_PUBLIC", PANTRY_URL)).rstrip("/")   # what a QR code on paper should open
NO_PRINTER = PRINTER.lower() in ("", "none", "off")

PLACE = FILE.get("place", {})        # name, latitude, longitude, units ("us" | "metric"), tide_station (NOAA)
BRIEF = FILE.get("brief", {})        # local_heading, local_feeds, national_feeds, calendars
