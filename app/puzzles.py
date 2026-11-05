"""Paper puzzles: sudoku, word search, maze. Each is seeded by a number so a slip can be reprinted (or its solution)
later: the number is printed on the slip. Grids are drawn with Pillow at the printer's full width (576 dots)."""
import base64, io, random
from PIL import Image, ImageDraw, ImageFont
from modules import Module, Field, register, footer, stamp

W = 576


def font(size):
    try: return ImageFont.load_default(size=size)
    except TypeError: return ImageFont.load_default()


def png(im):
    buf = io.BytesIO(); im.save(buf, "PNG"); return base64.b64encode(buf.getvalue()).decode()


def seed_of(p):
    s = (p.get("number") or "").strip()
    return (int(s) if s.isdigit() else random.randint(1000, 9999)) % 100000


def text_center(d, box, s, f, fill=0):
    x0, y0, x1, y1 = box; l, t, r, b = f.getbbox(s)
    d.text(((x0 + x1 - (r - l)) / 2 - l, (y0 + y1 - (b - t)) / 2 - t), s, font=f, fill=fill)


# ---------------------------------------------------------------- sudoku

def sudoku_solutions(g, limit=2):
    """Count solutions up to `limit` (backtracking)."""
    best = None
    for i in range(81):
        if g[i] == 0:
            r, c = divmod(i, 9); used = set(g[r * 9:(r + 1) * 9]) | {g[c + 9 * k] for k in range(9)} | {g[(r // 3 * 3 + k // 3) * 9 + c // 3 * 3 + k % 3] for k in range(9)}
            cands = [v for v in range(1, 10) if v not in used]
            if best is None or len(cands) < len(best[1]): best = (i, cands)
            if not cands: return 0
    if best is None: return 1
    i, cands = best; n = 0
    for v in cands:
        g[i] = v; n += sudoku_solutions(g, limit - n)
        if n >= limit: break
    g[i] = 0
    return n


def sudoku_fill(rng):
    g = [0] * 81
    def fill(i):
        if i == 81: return True
        r, c = divmod(i, 9); used = set(g[r * 9:(r + 1) * 9]) | {g[c + 9 * k] for k in range(9)} | {g[(r // 3 * 3 + k // 3) * 9 + c // 3 * 3 + k % 3] for k in range(9)}
        for v in rng.sample(range(1, 10), 9):
            if v not in used:
                g[i] = v
                if fill(i + 1): return True
        g[i] = 0; return False
    fill(0); return g


def sudoku_make(seed, level):
    rng = random.Random(seed); full = sudoku_fill(rng); g = full[:]
    target = {"easy": 38, "medium": 48, "hard": 56}[level]
    order = list(range(81)); rng.shuffle(order); removed = 0
    for i in order:
        if removed >= target: break
        v = g[i]; g[i] = 0
        if sudoku_solutions(g[:]) == 1: removed += 1
        else: g[i] = v
    return g, full


def sudoku_image(g, width=W, givens=None):
    cell = (width - 36) // 9; size = cell * 9; ox = (width - size) // 2; oy = 8
    im = Image.new("L", (width, size + 16), 255); d = ImageDraw.Draw(im); f = font(int(cell * 0.68)); fs = font(int(cell * 0.5))
    for k in range(10):
        w = 5 if k % 3 == 0 else 1
        d.line([(ox + k * cell, oy), (ox + k * cell, oy + size)], fill=0, width=w); d.line([(ox, oy + k * cell), (ox + size, oy + k * cell)], fill=0, width=w)
    for i, v in enumerate(g):
        if v:
            r, c = divmod(i, 9); box = (ox + c * cell, oy + r * cell, ox + (c + 1) * cell, oy + (r + 1) * cell)
            text_center(d, box, str(v), f if (givens is None or givens[i]) else fs)
    return im


@register
class Sudoku(Module):
    name = "sudoku"; title = "Sudoku"; blurb = "A fresh puzzle, numbered so it can be reprinted or solved later. Easy is a cup of coffee; hard is the whole pot."
    icon = "grid"; group = "play"
    fields = [Field("level", "Level", "select", default="medium", options=[["easy", "Easy"], ["medium", "Medium"], ["hard", "Hard"]]),
              Field("number", "Puzzle number", "text", placeholder="blank = new puzzle", help="Printed on the slip; type it here to print it again"),
              Field("solution", "Print the solution instead", "checkbox", default=False)]

    def build(self, p):
        seed = seed_of(p); g, full = sudoku_make(seed, p["level"])
        head = f"#{seed} · {p['level']}"
        if p["solution"]:
            return {"blocks": [{"t": "title", "text": "Sudoku, solved"}, {"t": "small", "text": head, "align": "center"},
                               {"t": "image", "b64": png(sudoku_image(full, 420, givens=[bool(v) for v in g])), "width": 420, "dither": False}, {"t": "small", "text": "big digits were given, small ones are the answer", "align": "center"}] + footer("Char")}
        return {"blocks": [{"t": "title", "text": "Sudoku"}, {"t": "small", "text": head, "align": "center"}, {"t": "image", "b64": png(sudoku_image(g)), "dither": False},
                           {"t": "small", "text": "Solution: print Sudoku again with this number and 'solution' ticked.", "align": "center"}] + footer("Char")}


# ---------------------------------------------------------------- word search

THEMES = {
    "beach": ["TIDE", "CRAB", "KELP", "ORCA", "HERON", "DRIFTWOOD", "CLAM", "SEAL", "SAND", "WAVE", "EAGLE", "FERRY", "ANCHOR", "SHELL", "GULL", "OYSTER"],
    "kitchen": ["WHISK", "SPOON", "BUTTER", "FLOUR", "SUGAR", "OVEN", "PANCAKE", "TACO", "SOUP", "KETTLE", "APRON", "LADLE", "GRATER", "TIMER", "SALT", "ONION"],
    "animals": ["OTTER", "MOOSE", "RABBIT", "BEAVER", "COYOTE", "RACCOON", "OWL", "DEER", "FROG", "SALMON", "SQUIRREL", "BEAR", "HAWK", "ELK", "NEWT", "SKUNK"],
    "birds": ["ROBIN", "CHICKADEE", "JUNCO", "FLICKER", "WREN", "TOWHEE", "CROW", "RAVEN", "SPARROW", "FINCH", "HERON", "OSPREY", "EAGLE", "NUTHATCH", "SISKIN", "KINGLET"],
    "space": ["PLANET", "COMET", "ROCKET", "ORBIT", "MOON", "SATURN", "GALAXY", "METEOR", "STAR", "NEBULA", "CRATER", "JUPITER", "ECLIPSE", "ALIEN", "MARS", "VENUS"],
    "dinosaurs": ["TREX", "RAPTOR", "FOSSIL", "CLAW", "SCALES", "JURASSIC", "EGG", "HERD", "TAIL", "SPIKES", "BONES", "ROAR", "VOLCANO", "FERN", "AMBER", "TRIASSIC"],
    "garden": ["TULIP", "SHOVEL", "COMPOST", "SEED", "WORM", "ROSE", "TOMATO", "HOSE", "BEES", "MULCH", "DAHLIA", "SPROUT", "TROWEL", "FENCE", "WEED", "RAIN"],
    "holidays": ["SNOW", "GIFT", "CANDLE", "COOKIE", "TURKEY", "PUMPKIN", "LIGHTS", "SLEIGH", "MITTENS", "COCOA", "WREATH", "FROST", "CAROL", "FEAST", "SCARF", "STAR"],
    "school": ["PENCIL", "RECESS", "TEACHER", "BOOK", "ERASER", "LUNCH", "SCIENCE", "MATH", "BUS", "DESK", "CRAYON", "GLUE", "LIBRARY", "MAP", "QUIZ", "ART"],
    "sports": ["SOCCER", "GOAL", "SKATE", "BIKE", "HOCKEY", "SWIM", "TENNIS", "BASEBALL", "KAYAK", "HIKE", "RUN", "CATCH", "WHISTLE", "TEAM", "PUCK", "JERSEY"],
}


def wordsearch_make(seed, level, words):
    rng = random.Random(seed)
    n, count, dirs = {"easy": (10, 8, [(1, 0), (0, 1)]), "medium": (12, 10, [(1, 0), (0, 1), (1, 1), (1, -1)]),
                      "hard": (15, 14, [(1, 0), (0, 1), (1, 1), (1, -1), (-1, 0), (0, -1), (-1, -1), (-1, 1)])}[level]
    words = [w for w in words if 2 < len(w) <= n]; rng.shuffle(words); words = sorted(words[:count], key=len, reverse=True)
    grid = [[""] * n for _ in range(n)]; placed = []
    for w in words:
        for _ in range(300):
            dx, dy = rng.choice(dirs); x, y = rng.randrange(n), rng.randrange(n)
            xe, ye = x + dx * (len(w) - 1), y + dy * (len(w) - 1)
            if not (0 <= xe < n and 0 <= ye < n): continue
            if all(grid[y + dy * k][x + dx * k] in ("", w[k]) for k in range(len(w))):
                for k in range(len(w)): grid[y + dy * k][x + dx * k] = w[k]
                placed.append((w, x, y, dx, dy)); break
    for r in range(n):
        for c in range(n):
            if not grid[r][c]: grid[r][c] = rng.choice("AABCDEEEFGHIIJKLMNOOPQRSSTUVWXYZ")
    return grid, placed


def wordsearch_image(grid, placed=None, width=W):
    n = len(grid); cell = (width - 24) // n; size = cell * n; ox = (width - size) // 2; oy = 6
    im = Image.new("L", (width, size + 12), 255); d = ImageDraw.Draw(im); f = font(int(cell * 0.62))
    if placed:   # capsules around each word: a fat black stroke, then a slightly thinner white one, letters drawn on top
        for w, x, y, dx, dy in placed:
            x0, y0 = ox + x * cell + cell / 2, oy + y * cell + cell / 2; x1, y1 = ox + (x + dx * (len(w) - 1)) * cell + cell / 2, oy + (y + dy * (len(w) - 1)) * cell + cell / 2
            for fill, wd in ((0, int(cell * 0.92)), (255, int(cell * 0.92) - 5)):
                d.line([(x0, y0), (x1, y1)], fill=fill, width=wd)
                for cx, cy in ((x0, y0), (x1, y1)): d.ellipse([cx - wd / 2, cy - wd / 2, cx + wd / 2, cy + wd / 2], fill=fill)
    for r in range(n):
        for c in range(n): text_center(d, (ox + c * cell, oy + r * cell, ox + (c + 1) * cell, oy + (r + 1) * cell), grid[r][c], f)
    d.rectangle([ox - 4, oy - 4, ox + size + 4, oy + size + 4], outline=0, width=2)
    return im


@register
class WordSearch(Module):
    name = "wordsearch"; title = "Word search"; blurb = "A themed grid of hidden words, or your own list. Easy reads left-to-right and down; hard hides them backwards and on the diagonal."
    icon = "letters"; group = "play"
    fields = [Field("theme", "Theme", "select", default="beach", options=[[k, k.title()] for k in THEMES] + [["custom", "My own words"]]),
              Field("words", "Your words", "text", placeholder="comma separated, for the custom theme", help="Names, spelling words, a grocery list..."),
              Field("level", "Level", "select", default="medium", options=[["easy", "Easy, 10×10"], ["medium", "Medium, 12×12"], ["hard", "Hard, 15×15"]]),
              Field("number", "Puzzle number", "text", placeholder="blank = new puzzle"), Field("solution", "Print the solution instead", "checkbox", default=False)]

    def build(self, p):
        import re
        seed = seed_of(p)
        words = [re.sub(r"[^A-Z]", "", w.upper()) for w in p["words"].split(",")] if p["theme"] == "custom" else THEMES[p["theme"]]
        words = [w for w in words if w]
        if len(words) < 3: raise ValueError("Give at least three words for a custom search")
        grid, placed = wordsearch_make(seed, p["level"], words)
        head = f"#{seed} · {p['theme'] if p['theme'] != 'custom' else 'custom'} · {p['level']}"
        if p["solution"]:
            return {"blocks": [{"t": "title", "text": "Word search, solved"}, {"t": "small", "text": head, "align": "center"},
                               {"t": "image", "b64": png(wordsearch_image(grid, placed)), "dither": False}] + footer("Char")}
        names = sorted(w for w, *_ in placed)
        rows = [names[i:i + 2] + [""] * (2 - len(names[i:i + 2])) for i in range(0, len(names), 2)]
        return {"blocks": [{"t": "title", "text": "Word search"}, {"t": "small", "text": head, "align": "center"}, {"t": "image", "b64": png(wordsearch_image(grid)), "dither": False},
                           {"t": "table", "rows": rows}, {"t": "small", "text": "Solution: print again with this number and 'solution' ticked.", "align": "center"}] + footer("Char")}


# ---------------------------------------------------------------- maze

def maze_make(seed, cols, rows):
    rng = random.Random(seed); walls = {}   # (x, y) -> set of open directions
    visited = [[False] * cols for _ in range(rows)]; openings = [[set() for _ in range(cols)] for _ in range(rows)]
    stack = [(0, 0)]; visited[0][0] = True
    while stack:
        x, y = stack[-1]
        nbrs = [(dx, dy) for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)) if 0 <= x + dx < cols and 0 <= y + dy < rows and not visited[y + dy][x + dx]]
        if not nbrs: stack.pop(); continue
        dx, dy = rng.choice(nbrs); openings[y][x].add((dx, dy)); openings[y + dy][x + dx].add((-dx, -dy)); visited[y + dy][x + dx] = True; stack.append((x + dx, y + dy))
    return openings


def maze_solve(openings, cols, rows):
    from collections import deque
    prev = {(0, 0): None}; q = deque([(0, 0)])
    while q:
        x, y = q.popleft()
        if (x, y) == (cols - 1, rows - 1): break
        for dx, dy in openings[y][x]:
            if (x + dx, y + dy) not in prev: prev[(x + dx, y + dy)] = (x, y); q.append((x + dx, y + dy))
    path, cur = [], (cols - 1, rows - 1)
    while cur: path.append(cur); cur = prev[cur]
    return path[::-1]


def maze_image(openings, cols, rows, path=None, width=W):
    cell = (width - 24) // cols; ox = (width - cell * cols) // 2; oy = 8
    im = Image.new("L", (width, cell * rows + 16), 255); d = ImageDraw.Draw(im); lw = max(3, cell // 7)
    for y in range(rows):
        for x in range(cols):
            x0, y0 = ox + x * cell, oy + y * cell
            if (0, -1) not in openings[y][x] and not (x == 0 and y == 0): d.line([(x0, y0), (x0 + cell, y0)], fill=0, width=lw)
            if (-1, 0) not in openings[y][x]: d.line([(x0, y0), (x0, y0 + cell)], fill=0, width=lw)
    d.line([(ox, oy + cell * rows), (ox + cell * cols, oy + cell * rows)], fill=0, width=lw)
    d.line([(ox + cell * cols, oy), (ox + cell * cols, oy + cell * (rows - 1))], fill=0, width=lw)   # exit on the right of the last cell
    r = max(3, cell // 4); cx, cy = ox + cell / 2, oy + cell / 2; ex, ey = ox + (cols - 0.5) * cell, oy + (rows - 0.5) * cell
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=0)                       # start: a dot
    d.ellipse([ex - r, ey - r, ex + r, ey + r], outline=0, width=max(2, cell // 8))   # end: a ring
    if path:   # the answer as a trail of small dots, so it reads differently from the walls
        rr = max(2, cell // 7)
        for x, y in path:
            px, py = ox + x * cell + cell / 2, oy + y * cell + cell / 2; d.ellipse([px - rr, py - rr, px + rr, py + rr], fill=0)
    return im


@register
class Maze(Module):
    name = "maze"; title = "Maze"; blurb = "Start at the top, out at the bottom. Easy is a minute with a crayon; hard is a whole snack."
    icon = "maze"; group = "play"
    fields = [Field("level", "Level", "select", default="medium", options=[["easy", "Easy, 12 wide"], ["medium", "Medium, 18 wide"], ["hard", "Hard, 26 wide"]]),
              Field("number", "Puzzle number", "text", placeholder="blank = new maze"), Field("solution", "Print the solution instead", "checkbox", default=False)]

    def build(self, p):
        seed = seed_of(p); cols, rows = {"easy": (12, 16), "medium": (18, 24), "hard": (26, 34)}[p["level"]]
        op = maze_make(seed, cols, rows); head = f"#{seed} · {p['level']}"
        path = maze_solve(op, cols, rows) if p["solution"] else None
        return {"blocks": [{"t": "title", "text": "Maze" + (", solved" if path else "")}, {"t": "small", "text": head, "align": "center"},
                           {"t": "image", "b64": png(maze_image(op, cols, rows, path)), "dither": False}] + ([] if path else [{"t": "small", "text": "Solution: print again with this number and 'solution' ticked.", "align": "center"}]) + footer("Char")}
