#!/usr/bin/python3
"""
sway_wp.py: the window manager, explained by its own configuration, as wallpaper.

Every value on the wallpaper is read from the configuration that styles and
drives the running session, and every on-screen thing is drawn the way the
program that owns it draws it:

  * text is laid out with Pango and painted with Cairo, sway's own text stack,
    with sway's font-metric rule (height = ascent + descent, as in
    common/pango.c) and its hinting (full, grey antialias unless the output
    reports a subpixel layout)
  * window decorations follow sway 1.11's geometry: titlebar height =
    font_height + 2 * vertical titlebar_padding, a titlebar_border_thickness
    frame, title text at the horizontal padding (title_align aware), child
    borders and the split indicator on the right/bottom edge, and workspace
    gaps = outer + inner with inner gaps between siblings
  * the bar, notifications, launcher, lock screen, nag bar and terminal are
    drawn from their own config files (or the program's dump/defaults)

Which elements appear, and in which order, is set in sway_wp-elements.config:
    $XDG_CONFIG_HOME/sway_wp/sway_wp-elements.config   (per user, wins)
    /etc/sway_wp/sway_wp-elements.config               (system default)
Each element reads only the sources it needs, so a disabled element costs
nothing. `sway_wp.py --list` prints every element and where its values come
from.

Triggered by `exec_always` in /etc/sway/config.d/99-sway_wp.conf, i.e. on every
sway start and reload (Mod4+Shift+c), the same event that applies changes to
every value shown here. Applies the result itself via sway IPC.

Dependencies: python3-gobject, python3-cairo, pango (all part of Fedora Sway).
"""

import argparse
import configparser
import functools
import glob
import json
import math
import os
import re
import shlex
import socket
import struct
import subprocess
import sys
import traceback
from datetime import datetime
from pathlib import Path

import cairo
import gi

gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import GLib, Pango, PangoCairo  # noqa: E402

NAME = "sway_wp"
HOME = Path.home()
XDG_CONFIG = Path(os.environ.get("XDG_CONFIG_HOME") or HOME / ".config")
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME") or HOME / ".cache") / NAME
LOG_FILE = CACHE_DIR / "sway_wp.log"
ELEMENTS_FILE = "sway_wp-elements.config"
ELEMENTS_PATHS = [XDG_CONFIG / NAME / ELEMENTS_FILE, Path("/etc") / NAME / ELEMENTS_FILE]

GERMAN_CHARS = "äöüßÄÖÜẞ"
DEFAULT = "default"          # reference label for built-in program defaults
LIVE = "ipc"                 # reference label for live values from sway IPC


def log(msg):
    print(msg, file=sys.stderr)


# ===========================================================================
# sway IPC (pure python, no swaymsg needed)
# ===========================================================================

IPC_MAGIC = b"i3-ipc"
RUN_COMMAND, GET_WORKSPACES, GET_OUTPUTS, GET_TREE = 0, 1, 3, 4
GET_BAR_CONFIG, GET_VERSION, GET_CONFIG, GET_INPUTS, GET_SEATS = 6, 7, 9, 100, 101


class SwayIPC:
    def __init__(self):
        self.sock = None
        candidates = []
        if os.environ.get("SWAYSOCK"):
            candidates.append(os.environ["SWAYSOCK"])
        candidates += sorted(glob.glob(f"/run/user/{os.getuid()}/sway-ipc.{os.getuid()}.*.sock"))
        for path in candidates:
            try:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.settimeout(2)
                s.connect(path)
                self.sock = s
                self.version = self._msg(GET_VERSION)  # probe: stale sockets fail here
                break
            except OSError:
                if self.sock:
                    self.sock.close()
                self.sock = None
        if not self.sock:
            raise RuntimeError("no running sway found (no usable IPC socket)")

    def _recv_exact(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise RuntimeError("sway IPC connection closed")
            buf += chunk
        return buf

    def _msg(self, mtype, payload=""):
        data = payload.encode()
        self.sock.sendall(IPC_MAGIC + struct.pack("<II", len(data), mtype) + data)
        if self._recv_exact(6) != IPC_MAGIC:
            raise RuntimeError("bad sway IPC reply")
        length, _ = struct.unpack("<II", self._recv_exact(8))
        body = self._recv_exact(length)
        return json.loads(body) if body else None

    def outputs(self):
        return [o for o in self._msg(GET_OUTPUTS) if o.get("active")]

    def inputs(self):
        return self._msg(GET_INPUTS)

    def seats(self):
        return self._msg(GET_SEATS)

    def tree(self):
        return self._msg(GET_TREE)

    def bar_ids(self):
        return self._msg(GET_BAR_CONFIG)

    def bar_config(self, bar_id):
        return self._msg(GET_BAR_CONFIG, bar_id)

    def command(self, cmd):
        return self._msg(RUN_COMMAND, cmd)


# ===========================================================================
# generic helpers: references, colors, small parsers
# ===========================================================================

def ref(path, line=None):
    """Short source reference shown next to a value: file:line."""
    name = Path(path).name
    return f"{name}:{line}" if line else name


def first_existing(*paths):
    for p in paths:
        if p and Path(p).is_file():
            return Path(p)
    return None


NAMED_COLORS = {
    "transparent": (0, 0, 0, 0), "white": (1, 1, 1, 1), "black": (0, 0, 0, 1),
    "red": (1, 0, 0, 1), "green": (0, 0.5, 0, 1), "blue": (0, 0, 1, 1),
    "gray": (0.5, 0.5, 0.5, 1), "grey": (0.5, 0.5, 0.5, 1),
    "yellow": (1, 1, 0, 1), "orange": (1, 0.65, 0, 1),
}


def parse_color(s):
    """CSS/sway/swaylock/foot/rofi color -> (r, g, b, a) floats, or None."""
    if s is None:
        return None
    s = s.strip().strip("'\"").strip()
    if s.lower() in NAMED_COLORS:
        return NAMED_COLORS[s.lower()]
    m = re.fullmatch(r"(?:#|0x)?([0-9a-fA-F]{3,4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})", s)
    if m and (s.startswith(("#", "0x")) or len(m.group(1)) in (6, 8)):
        h = m.group(1)
        if len(h) in (3, 4):
            h = "".join(2 * c for c in h)
        vals = [int(h[i:i + 2], 16) / 255 for i in range(0, len(h), 2)]
        return tuple(vals) + ((1.0,) if len(vals) == 3 else ())
    m = re.fullmatch(r"rgba?\s*\(([^)]*)\)", s)
    if m:
        parts = [p.strip() for p in re.split(r"[,\s/]+", m.group(1).strip()) if p.strip()]
        if len(parts) >= 3:
            def chan(p):
                return float(p[:-1]) / 100 if p.endswith("%") else float(p) / 255
            parts_clean = []
            i = 0
            while i < len(parts):  # rofi writes "100 %"
                if i + 1 < len(parts) and parts[i + 1] == "%":
                    parts_clean.append(parts[i] + "%")
                    i += 2
                else:
                    parts_clean.append(parts[i])
                    i += 1
            rgb = tuple(chan(p) for p in parts_clean[:3])
            a = 1.0
            if len(parts_clean) > 3:
                p = parts_clean[3]
                a = float(p[:-1]) / 100 if p.endswith("%") else float(p)
            return rgb + (a,)
    return None


def over(fg, bg):
    """Composite a (possibly translucent) color over an opaque base."""
    a = fg[3]
    return tuple(a * c + (1 - a) * d for c, d in zip(fg[:3], bg[:3])) + (1.0,)


def mix(a, b, t):
    return tuple(x * (1 - t) + y * t for x, y in zip(a[:3], b[:3])) + (1.0,)


def hexs(c):
    s = "#" + "".join(f"{round(v * 255):02x}" for v in c[:3])
    return s + (f"{round(c[3] * 255):02x}" if len(c) > 3 and c[3] < 0.999 else "")


def px(value, default=0.0):
    m = re.search(r"(-?[\d.]+)\s*px", value or "")
    if m:
        return float(m.group(1))
    m = re.fullmatch(r"\s*(-?[\d.]+)\s*", value or "")
    return float(m.group(1)) if m else default


def box4(value):
    """CSS 1-4 value shorthand -> (top, right, bottom, left) in px."""
    parts = (value or "0").split()
    v = [px(p) for p in parts] or [0.0]
    if len(v) == 1:
        return v * 4
    if len(v) == 2:
        return [v[0], v[1], v[0], v[1]]
    if len(v) == 3:
        return [v[0], v[1], v[2], v[1]]
    return v[:4]


def strip_jsonc(text):
    """Remove // and /* */ comments and trailing commas, respecting strings."""
    def scan(text, handle_comma):
        out, i, n = [], 0, len(text)
        while i < n:
            c = text[i]
            if c == '"':
                j = i + 1
                while j < n:
                    if text[j] == "\\":
                        j += 2
                        continue
                    if text[j] == '"':
                        break
                    j += 1
                out.append(text[i:j + 1])
                i = j + 1
            elif text.startswith("//", i):
                while i < n and text[i] != "\n":
                    i += 1
            elif text.startswith("/*", i):
                end = text.find("*/", i + 2)
                i = n if end < 0 else end + 2
            elif c == "," and handle_comma:
                j = i + 1
                while j < n and text[j] in " \t\r\n":
                    j += 1
                if j < n and text[j] in "}]":
                    i += 1
                else:
                    out.append(c)
                    i += 1
            else:
                out.append(c)
                i += 1
        return "".join(out)
    return scan(scan(text, False), True)


def parse_css(text):
    """Flat selector -> {prop: value} map (GTK CSS / rofi rasi are close enough)."""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = re.sub(r"@keyframes[^{]*\{(?:[^{}]*|\{[^{}]*\})*\}", "", text)
    rules = {}
    for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", text):
        props = {}
        for decl in m.group(2).split(";"):
            if ":" in decl:
                k, v = decl.split(":", 1)
                props[k.strip()] = v.strip()
        for sel in m.group(1).split(","):
            sel = " ".join(sel.split())
            if sel:
                rules.setdefault(sel, {}).update(props)
    return rules


def read_ini(paths, comment_defaults=False):
    """Read key=value ini files (foot, dunst, swaylock style) into
    {section: {key: (value, ref)}}. Later files override earlier ones. With
    comment_defaults, `# key=value` lines are taken as documented defaults."""
    data = {}
    for path in paths:
        if not path or not Path(path).is_file():
            continue
        section = ""
        for i, raw in enumerate(Path(path).read_text(errors="replace").splitlines(), 1):
            line = raw.strip()
            m = re.match(r"^\[([^\]]+)\]", line)
            if m:
                section = m.group(1).strip()
                continue
            is_default = False
            if line.startswith(("#", ";")):
                if not comment_defaults:
                    continue
                line = line.lstrip("#; ").strip()
                is_default = True
            m = re.match(r"^([A-Za-z0-9_.\-]+)\s*=\s*(.*)$", line)
            if not m:
                continue
            key, val = m.group(1), m.group(2)
            if is_default:
                val = re.split(r"\s+#", val)[0].strip()
                if val.startswith("<") or key in data.get(section, {}):
                    continue
                data.setdefault(section, {})[key] = (val, DEFAULT)
            else:
                val = val.strip().strip('"')
                data.setdefault(section, {})[key] = (val, ref(path, i))
    return data


# ===========================================================================
# sway config tree: locate, follow includes, parse
# ===========================================================================

def find_main_config():
    for p in (XDG_CONFIG / "sway/config", HOME / ".config/sway/config",
              Path("/etc/sway/config"), Path("/usr/etc/sway/config")):
        if p.is_file():
            return p
    raise RuntimeError("no sway config found")


def expand_include(arg):
    """Resolve an `include` argument the way sway does (wordexp(3))."""
    arg = arg.strip()
    if arg[:1] in ("'", '"') and arg[-1:] == arg[:1]:
        arg = arg[1:-1]
    if "$(" in arg or "`" in arg:
        out = subprocess.run(["sh", "-c", f'printf "%s\\n" {arg}'],
                             capture_output=True, text=True, timeout=10)
        return [l for l in out.stdout.splitlines() if l.strip()]
    return sorted(glob.glob(os.path.expanduser(os.path.expandvars(arg))))


def load_config_tree(path, seen=None):
    """Return [(path, text)] in sway load order, following includes."""
    if seen is None:
        seen = set()
    path = Path(path).resolve()
    if path in seen or not path.is_file():
        return []
    seen.add(path)
    result = [(str(path), path.read_text(errors="replace"))]
    text = re.sub(r"\\\n", " ", result[0][1])
    for m in re.finditer(r"^\s*include\s+(.+?)\s*$", text, re.M):
        for inc in expand_include(m.group(1)):
            result += load_config_tree(inc, seen)
    return result


def logical_lines(text):
    """Yield (first line number, line) with backslash continuations joined."""
    buf, start = "", None
    for i, line in enumerate(text.splitlines(), 1):
        if start is None:
            start = i
        if line.endswith("\\"):
            buf += line[:-1] + " "
            continue
        yield start, buf + line
        buf, start = "", None
    if buf:
        yield start, buf


def expand_vars(s, variables):
    for _ in range(5):
        new = re.sub(r"\$([A-Za-z_][A-Za-z0-9_]*)",
                     lambda m: variables.get(m.group(1), m.group(0)), s)
        if new == s:
            break
        s = new
    return s


def section_from_filename(path):
    name = Path(path).stem
    name = re.sub(r"^\d+-", "", name)
    name = re.sub(r"^bindings-", "", name)
    return name.replace("-", " ").capitalize()


class Binding:
    __slots__ = ("keys", "command", "gloss", "mode", "locked", "section", "order", "ref")

    def __init__(self, keys, command, gloss, mode, locked, section, order, ref_):
        self.keys, self.command, self.gloss = keys, command, gloss
        self.mode, self.locked, self.section, self.order = mode, locked, section, order
        self.ref = ref_


class SwayConfig:
    """Every directive of the loaded config tree, with its file:line.

    Block forms are flattened the way sway itself treats them:
    `input X { a b }` == `input X a b`, `bar { colors { c d } }` -> `bar.colors.c d`.
    """

    def __init__(self, tree):
        self.tree = tree
        self.variables = {}
        self.bindings = []
        self.directives = {}
        self._parse()

    def add(self, key, args, r):
        self.directives.setdefault(key, []).append((args, r))

    def get(self, key, default=None):
        """Last value of a directive -> (value, ref); sway default if unset."""
        v = self.directives.get(key)
        if v:
            return v[-1]
        if default is None:
            default = SWAY_DEFAULTS.get(key)
        return (default, DEFAULT) if default is not None else (None, None)

    def all(self, key):
        return self.directives.get(key, [])

    def _parse(self):
        order = 0
        for path, text in self.tree:
            section = section_from_filename(path) if Path(path).parent.name == "config.d" else None
            stack, saved_section, comments = [], None, []
            for lineno, raw in logical_lines(text):
                line = raw.strip()
                r = ref(path, lineno)
                if not line:
                    comments = []
                    continue
                if line.startswith("#"):
                    comment = line.lstrip("#").strip()
                    if not comment:
                        comments = []
                        continue
                    m = re.match(r"^(.+):$", comment)
                    if m and not stack:
                        section = m.group(1).strip()
                        comments = []
                    else:
                        comments.append(comment)
                    continue
                gloss = comments[0] if comments else None
                comments = []
                mode = next((a for k, a in reversed(stack) if k == "mode"), None)

                if line == "}":
                    if stack:
                        kind, _ = stack.pop()
                        if kind == "mode":
                            section = saved_section
                    continue

                if line.endswith("{"):
                    head = expand_vars(line[:-1].strip(), self.variables)
                    words = head.split()
                    kind = words[0] if words else ""
                    if kind == "mode":
                        name = " ".join(w for w in words[1:] if not w.startswith("--")).strip("\"'")
                        stack.append(("mode", name))
                        saved_section, section = section, f"{name} mode"
                    elif kind in ("bindsym", "bindcode"):
                        stack.append(("bind", words[1:]))
                    else:
                        stack.append((kind, " ".join(words[1:])))
                    continue

                if stack and stack[-1][0] == "bind":
                    parts = line.split(None, 1)
                    if len(parts) == 2:
                        self.bindings.append(Binding(
                            expand_vars(parts[0], self.variables),
                            expand_vars(parts[1], self.variables), gloss, mode,
                            "--locked" in stack[-1][1], section, order, r))
                        order += 1
                    continue

                m = re.match(r"^set\s+\$(\w+)\s+(.+)$", line)
                if m:
                    self.variables[m.group(1)] = m.group(2).strip().strip("'\"")
                    continue
                if re.match(r"^include\s+", line):
                    continue  # already followed by load_config_tree

                m = re.match(r"^(bindsym|bindcode)\s+((?:--[\w-]+(?:=\S+)?\s+)*)(\S+)\s+(.+)$", line)
                if m:
                    _, flags, keys, cmd = m.groups()
                    keys = expand_vars(keys, self.variables)
                    cmd = expand_vars(cmd, self.variables)
                    sec = section
                    mm = re.match(r'^mode\s+"?([^"]+)"?$', cmd)
                    if mm and mode is None:
                        sec = f"{mm.group(1)} mode"  # mode-entry binds live with their mode
                    self.bindings.append(Binding(keys, cmd, gloss, mode,
                                                 "--locked" in flags.split(), sec, order, r))
                    order += 1
                    continue

                words = line.split(None, 1)
                key = words[0]
                args = expand_vars(words[1], self.variables) if len(words) > 1 else ""
                outer = [s for s in stack if s[0] != "mode"]
                if outer:
                    top = outer[0]
                    if top[0] in ("input", "output", "seat"):
                        self.add(top[0], f"{top[1]} {key} {args}".strip(), r)
                    elif top[0] == "bar":
                        sub = ".".join(k for k, _ in outer[1:])
                        self.add("bar." + (sub + "." if sub else "") + key, args, r)
                    else:
                        self.add(f"{top[0]}.{key}", args, r)
                else:
                    self.add(key, args, r)


# Built-in values of sway 1.11 (sway/config.c, sway/commands/*.c), shown with
# the reference "default" whenever the config does not set them.
SWAY_DEFAULTS = {
    "font": "monospace 10",
    "titlebar_border_thickness": "1",
    "titlebar_padding": "5 4",
    "title_align": "left",
    "default_border": "normal 2",
    "default_floating_border": "normal 2",
    "hide_edge_borders": "none",
    "smart_borders": "off",
    "smart_gaps": "off",
    "show_marks": "yes",
    "focus_follows_mouse": "yes",
    "mouse_warping": "output",
    "focus_wrapping": "yes",
    "focus_on_window_activation": "urgent",
    "workspace_auto_back_and_forth": "no",
    "popup_during_fullscreen": "smart",
    "tiling_drag": "enable",
    "tiling_drag_threshold": "9",
    "workspace_layout": "default",
    "default_orientation": "auto",
    "floating_minimum_size": "75 x 50",
    "floating_maximum_size": "0 x 0 (unlimited)",
    "xwayland": "enable (lazy)",
    "primary_selection": "enabled",
    "floating_modifier": "none",
    "client.focused": "#4c7899 #285577 #ffffff #2e9ef4 #285577",
    "client.focused_inactive": "#333333 #5f676a #ffffff #484e50 #5f676a",
    "client.unfocused": "#333333 #222222 #888888 #292d2e #222222",
    "client.urgent": "#2f343a #900000 #ffffff #900000 #900000",
    "client.placeholder": "#000000 #0c0c0c #ffffff #000000 #0c0c0c",
}
CLIENT_CLASSES = ["focused", "focused_inactive", "focused_tab_title", "unfocused",
                  "urgent", "placeholder"]
CLIENT_PARTS = ["border", "background", "text", "indicator", "child_border"]


# ===========================================================================
# rendering: Pango + Cairo exactly as sway uses them
# ===========================================================================

def font_desc(spec, absolute_px=None):
    desc = Pango.FontDescription.from_string(spec)
    if absolute_px:
        desc.set_absolute_size(absolute_px * Pango.SCALE)
    return desc


def text_metrics(desc):
    """sway common/pango.c get_text_metrics(): (height, baseline) in px."""
    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, 1, 1)
    pctx = PangoCairo.create_context(cairo.Context(surf))
    pctx.set_round_glyph_positions(False)
    m = pctx.get_metrics(desc, None)
    baseline = m.get_ascent() // Pango.SCALE
    return baseline + m.get_descent() // Pango.SCALE, baseline


class Canvas:
    """A cairo image surface in logical (config) pixels, scaled to device px."""

    def __init__(self, width, height, scale=1.0, subpixel="unknown"):
        self.W, self.H, self.scale = width, height, scale
        self.surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, width, height)
        self.cr = cairo.Context(self.surface)
        # sway_text_node.c: full hinting, grey AA unless a subpixel order is known
        self.fo = cairo.FontOptions()
        self.fo.set_hint_style(cairo.HINT_STYLE_FULL)
        order = {"rgb": cairo.SUBPIXEL_ORDER_RGB, "bgr": cairo.SUBPIXEL_ORDER_BGR,
                 "vrgb": cairo.SUBPIXEL_ORDER_VRGB, "vbgr": cairo.SUBPIXEL_ORDER_VBGR}
        if subpixel in order:
            self.fo.set_antialias(cairo.ANTIALIAS_SUBPIXEL)
            self.fo.set_subpixel_order(order[subpixel])
        else:
            self.fo.set_antialias(cairo.ANTIALIAS_GRAY)
        self.set_zoom(1.0)

    def set_zoom(self, zoom):
        self.zoom = zoom
        self.cr.identity_matrix()
        self.cr.scale(self.scale * zoom, self.scale * zoom)
        self.cr.set_antialias(cairo.ANTIALIAS_BEST)
        self.cr.set_font_options(self.fo)
        self.lw = self.W / (self.scale * zoom)   # logical canvas size
        self.lh = self.H / (self.scale * zoom)

    # --- text (sway common/pango.c get_pango_layout) -----------------------
    def layout(self, text, desc, markup=False, max_w=None, wrap=False):
        lay = PangoCairo.create_layout(self.cr)
        pctx = lay.get_context()
        pctx.set_round_glyph_positions(False)
        PangoCairo.context_set_font_options(pctx, self.fo)
        attrs = None
        if markup:
            try:
                _, attrs, plain, _ = Pango.parse_markup(text, -1, "\0")
                text = plain
            except GLib.Error:
                attrs = None
        lay.set_text(text, -1)
        if attrs is not None:
            lay.set_attributes(attrs)
        lay.set_font_description(desc)
        if not wrap:
            lay.set_single_paragraph_mode(True)
        if max_w is not None:
            lay.set_width(max(1, int(max_w * Pango.SCALE)))
            if wrap:
                lay.set_wrap(Pango.WrapMode.WORD_CHAR)
            else:
                lay.set_ellipsize(Pango.EllipsizeMode.END)
        PangoCairo.update_layout(self.cr, lay)
        return lay

    def measure(self, text, desc, markup=False, max_w=None, wrap=False):
        lay = self.layout(text, desc, markup, max_w, wrap)
        w, h = lay.get_pixel_size()
        return w, h, lay.get_baseline() / Pango.SCALE

    def text(self, x, y, text, desc, color, markup=False, max_w=None, align="left",
             valign="top", box_h=None, wrap=False):
        lay = self.layout(text, desc, markup, max_w, wrap)
        w, h = lay.get_pixel_size()
        if align == "right":
            x -= w
        elif align == "center":
            x -= w / 2
        if valign == "middle" and box_h is not None:
            y += (box_h - h) / 2
        self.cr.set_source_rgba(*color[:3], color[3] if len(color) > 3 else 1)
        self.cr.move_to(x, y)
        PangoCairo.show_layout(self.cr, lay)
        return w, h

    def rect(self, x, y, w, h, color):
        if w <= 0 or h <= 0 or color is None:
            return
        self.cr.set_source_rgba(*color[:3], color[3] if len(color) > 3 else 1)
        self.cr.rectangle(x, y, w, h)
        self.cr.fill()

    def frame(self, x, y, w, h, t, color):
        """Border of thickness t inside the rect."""
        if t <= 0:
            return
        self.rect(x, y, w, t, color)
        self.rect(x, y + h - t, w, t, color)
        self.rect(x, y + t, t, h - 2 * t, color)
        self.rect(x + w - t, y + t, t, h - 2 * t, color)

    def rounded(self, x, y, w, h, r, color, stroke=None, lw=0):
        cr = self.cr
        r = max(0, min(r, w / 2, h / 2))
        cr.new_sub_path()
        cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
        cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
        cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
        cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
        cr.close_path()
        if color:
            cr.set_source_rgba(*color[:3], color[3] if len(color) > 3 else 1)
            if stroke:
                cr.fill_preserve()
            else:
                cr.fill()
        if stroke and lw > 0:
            cr.set_source_rgba(*stroke[:3], stroke[3] if len(stroke) > 3 else 1)
            cr.set_line_width(lw)
            cr.stroke()
        cr.new_path()

    def clip(self, x, y, w, h):
        self.cr.save()
        self.cr.rectangle(x, y, w, h)
        self.cr.clip()

    def unclip(self):
        self.cr.restore()


# ===========================================================================
# context: every source, loaded lazily (only what enabled elements ask for)
# ===========================================================================

class Ctx:
    def __init__(self, ipc):
        self.ipc = ipc
        self.sources = {}     # path -> set of things read from it
        self.notes = []       # skipped elements etc.

    def use(self, path, what):
        if path:
            self.sources.setdefault(str(path), set()).add(what)

    # --- sway --------------------------------------------------------------
    @functools.cached_property
    def sway(self):
        main = find_main_config()
        sc = SwayConfig(load_config_tree(main))
        sc.main = main
        for p, _ in sc.tree:
            self.use(p, "sway")
        return sc

    @functools.cached_property
    def sway_style(self):
        return SwayStyle(self.sway)

    @functools.cached_property
    def inputs(self):
        return self.ipc.inputs()

    @functools.cached_property
    def outputs(self):
        return self.ipc.outputs()

    @functools.cached_property
    def keyboard(self):
        for i in self.inputs:
            if i.get("type") == "keyboard" and i.get("xkb_layout_names") and \
                    "Power_Button" not in i.get("identifier", ""):
                return i
        return next((i for i in self.inputs if i.get("type") == "keyboard"), {})

    # --- waybar: the bar, and the stylesheet the sheet itself is drawn in --
    @functools.cached_property
    def waybar(self):
        cfg_path = None
        for d in (XDG_CONFIG / "waybar", Path("/etc/xdg/waybar")):
            cfg_path = first_existing(d / "config.jsonc", d / "config")
            if cfg_path:
                break
        cfg, css, css_path = {}, {}, None
        if cfg_path:
            try:
                cfg = json.loads(strip_jsonc(cfg_path.read_text()))
            except json.JSONDecodeError as e:
                log(f"waybar config: {e}")
            if isinstance(cfg, list):
                cfg = cfg[0] if cfg else {}
            self.use(cfg_path, "bar")
            css_path = first_existing(cfg_path.parent / "style.css")
            if css_path:
                css = parse_css(css_path.read_text())
                self.use(css_path, "bar + sheet style")
        return {"cfg": cfg, "css": css, "cfg_path": cfg_path, "css_path": css_path}

    @functools.cached_property
    def theme(self):
        return Theme(self)

    @functools.cached_property
    def compose(self):
        return ComposeInfo(self)


class SwayStyle:
    """sway's decoration state, resolved exactly as sway resolves it."""

    def __init__(self, sc):
        self.sc = sc
        font, self.font_ref = sc.get("font")
        font = font.strip()
        self.markup = font.startswith("pango:")
        self.font_str = font[len("pango:"):] if self.markup else font
        self.font = font_desc(self.font_str)
        self.font_height, self.font_baseline = text_metrics(self.font)

        v, self.tbt_ref = sc.get("titlebar_border_thickness")
        self.tb_border = int(v)
        v, self.pad_ref = sc.get("titlebar_padding")
        p = v.split()
        self.tb_hpad = int(p[0])
        self.tb_vpad = int(p[1]) if len(p) > 1 else self.tb_hpad
        self.title_align, self.align_ref = sc.get("title_align")
        self.titlebar_h = self.font_height + 2 * self.tb_vpad

        self.border, self.border_w, self.border_ref = self._border("default_border")
        self.fborder, self.fborder_w, self.fborder_ref = self._border("default_floating_border")

        self.hide_edge, self.hide_edge_ref = sc.get("hide_edge_borders")
        self.smart_borders, self.smart_borders_ref = sc.get("smart_borders")
        self.smart_gaps, self.smart_gaps_ref = sc.get("smart_gaps")

        # gaps: `gaps inner|outer|horizontal|vertical|top|right|bottom|left N`
        self.gaps = {"inner": 0, "top": 0, "right": 0, "bottom": 0, "left": 0}
        self.gaps_ref = {k: DEFAULT for k in self.gaps}
        for args, r in sc.all("gaps"):
            w = args.split()
            if len(w) < 2:
                continue
            try:
                n = int(w[-1].rstrip("px"))
            except ValueError:
                continue
            sides = {"inner": ["inner"], "outer": ["top", "right", "bottom", "left"],
                     "horizontal": ["left", "right"], "vertical": ["top", "bottom"]}.get(
                         w[0], [w[0]] if w[0] in self.gaps else [])
            for s in sides:
                self.gaps[s] = n
                self.gaps_ref[s] = r

        # client colors (sway/commands/client.c: indicator falls back to the
        # class default, child_border to background)
        self.colors, self.colors_ref = {}, {}
        for cls in CLIENT_CLASSES:
            args, r = sc.get(f"client.{cls}")
            if cls == "focused_tab_title" and args is None:
                continue  # unset: sway uses focused_inactive/focused logic
            w = args.split()
            default = SWAY_DEFAULTS.get(f"client.{cls}", SWAY_DEFAULTS["client.focused"]).split()
            vals = {"border": w[0], "background": w[1], "text": w[2],
                    "indicator": w[3] if len(w) > 3 else default[3],
                    "child_border": w[4] if len(w) > 4 else w[1]}
            self.colors[cls] = {k: parse_color(v) or parse_color(default[i])
                                for i, (k, v) in enumerate(vals.items())}
            self.colors_ref[cls] = r

    def _border(self, key):
        v, r = self.sc.get(key)
        w = v.split()
        kind = w[0]
        if kind in ("none", "normal", "pixel"):
            width = int(w[1]) if len(w) > 1 else 2
            return kind, (0 if kind == "none" else width), r
        return "normal", 2, r

    # --- container drawing (sway/tree/container.c + view.c) -----------------
    def draw_titlebar(self, cv, x, y, w, cls, title):
        """container_arrange_title_bar(): border frame, background, text node."""
        c = self.colors[cls]
        h, t = self.titlebar_h, self.tb_border
        cv.rect(x, y, w, h, c["border"])
        cv.rect(x + t, y + t, w - 2 * t, h - 2 * t, c["background"])
        tw, _, baseline = cv.measure(title, self.font, self.markup)
        if self.title_align == "right":
            hp = w - self.tb_hpad - tw
        elif self.title_align == "center":
            hp = (w - tw) // 2
        else:
            hp = self.tb_hpad
        hp = max(hp, self.tb_hpad)
        alloc = max(0, min(tw, w - hp - self.tb_hpad))
        ty = (h - self.font_height) // 2       # text node height = font_height
        cv.rect(x + hp, y + ty, alloc, self.font_height, c["background"])
        cv.clip(x + hp, y + ty, alloc, self.font_height)
        cv.text(x + hp, y + ty + (self.font_baseline - baseline), title, self.font,
                c["text"], self.markup)
        cv.unclip()

    def draw_window(self, cv, x, y, w, h, cls, title, indicator=None, border=None,
                    titlebar=True, content=None):
        """A tiled view: titlebar (normal) or top border (pixel), child borders,
        indicator on the right/bottom edge. Returns the content rect."""
        c = self.colors[cls]
        kind = border or self.border
        t = 0 if kind == "none" else self.border_w
        right = c["indicator"] if indicator == "right" else c["child_border"]
        bottom = c["indicator"] if indicator == "bottom" else c["child_border"]
        top = y
        if kind == "normal" and titlebar:
            self.draw_titlebar(cv, x, y, w, cls, title)
            top = y + self.titlebar_h
        elif kind != "none":
            cv.rect(x, y, w, t, c["child_border"])
            top = y + t
        if t:
            cv.rect(x, top, t, y + h - top - t, c["child_border"])
            cv.rect(x + w - t, top, t, y + h - top - t, right)
            cv.rect(x, y + h - t, w, t, bottom)
        cx, cy, cw, ch = x + t, top, w - 2 * t, y + h - top - t
        cv.rect(cx, cy, cw, ch, content)
        return cx, cy, cw, ch


# ===========================================================================
# sheet theme: waybar's stylesheet (sway's own font wins when set)
# ===========================================================================

def waybar_id(module):
    """CSS id waybar gives a module: sway/workspaces -> #workspaces."""
    name = module.split("#", 1)[0]
    if name.startswith("custom/"):
        return "custom-" + name.split("/", 1)[1]
    return name.rsplit("/", 1)[-1]


class Theme:
    def __init__(self, ctx):
        wb = ctx.waybar
        cfg, css = wb["cfg"], wb["css"]
        self.css, self.cfg = css, cfg
        univ = css.get("*", {})
        families = [f.strip().strip("'\"") for f in
                    univ.get("font-family", "monospace").split(",") if f.strip()]
        self.font_family = ", ".join(families) or "monospace"
        self.font_px = px(univ.get("font-size"), 13)

        bar = css.get("window#waybar", {})
        bar_bg = parse_color(bar.get("background-color", "#2B303B")) or (0.17, 0.19, 0.23, 1)
        self.bar_bg = bar_bg
        self.base = bar_bg[:3] + (1.0,)
        self.fg = over(parse_color(bar.get("color", "#ffffff")) or (1, 1, 1, 1), self.base)
        self.dim = mix(self.fg, self.base, 0.45)
        self.border_w, self.border_col = 0, self.base
        m = re.match(r"([\d.]+)px\s+\w+\s+(.+)", bar.get("border-bottom", ""))
        if m:
            self.border_w = float(m.group(1))
            c = parse_color(m.group(2))
            if c:
                self.border_col = over(c, self.base)

        ws = css.get("#workspaces button", {})
        self.chip_pad = box4(ws.get("padding", "0 5px"))[1]
        self.chip_fg = over(parse_color(ws.get("color", "#ffffff")) or (1, 1, 1, 1), self.base)
        focused = css.get("#workspaces button.focused", css.get("#workspaces button.active", {}))
        self.chip_bg = over(parse_color(focused.get("background-color", "#64727D"))
                            or (0.39, 0.45, 0.49, 1), self.base)
        self.underline_w, self.underline_col = 0, self.fg
        m = re.match(r"inset\s+0\s+(-?[\d.]+)px\s+(.+)", focused.get("box-shadow", ""))
        if m:
            self.underline_w = abs(float(m.group(1)))
            c = parse_color(m.group(2))
            if c:
                self.underline_col = over(c, self.base)
        self.urgent_bg = over(parse_color(css.get("#workspaces button.urgent", {})
                                          .get("background-color", "#eb4d4b")) or (0.9, 0.3, 0.3, 1),
                              self.base)

        lang = css.get("#language", {})
        self.lang_bg = over(parse_color(lang.get("background", lang.get("background-color", "#00b093")))
                            or (0, 0.69, 0.58, 1), self.base)
        self.lang_fg = over(parse_color(lang.get("color", "#740864")) or (0.45, 0.03, 0.39, 1),
                            self.lang_bg)
        self.lang_pad = box4(lang.get("padding", "0 5px"))[1]
        self.lang_margin = box4(lang.get("margin", "0 5px"))[1]

        ws_margin = box4(css.get("#workspaces", {}).get("margin", "0"))[3]
        left = cfg.get("modules-left", [])
        if left and left[0] == "sway/workspaces":
            first = css.get(".modules-left > widget:first-child > #workspaces", {})
            if "margin-left" in first:
                ws_margin = px(first["margin-left"])

        self.bar_height = float(cfg.get("height", 0) or 0)
        self.bar_position = cfg.get("position", "top")
        self.spacing = float(cfg.get("spacing", 4))
        self.pad = self.chip_pad + ws_margin   # padding of the "1" from the screen edge

        # sway's own font directive wins (it styles the WM directly)
        f, r = ctx.sway.get("font", "")
        m = re.match(r"(?:pango:)?(.+?)\s+(\d+(?:\.\d+)?)(px)?$", f or "")
        if m and r != DEFAULT:
            self.font_family = m.group(1)
            self.font_px = float(m.group(2)) * (1 if m.group(3) else 96 / 72)

        self.font = font_desc(self.font_family, self.font_px)
        self.bold = font_desc(self.font_family, self.font_px)
        self.bold.set_weight(Pango.Weight.BOLD)
        self.small = font_desc(self.font_family, self.font_px * 0.85)
        self.row_h = self.bar_height or round(self.font_px * 2.3)   # chip rows = bar height
        self.line_h = round(self.font_px * 1.55)                   # table rows
        self.key_h = round(self.font_px * 1.9)                     # binding chip rows

    def bar_footprint(self):
        if self.bar_height:
            return self.bar_height  # waybar draws its border inside the height
        return self.font_px + 4 + self.border_w


# ===========================================================================
# blocks: measured + drawn pieces the layout arranges
# ===========================================================================

class Block:
    def __init__(self, name, measure, draw, place="flow", min_w=0):
        self.name, self.measure, self.draw = name, measure, draw
        self.place, self.min_w = place, min_w


def header_row(cv, T, x, y, w, title, note=None):
    cv.text(x, y, title, T.bold, T.fg, valign="middle", box_h=T.row_h, max_w=w)
    if note:
        nw, _, _ = cv.measure(title, T.bold)
        cv.text(x + w, y, note, T.small, T.dim, align="right", valign="middle",
                box_h=T.row_h, max_w=max(10, w - nw - T.spacing * 2))
    cv.rect(x, y + T.row_h - T.border_w, w, T.border_w, T.border_col)
    return T.row_h + T.spacing


def kv_block(ctx, name, title, rows, note=None):
    """A titled table: key · value (+ color swatch) · source reference."""
    T = ctx.theme

    def widths(cv):
        kw = max((cv.measure(r[0], T.font)[0] for r in rows), default=0)
        vw = max((cv.measure(str(r[1]), T.font)[0] + (T.line_h if len(r) > 3 and r[3] else 0)
                  for r in rows), default=0)
        rw = max((cv.measure(r[2] or "", T.small)[0] for r in rows), default=0)
        return kw, vw, rw

    def measure(cv, w):
        return T.row_h + T.spacing + len(rows) * T.line_h + T.spacing

    def draw(cv, x, y, w):
        y += header_row(cv, T, x, y, w, title, note)
        kw, vw, rw = widths(cv)
        kw = min(kw, w * 0.55)          # long keys (paths) are ellipsized
        vx = x + kw + 2 * T.spacing
        for r in rows:
            key, val, rf = r[0], str(r[1]), r[2]
            sw = r[3] if len(r) > 3 else None
            cv.text(x, y, key, T.font, T.dim, valign="middle", box_h=T.line_h, max_w=kw)
            tx = vx
            if sw:
                s = T.line_h - 6
                cv.rect(vx, y + 3, s, s, T.border_col)
                cv.rect(vx + 1, y + 4, s - 2, s - 2, over(sw, T.base))
                tx += T.line_h
            cv.text(tx, y, val, T.font, T.fg, valign="middle", box_h=T.line_h,
                    max_w=max(10, x + w - tx - (rw + T.spacing)))
            if rf:
                cv.text(x + w, y, rf, T.small, T.dim, align="right", valign="middle",
                        box_h=T.line_h)
            y += T.line_h

    def min_w(cv):
        kw, vw, rw = widths(cv)
        return kw + vw + rw + 4 * T.spacing

    b = Block(name, measure, draw)
    b.min_w_fn = min_w
    return b


# ===========================================================================
# key bindings (labels, glosses, grouping)
# ===========================================================================

KEY_LABELS = {
    "Mod4": "Super", "Mod1": "Alt", "Mod2": "Num", "Mod3": "Mod3", "Mod5": "AltGr",
    "Control": "Ctrl", "Ctrl": "Ctrl", "Shift": "Shift", "Return": "Enter", "space": "Space",
    "minus": "-", "plus": "+", "equal": "=", "Escape": "Esc", "Print": "PrtSc",
    "Pause": "Pause", "BackSpace": "Backspace", "Tab": "Tab", "Delete": "Del",
    "Left": "←", "Right": "→", "Up": "↑", "Down": "↓",
    "XF86AudioRaiseVolume": "Vol+", "XF86AudioLowerVolume": "Vol-",
    "XF86AudioMute": "Mute", "XF86AudioMicMute": "MicMute",
    "XF86AudioPlay": "Play", "XF86AudioPause": "Pause", "XF86AudioStop": "Stop",
    "XF86AudioNext": "Next", "XF86AudioPrev": "Prev",
    "XF86AudioForward": "Fwd", "XF86AudioRewind": "Rew",
    "XF86MonBrightnessUp": "Bright+", "XF86MonBrightnessDown": "Bright-",
}


def key_label(combo):
    return "+".join(KEY_LABELS.get(k, k) for k in combo.split("+"))


def derive_gloss(cmd):
    """Gloss from the command itself; None when the config comment is better."""
    c = cmd.strip()
    m = re.match(r"^workspace number (\d+)$", c)
    if m:
        return f"Go to workspace {m.group(1)}"
    m = re.match(r"^move container to workspace number (\d+)$", c)
    if m:
        return f"Move window to workspace {m.group(1)}"
    m = re.match(r"^focus (left|down|up|right|parent|mode_toggle)$", c)
    if m:
        return {"left": "Focus window left", "down": "Focus window down",
                "up": "Focus window up", "right": "Focus window right",
                "parent": "Focus parent container",
                "mode_toggle": "Switch focus tiling ⇄ floating"}[m.group(1)]
    m = re.match(r"^move (left|down|up|right)$", c)
    if m:
        return f"Move window {m.group(1)}"
    simple = {"kill": "Kill focused window",
              "reload": "Reload configuration (also regenerates this wallpaper)",
              "splith": "Split horizontally", "splitv": "Split vertically",
              "fullscreen": "Toggle fullscreen",
              "floating toggle": "Toggle floating for this window",
              "move scratchpad": "Send window to scratchpad",
              "scratchpad show": "Show / cycle scratchpad windows"}
    if c in simple:
        return simple[c]
    m = re.match(r"^layout (.+)$", c)
    if m:
        return f"Layout: {m.group(1)}"
    m = re.match(r'^resize (shrink|grow) (width|height) (\d+)px$', c)
    if m:
        return f"{m.group(1).capitalize()} {m.group(2)} {m.group(3)}px"
    m = re.match(r'^mode "?([^"]+)"?$', c)
    if m:
        if m.group(1) == "default":
            return "Leave this mode"
        if m.group(1) == "passthrough":
            return "Enter passthrough mode (keys go to the app)"
        return f"Enter {m.group(1)} mode"
    if "set-sink-mute" in c:
        return "Toggle speaker mute"
    if "set-source-mute" in c:
        return "Toggle microphone mute"
    if "playerctl" in c:
        act = c.split("playerctl", 1)[1].strip()
        return {"play-pause": "Media: play / pause", "pause": "Media: pause",
                "stop": "Media: stop", "next": "Media: next track",
                "previous": "Media: previous track",
                "position +10": "Media: forward 10s",
                "position -10": "Media: rewind 10s"}.get(act, f"Media: {act}")
    if "brightnessctl" in c:
        return "Brightness +5%" if "+${" in c or "set +" in c else "Brightness −5%"
    if "volume-helper" in c or "volume_helper" in c:
        return "Volume up" if "--increase" in c else "Volume down"
    m = re.match(r"^exec grimshot save (\w+)", c)
    if m:
        return {"output": "Screenshot: whole screen",
                "active": "Screenshot: focused window",
                "area": "Screenshot: select area"}.get(m.group(1), f"Screenshot: {m.group(1)}")
    if c.startswith("exec swaynag") and "exit" in c:
        return "Exit sway (asks first)"
    m = re.match(r"^exec (\S+)", c)
    if m:
        prog = m.group(1).split("/")[-1]
        if prog == "foot":
            return "Open terminal (foot)"
        if prog == "rofi":
            return "Application launcher (rofi)"
    return None


def build_sections(sc):
    """Merge equal commands, compress workspace rows, order sections."""
    merged = {}
    for b in sc.bindings:
        gloss = derive_gloss(b.command) or b.gloss or b.command
        gloss = re.sub(r"\s+", " ", gloss).strip()
        if b.locked:
            gloss += " (also when locked)"
        key = (b.section, b.mode, b.command)
        if key in merged:
            if key_label(b.keys) not in merged[key]["keys"]:
                merged[key]["keys"].append(key_label(b.keys))
        else:
            merged[key] = {"section": b.section or "Other", "mode": b.mode,
                           "keys": [key_label(b.keys)], "gloss": gloss,
                           "command": b.command, "order": b.order}
    entries = list(merged.values())

    for verb, gloss in (("workspace number", "Go to workspace 1–10 (0 = 10)"),
                        ("move container to workspace number", "Move window to workspace 1–10")):
        group = [e for e in entries if e["command"].startswith(verb + " ") and e["mode"] is None]
        nums = {}
        for e in group:
            try:
                nums[int(e["command"].rsplit(" ", 1)[1])] = e
            except ValueError:
                pass
        if set(nums) == set(range(1, 11)):
            first = min(group, key=lambda e: e["order"])
            modkeys = {k.rsplit("+", 1)[0] for e in group for k in e["keys"]}
            if len(modkeys) == 1:
                mod = modkeys.pop()
                for e in group:
                    entries.remove(e)
                entries.append({"section": first["section"], "mode": None,
                                "keys": [f"{mod}+1…0"], "gloss": gloss,
                                "command": verb, "order": first["order"]})

    fm, _ = sc.get("floating_modifier")
    if fm and fm != "none":
        w = fm.split()
        drag, resize = ("left", "right") if (len(w) < 2 or w[1] == "normal") else ("right", "left")
        entries.append({"section": "Basics", "mode": None,
                        "keys": [f"{key_label(w[0])}+{drag.capitalize()}-drag"],
                        "gloss": f"Drag floating windows ({resize} button: resize)",
                        "command": "floating_modifier", "order": -1})

    sections, index = [], {}
    for e in sorted(entries, key=lambda e: e["order"]):
        sec = e["section"]
        if sec not in index:
            index[sec] = len(sections)
            sections.append({"name": sec, "entries": []})
        sections[index[sec]]["entries"].append(e)
    for sec in sections:
        if sec["name"].endswith(" mode"):
            mode = sec["name"][:-5]
            sec["entries"].sort(key=lambda e: 0 if e["command"] == f'mode "{mode}"' else 1)
    return [s for s in sections if s["entries"]]


# ===========================================================================
# compose sequences (xkbcommon's lookup order)
# ===========================================================================

KEYSYM_LABELS = {
    "quotedbl": '"', "apostrophe": "'", "grave": "`", "acute": "´",
    "diaeresis": "¨", "macron": "¯", "cedilla": "¸", "comma": ",",
    "period": ".", "slash": "/", "minus": "-", "plus": "+", "equal": "=",
    "space": "Space", "underscore": "_", "colon": ":", "semicolon": ";",
}

COMPOSE_KEYS = {"compose:ralt": "Right Alt", "compose:lalt": "Left Alt",
                "compose:caps": "Caps Lock", "compose:rwin": "Right Super",
                "compose:lwin": "Left Super", "compose:menu": "Menu",
                "compose:prsc": "PrtSc", "compose:sclk": "Scroll Lock",
                "compose:rctrl": "Right Ctrl", "compose:lctrl": "Left Ctrl",
                "compose:102": "<> key", "compose:paus": "Pause"}


def system_compose_file():
    loc = os.environ.get("LC_ALL") or os.environ.get("LC_CTYPE") or os.environ.get("LANG") or "C"
    lang = loc.split(".")[0]
    for cand in (f"{lang}.UTF-8", "en_US.UTF-8"):
        p = Path(f"/usr/share/X11/locale/{cand}/Compose")
        if p.is_file():
            return p
    return None


class ComposeInfo:
    """Compose key + the shortest sequence per German letter, from the same
    files libxkbcommon reads: $XCOMPOSEFILE, $XDG_CONFIG_HOME/XCompose,
    ~/.XCompose, else the locale's system file."""

    def __init__(self, ctx):
        files = []
        user = first_existing(os.environ.get("XCOMPOSEFILE"), XDG_CONFIG / "XCompose",
                              HOME / ".XCompose")
        system = system_compose_file()
        if user:
            files.append(user)
            if re.search(r'^\s*include\s+"%L"', user.read_text(errors="replace"), re.M) and system:
                files.insert(0, system)
        elif system:
            files.append(system)
        self.files = files
        seqs = {c: None for c in GERMAN_CHARS}
        for f in files:
            ctx.use(f, "compose")
            for line in f.read_text(errors="replace").splitlines():
                m = re.match(r'^((?:<[^>]+>\s*)+):\s*"(.+?)"', line)
                if not m or m.group(2) not in seqs:
                    continue
                keys = re.findall(r"<([^>]+)>", m.group(1))
                if keys[0] != "Multi_key" or any(k.startswith("dead_") for k in keys):
                    continue
                labels = []
                for k in keys[1:]:
                    if k in KEYSYM_LABELS:
                        labels.append(KEYSYM_LABELS[k])
                    elif len(k) == 1:
                        labels.append(k)
                    else:
                        labels = None
                        break
                if labels is None:
                    continue
                rank = (len(keys), any(ord(l) > 127 for l in labels))
                cur = seqs[m.group(2)]
                if cur is None or rank < cur[0]:
                    seqs[m.group(2)] = (rank, "Compose + " + " + ".join(labels))
        self.seqs = {c: (v[1] if v else "—") for c, v in seqs.items()}

        opts = ctx.keyboard.get("xkb_options") or ""
        if not opts:
            for args, _ in ctx.sway.all("input"):
                m = re.search(r"xkb_options\s+(\S+)", args)
                if m:
                    opts = m.group(1)
        self.key_label, self.option = None, opts
        for o in opts.split(","):
            if o.strip() in COMPOSE_KEYS:
                self.key_label, self.option = COMPOSE_KEYS[o.strip()], o.strip()


# ===========================================================================
# ELEMENTS: the closed set of things the wallpaper can explain
# ===========================================================================

ELEMENTS = {}


def element(name, source, doc, place="flow"):
    def deco(fn):
        ELEMENTS[name] = {"fn": fn, "source": source, "doc": doc, "place": place}
        return fn
    return deco


# --- frame ----------------------------------------------------------------

@element("header", "sway IPC (outputs, inputs) + config", "Title line and live session facts", "top")
def el_header(ctx, out):
    T = ctx.theme
    sc = ctx.sway
    mod = key_label(sc.variables.get("mod", "Mod4"))
    layout = ctx.keyboard.get("xkb_active_layout_name") or "?"
    mode = out.get("current_mode") or {}
    facts = (f"mod: {mod} · layout: {layout} · {out['name']} "
             f"{mode.get('width', out['rect']['width'])}×{mode.get('height', out['rect']['height'])}"
             f" @ {out.get('scale', 1)}x · sway {ctx.ipc.version.get('human_readable', '?')}"
             f" · generated {datetime.now():%Y-%m-%d %H:%M}")

    def measure(cv, w):
        return T.row_h + T.line_h + T.spacing * 2

    def draw(cv, x, y, w):
        cv.text(x, y, "sway_wp — this session, explained by its own configuration", T.bold,
                T.fg, valign="middle", box_h=T.row_h)
        cv.text(x, y + T.row_h, facts + f" · zoom {round(cv.zoom * 100)}%", T.font, T.dim,
                valign="middle", box_h=T.line_h, max_w=w)
    return [Block("header", measure, draw, "top")]


@element("sources", "all files read for this image", "Every file that fed a value, and what it fed", "flow")
def el_sources(ctx, out):
    # filled in after all other elements have run (see build_blocks)
    T = ctx.theme
    rows = []
    for path, what in sorted(ctx.sources.items()):
        try:
            mt = datetime.fromtimestamp(Path(path).stat().st_mtime).strftime("%m-%d %H:%M")
        except OSError:
            mt = ""
        rows.append((path, ", ".join(sorted(what)), mt))
    rows.append(("sway IPC", "outputs, inputs, seats, tree", LIVE))
    return [kv_block(ctx, "sources", "Sources", rows, "file · feeds · modified")]


# --- key bindings -----------------------------------------------------------

@element("bindings", "sway config tree: bindsym/bindcode + comments",
         "Every key binding, grouped by config file / comment section")
def el_bindings(ctx, out):
    T = ctx.theme
    blocks = []
    for sec in build_sections(ctx.sway):
        rows = [(" / ".join(e["keys"]), e["gloss"]) for e in sec["entries"]]

        def measure(cv, w, rows=rows):
            return T.row_h + T.spacing + len(rows) * (T.key_h + T.spacing / 2)

        def draw(cv, x, y, w, rows=rows, name=sec["name"]):
            y += header_row(cv, T, x, y, w, name)
            for keys, gloss in rows:
                kw = T.chip_pad * 2 + cv.measure(keys, T.font)[0]
                cv.rect(x, y, kw, T.key_h, T.chip_bg)
                if T.underline_w:
                    cv.rect(x, y + T.key_h - T.underline_w, kw, T.underline_w, T.underline_col)
                cv.text(x + T.chip_pad, y, keys, T.font, T.chip_fg, valign="middle", box_h=T.key_h)
                cv.text(x + kw + T.spacing, y, gloss, T.font, T.fg, valign="middle",
                        box_h=T.key_h, max_w=w - kw - T.spacing)
                y += T.key_h + T.spacing / 2

        def min_w(cv, rows=rows):
            return max(T.chip_pad * 2 + cv.measure(k, T.font)[0] + T.spacing
                       + cv.measure(g, T.font)[0] for k, g in rows)
        b = Block(f"bindings:{sec['name']}", measure, draw)
        b.min_w_fn = min_w
        blocks.append(b)
    return blocks


@element("compose", "XCompose files (libxkbcommon order) + live xkb_options",
         "How to type German letters on the US layout", "bottom")
def el_compose(ctx, out):
    T = ctx.theme
    ci = ctx.compose
    cap = (f"German on US layout — Compose = {ci.key_label or 'not configured'}"
           f"{' (' + ci.option + ')' if ci.key_label else ''}, press Compose then the sequence"
           " · or Ctrl+Shift+U, hex code, Enter")
    src = " + ".join(ref(f) for f in ci.files)

    def measure(cv, w):
        return T.lang_pad * 2 + T.line_h + T.spacing * 3 + T.row_h * 3

    def draw(cv, x, y, w):
        sx, sw = x + T.lang_margin, w - 2 * T.lang_margin
        h = measure(cv, w)
        cv.rect(sx, y, sw, h, T.lang_bg)
        tx, ty = sx + T.lang_pad, y + T.lang_pad
        cv.text(tx, ty, cap, T.bold, T.lang_fg, valign="middle", box_h=T.line_h)
        cv.text(sx + sw - T.lang_pad, ty, src, T.small, T.lang_fg, align="right",
                valign="middle", box_h=T.line_h)
        ty += T.line_h + T.spacing
        cell = (sw - 2 * T.lang_pad) / len(GERMAN_CHARS)
        for row in range(3):
            for i, ch in enumerate(GERMAN_CHARS):
                txt, f = ((ch, T.bold), (ci.seqs[ch], T.font),
                          (f"Ctrl+Shift+U {ord(ch):04X}", T.font))[row]
                cv.text(tx + i * cell + cell / 2, ty, txt, f, T.lang_fg, align="center",
                        valign="middle", box_h=T.row_h)
            ty += T.row_h + T.spacing
    return [Block("compose", measure, draw, "bottom")]


# --- sway: decorations, drawn the way sway draws them -----------------------

@element("sway.windows", "sway: client.* · font · titlebar_* · title_align · default_border",
         "Each client.* colour class rendered as a real sway window")
def el_sway_windows(ctx, out):
    T, S = ctx.theme, ctx.sway_style
    classes = [c for c in CLIENT_CLASSES if c in S.colors]
    content_h = T.line_h * 2 + 6

    def win_h():
        top = S.titlebar_h if S.border == "normal" else S.border_w
        return top + content_h + S.border_w

    def measure(cv, w):
        return T.row_h + T.spacing + len(classes) * (win_h() + T.spacing)

    def draw(cv, x, y, w):
        y += header_row(cv, T, x, y, w, "Window decorations",
                        f"font {S.font_str} · {S.font_ref}")
        for cls in classes:
            c = S.colors[cls]
            cx, cy, cw, ch = S.draw_window(cv, x, y, w, win_h(), cls,
                                           f"client.{cls}", indicator="right",
                                           content=T.base)
            parts = "  ".join(f"{p} {hexs(c[p])}" for p in CLIENT_PARTS)
            cv.text(cx + 4, cy + 3, parts, T.small, T.fg, max_w=cw - 8)
            cv.text(cx + 4, cy + 3 + T.line_h, f"{S.colors_ref[cls]}  ·  right edge = indicator",
                    T.small, T.dim, max_w=cw - 8)
            y += win_h() + T.spacing
    b = Block("sway.windows", measure, draw)
    b.min_w_fn = lambda cv: 380
    return [b]


@element("sway.layout", "sway: gaps · default_border · hide_edge_borders · client.*",
         "A miniature workspace: gaps, split + tabbed containers, focus states")
def el_sway_layout(ctx, out):
    T, S = ctx.theme, ctx.sway_style

    def measure(cv, w):
        return T.row_h + T.spacing + round(w * 0.5) + T.line_h + T.spacing

    def draw(cv, x, y, w):
        g = S.gaps
        y += header_row(cv, T, x, y, w, "Workspace layout",
                        f"gaps inner {g['inner']} outer {g['top']}/{g['right']}/{g['bottom']}/{g['left']}")
        H = round(w * 0.5)
        cv.rect(x, y, w, H, mix(T.base, (0, 0, 0, 1), 0.25))   # the workspace area
        # sway/tree/workspace.c workspace_add_gaps(): edge gap = outer + inner
        gl, gr = g["left"] + g["inner"], g["right"] + g["inner"]
        gt, gb = g["top"] + g["inner"], g["bottom"] + g["inner"]
        wx, wy, ww, wh = x + gl, y + gt, w - gl - gr, H - gt - gb
        inner = g["inner"]
        half = (ww - inner) / 2
        # left: tabbed container holding focus -> focused tab + unfocused tabs
        tabs = ["foot", "qutebrowser", "xplr"]
        tw = half / len(tabs)
        for i, t in enumerate(tabs):
            S.draw_titlebar(cv, wx + i * tw, wy, tw if i < len(tabs) - 1 else half - i * tw,
                            "focused" if i == 0 else "unfocused", t)
        S.draw_window(cv, wx, wy + S.titlebar_h, half, wh - S.titlebar_h, "focused", "",
                      titlebar=False, border="pixel" if S.border != "none" else "none",
                      content=T.base)
        # right: vertical split without focus -> focused_inactive + unfocused
        rx = wx + half + inner
        hh = (wh - inner) / 2
        S.draw_window(cv, rx, wy, half, hh, "focused_inactive", "kak (focused_inactive)",
                      content=T.base)
        S.draw_window(cv, rx, wy + hh + inner, half, wh - hh - inner, "unfocused",
                      "fastfetch (unfocused)", content=T.base)
        cv.text(x, y + H, f"tabbed (focused) │ splitv · border {S.border} {S.border_w}px · "
                f"hide_edge_borders {S.hide_edge} · smart_gaps {S.smart_gaps}",
                T.small, T.dim, valign="middle", box_h=T.line_h, max_w=w)
    b = Block("sway.layout", measure, draw)
    b.min_w_fn = lambda cv: 380
    return [b]


@element("sway.font", "sway: font (Pango metrics like common/pango.c)",
         "The titlebar font, its metrics, and the titlebar height they produce")
def el_sway_font(ctx, out):
    S = ctx.sway_style
    live = None
    try:
        def walk(n):
            if n.get("type") == "con" and n.get("border") == "normal" and n.get("deco_rect", {}).get("height"):
                return n["deco_rect"]["height"]
            for c in n.get("nodes", []) + n.get("floating_nodes", []):
                r = walk(c)
                if r:
                    return r
        live = walk(ctx.ipc.tree())
    except Exception:
        pass
    rows = [("font", S.font_str + (" (pango markup)" if S.markup else ""), S.font_ref),
            ("font_height", f"{S.font_height}px (ascent + descent)", "pango"),
            ("font_baseline", f"{S.font_baseline}px", "pango"),
            ("titlebar height", f"{S.titlebar_h}px = {S.font_height} + 2×{S.tb_vpad}", "computed")]
    if live:
        rows.append(("live titlebar", f"{live}px", LIVE))
    return [kv_block(ctx, "sway.font", "Font", rows)]


@element("sway.decoration", "sway: default_border · titlebar_* · title_align · hide_edge_borders · smart_*",
         "Border and titlebar settings")
def el_sway_decoration(ctx, out):
    S, sc = ctx.sway_style, ctx.sway
    rows = [("default_border", f"{S.border} {S.border_w}px", S.border_ref),
            ("default_floating_border", f"{S.fborder} {S.fborder_w}px", S.fborder_ref),
            ("titlebar_padding", f"h {S.tb_hpad}px · v {S.tb_vpad}px", S.pad_ref),
            ("titlebar_border_thickness", f"{S.tb_border}px", S.tbt_ref),
            ("title_align", S.title_align, S.align_ref),
            ("hide_edge_borders", S.hide_edge, S.hide_edge_ref),
            ("smart_borders", S.smart_borders, S.smart_borders_ref),
            ("show_marks", *sc.get("show_marks"))]
    for args, r in sc.all("for_window"):
        if re.search(r"\b(border|opacity)\b", args):
            rows.append(("for_window", args, r))
    return [kv_block(ctx, "sway.decoration", "Borders & titlebars", rows)]


@element("sway.colors", "sway: client.* (all five parts per class)",
         "Every client colour as a swatch with its hex value")
def el_sway_colors(ctx, out):
    S = ctx.sway_style
    rows = []
    for cls in CLIENT_CLASSES:
        if cls not in S.colors:
            rows.append((f"{cls}", "unset → focused_inactive / focused", DEFAULT))
            continue
        for p in CLIENT_PARTS:
            rows.append((f"{cls}.{p}", hexs(S.colors[cls][p]), S.colors_ref[cls],
                         S.colors[cls][p]))
    return [kv_block(ctx, "sway.colors", "Client colours", rows)]


@element("sway.gaps", "sway: gaps · smart_gaps", "Inner and outer gaps")
def el_sway_gaps(ctx, out):
    S = ctx.sway_style
    g, r = S.gaps, S.gaps_ref
    rows = [("inner", f"{g['inner']}px", r["inner"])]
    rows += [(f"outer {s}", f"{g[s]}px (edge total {g[s] + g['inner']}px)", r[s])
             for s in ("top", "right", "bottom", "left")]
    rows.append(("smart_gaps", S.smart_gaps, S.smart_gaps_ref))
    return [kv_block(ctx, "sway.gaps", "Gaps", rows)]


@element("sway.behaviour", "sway: focus_* · mouse_warping · workspace_* · popup_* · tiling_drag · floating_*",
         "On-screen behaviour: focus, warping, dragging, floating limits")
def el_sway_behaviour(ctx, out):
    sc = ctx.sway
    keys = ["focus_follows_mouse", "mouse_warping", "focus_wrapping",
            "focus_on_window_activation", "workspace_auto_back_and_forth",
            "popup_during_fullscreen", "tiling_drag", "tiling_drag_threshold",
            "workspace_layout", "default_orientation", "floating_modifier",
            "floating_minimum_size", "floating_maximum_size", "xwayland", "primary_selection"]
    rows = [(k, *sc.get(k)) for k in keys]
    return [kv_block(ctx, "sway.behaviour", "Behaviour", rows)]


@element("sway.output", "sway IPC: outputs + config: output ... bg",
         "Every active output: mode, scale, transform, subpixel, background")
def el_sway_output(ctx, out):
    sc = ctx.sway
    rows = []
    for o in ctx.outputs:
        m = o.get("current_mode") or {}
        rows.append((o["name"], f"{o.get('make', '')} {o.get('model', '')}".strip(), LIVE))
        rows.append(("  mode", f"{m.get('width')}×{m.get('height')} @ "
                     f"{(m.get('refresh') or 0) / 1000:.2f} Hz", LIVE))
        rows.append(("  scale · transform", f"{o.get('scale')} · {o.get('transform')}", LIVE))
        rows.append(("  subpixel · adaptive sync", f"{o.get('subpixel_hinting')} · "
                     f"{o.get('adaptive_sync_status')}", LIVE))
    for args, r in sc.all("output"):
        rows.append(("output", args, r))
    return [kv_block(ctx, "sway.output", "Outputs", rows)]


@element("sway.input", "sway IPC: inputs + config: input ...",
         "Keyboard layout, xkb options, repeat; pointer acceleration and scrolling")
def el_sway_input(ctx, out):
    rows, seen = [], set()
    for i in ctx.inputs:
        t = i.get("type")
        if t == "keyboard" and i.get("xkb_layout_names"):
            sig = ("kb", tuple(i["xkb_layout_names"]), i.get("repeat_rate"), i.get("repeat_delay"))
            if sig in seen:
                continue
            seen.add(sig)
            rows.append(("keyboard", f"{i.get('xkb_active_layout_name')} "
                         f"(of {', '.join(i['xkb_layout_names'])})", LIVE))
            rows.append(("  repeat", f"{i.get('repeat_rate')}/s after {i.get('repeat_delay')}ms", LIVE))
        elif t in ("pointer", "touchpad") and i.get("libinput", {}).get("accel_profile"):
            li = i["libinput"]
            sig = ("ptr", t, li.get("accel_profile"), li.get("accel_speed"), li.get("natural_scroll"),
                   li.get("tap"))
            if sig in seen:
                continue
            seen.add(sig)
            rows.append((t, f"accel {li.get('accel_profile')} {li.get('accel_speed')} · "
                         f"natural scroll {li.get('natural_scroll')}"
                         + (f" · tap {li['tap']}" if "tap" in li else ""), LIVE))
    for args, r in ctx.sway.all("input"):
        rows.append(("input", args, r))
    return [kv_block(ctx, "sway.input", "Input", rows)]


@element("sway.seat", "sway: seat ... (xcursor_theme, hide_cursor) + XCURSOR_* env",
         "Cursor theme and size, cursor hiding")
def el_sway_seat(ctx, out):
    rows = []
    cfg = ctx.sway.all("seat")
    theme = next(((a, r) for a, r in cfg if " xcursor_theme " in f" {a} "), None)
    if theme:
        rows.append(("xcursor_theme", theme[0].split("xcursor_theme", 1)[1].strip(), theme[1]))
    else:
        rows.append(("xcursor_theme", f"{os.environ.get('XCURSOR_THEME', 'default')} "
                     f"{os.environ.get('XCURSOR_SIZE', '24')}",
                     "env" if os.environ.get("XCURSOR_THEME") else DEFAULT))
    for a, r in cfg:
        if "xcursor_theme" not in a:
            rows.append(("seat", a, r))
    try:
        for s in ctx.ipc.seats():
            rows.append((s["name"], f"{len(s.get('devices', []))} devices", LIVE))
    except Exception:
        pass
    return [kv_block(ctx, "sway.seat", "Seat & cursor", rows)]


# --- bar --------------------------------------------------------------------

@element("bar", "waybar: config.jsonc (modules, height, spacing) + style.css (#id rules)",
         "The bar's modules as waybar styles them, plus its geometry")
def el_bar(ctx, out):
    T = ctx.theme
    wb = ctx.waybar
    cfg, css = wb["cfg"], wb["css"]
    sc = ctx.sway
    cmd, cmd_ref = sc.get("bar.swaybar_command", "swaybar")
    if "waybar" not in (cmd or ""):
        return el_swaybar(ctx, out, cmd, cmd_ref)
    groups = [(g, cfg.get(f"modules-{g}", [])) for g in ("left", "center", "right")]

    def style(mod):
        sel = "#" + waybar_id(mod)
        rule = dict(css.get(sel, {}))
        bg = parse_color(rule.get("background-color", rule.get("background", "transparent")))
        fg = parse_color(rule.get("color", "")) or T.fg
        pad = box4(rule.get("padding", "0"))
        if "padding-right" in rule:
            pad[1] = px(rule["padding-right"])
        mar = box4(rule.get("margin", "0"))
        return sel, (over(bg, T.base) if bg and bg[3] > 0 else None), over(fg, T.base), pad, mar

    def chips(cv, w):
        """Lay the modules out like the bar does, wrapping into extra rows
        when the column is narrower than the screen. -> [(dx, row, chip)]"""
        label_w = cv.measure("center", T.small)[0] + T.spacing * 2
        placed, row = [], 0
        for g, mods in groups:
            placed.append((0, row, ("group", g)))
            cx = label_w
            for mod in mods:
                if mod == "sway/workspaces":
                    items = [("ws", n, st, T.chip_pad * 2 + cv.measure(n, T.font)[0], 0, 0)
                             for n, st in (("1", "focused"), ("2", ""), ("3", "urgent"))]
                else:
                    sel, bg, fg, pad, mar = style(mod)
                    label = waybar_id(mod)
                    items = [("mod", label, (bg, fg, pad),
                              cv.measure(label, T.font)[0] + pad[1] + pad[3], mar[3], mar[1])]
                total = sum(i[3] + i[4] + i[5] for i in items)
                if cx + total > w and cx > label_w:
                    row, cx = row + 1, label_w
                for it in items:
                    cx += it[4]
                    placed.append((cx, row, it))
                    cx += it[3] + it[5]
                cx += T.spacing
            row += 1
        return placed, row

    def measure(cv, w):
        _, rows = chips(cv, w)
        return T.row_h + T.spacing + rows * (T.row_h + T.spacing) + T.line_h * 2 + T.spacing

    def draw(cv, x, y, w):
        y += header_row(cv, T, x, y, w, "Bar (waybar)",
                        f"{ref(wb['cfg_path'] or 'config')} + {ref(wb['css_path'] or 'style.css')}")
        placed, rows = chips(cv, w)
        for dx, row, it in placed:
            cx, cy = x + dx, y + row * (T.row_h + T.spacing)
            if it[0] == "group":
                cv.text(cx, cy, it[1], T.small, T.dim, valign="middle", box_h=T.row_h)
            elif it[0] == "ws":
                _, n, st, bw, _, _ = it
                cv.rect(cx, cy, bw, T.row_h, {"focused": T.chip_bg, "urgent": T.urgent_bg}.get(st))
                if st == "focused" and T.underline_w:
                    cv.rect(cx, cy + T.row_h - T.underline_w, bw, T.underline_w, T.underline_col)
                cv.text(cx + T.chip_pad, cy, n, T.font, T.chip_fg, valign="middle", box_h=T.row_h)
            else:
                _, label, (bg, fg, pad), lw, _, _ = it
                cv.rect(cx, cy, lw, T.row_h, bg)
                cv.text(cx + pad[3], cy, label, T.font, fg, valign="middle", box_h=T.row_h)
        y += rows * (T.row_h + T.spacing)
        geo = (f"position {T.bar_position} · height {T.bar_height or 'auto'}px · spacing "
               f"{T.spacing}px · font {T.font_family} {T.font_px:g}px")
        bar_rule = css.get("window#waybar", {})
        look = (f"background {bar_rule.get('background-color', '?')} · border-bottom "
                f"{bar_rule.get('border-bottom', 'none')} · text {bar_rule.get('color', '?')}")
        cv.text(x, y, geo, T.small, T.dim, valign="middle", box_h=T.line_h, max_w=w)
        cv.text(x, y + T.line_h, look, T.small, T.dim, valign="middle", box_h=T.line_h, max_w=w)
    b = Block("bar", measure, draw)
    b.min_w_fn = lambda cv: 420
    return [b]


def el_swaybar(ctx, out, cmd, cmd_ref):
    """swaybar in use: show its live colours from IPC (bar config)."""
    rows = [("swaybar_command", cmd, cmd_ref)]
    try:
        for bid in ctx.ipc.bar_ids():
            bc = ctx.ipc.bar_config(bid)
            rows.append((bid, f"{bc.get('position')} · {bc.get('mode')} · font {bc.get('font')}", LIVE))
            for k, v in (bc.get("colors") or {}).items():
                rows.append((f"  {k}", v, LIVE, parse_color(v)))
    except Exception as e:
        rows.append(("error", str(e), LIVE))
    return [kv_block(ctx, "bar", "Bar (swaybar)", rows)]


# --- notifications: dunst ---------------------------------------------------

def dunst_paths():
    paths = [Path("/etc/xdg/dunst/dunstrc")]
    paths += sorted(Path("/etc/xdg/dunst/dunstrc.d").glob("*.conf"))
    user = XDG_CONFIG / "dunst/dunstrc"
    if user.is_file():
        paths = [user] + sorted((XDG_CONFIG / "dunst/dunstrc.d").glob("*.conf"))
    return paths


@element("notifications", "dunst: dunstrc [global] + [urgency_*]",
         "Low / normal / critical notifications as dunst draws them")
def el_notifications(ctx, out):
    if not first_existing("/usr/bin/dunst"):
        ctx.notes.append("notifications: dunst not installed")
        return []
    T = ctx.theme
    paths = [p for p in dunst_paths() if p.is_file()]
    for p in paths:
        ctx.use(p, "notifications")
    ini = read_ini(paths)
    g = {k: v[0] for k, v in ini.get("global", {}).items()}
    width = int(re.findall(r"\d+", g.get("width", "300"))[-1])
    pad, hpad = int(g.get("padding", 8)), int(g.get("horizontal_padding", 8))
    frame_w = int(g.get("frame_width", 3))
    frame_col = parse_color(g.get("frame_color", "#aaaaaa"))
    sep_h = int(g.get("separator_height", 2))
    sep = g.get("separator_color", "frame")
    gap = int(g.get("gap_size", 0))
    radius = int(g.get("corner_radius", 0))
    font = font_desc(g.get("font", "Monospace 8"))
    fmt = g.get("format", "<b>%s</b>\\n%b").replace("\\n", "\n")
    urg = []
    for u in ("low", "normal", "critical"):
        s = {k: v[0] for k, v in ini.get(f"urgency_{u}", {}).items()}
        urg.append((u, parse_color(s.get("background", "#222222")),
                    parse_color(s.get("foreground", "#888888")),
                    parse_color(s.get("frame_color")) or frame_col, s.get("timeout", "10"),
                    s.get("default_icon", "")))

    def body(u, timeout, icon):
        return (fmt.replace("%s", f"{u.capitalize()} urgency")
                .replace("%b", f"timeout {timeout}s · icon {icon or '—'}")
                .replace("%a", "dunst").replace("%p", "").replace("%n", ""))

    def notif_h(cv, u, t, i):
        return cv.measure(body(u, t, i), font, markup=True, max_w=width - 2 * hpad - 2 * frame_w,
                          wrap=True)[1] + 2 * pad

    def measure(cv, w):
        hs = [notif_h(cv, u[0], u[4], u[5]) for u in urg]
        return T.row_h + T.spacing + sum(hs) + 2 * frame_w + (len(hs) - 1) * (gap or sep_h) \
            + T.line_h + T.spacing + (2 * frame_w * (len(hs) - 1) if gap else 0)

    def draw(cv, x, y, w):
        y += header_row(cv, T, x, y, w, "Notifications (dunst)", " + ".join(ref(p) for p in paths))
        nx = x + max(0, (w - width) / 2)
        nw = min(width, w)
        hs = [notif_h(cv, u[0], u[4], u[5]) for u in urg]
        if gap:   # separate windows
            for (u, bg, fg, fc, t, i), h in zip(urg, hs):
                cv.rounded(nx, y, nw, h + 2 * frame_w, radius, fc)
                cv.rounded(nx + frame_w, y + frame_w, nw - 2 * frame_w, h, max(0, radius - frame_w), bg)
                cv.text(nx + frame_w + hpad, y + frame_w + pad, body(u, t, i), font, fg, markup=True,
                        max_w=nw - 2 * hpad - 2 * frame_w, wrap=True)
                y += h + 2 * frame_w + gap
        else:     # one window, separators between (dunst gap_size = 0)
            total = sum(hs) + (len(hs) - 1) * sep_h + 2 * frame_w
            cv.rounded(nx, y, nw, total, radius, urg[1][3])
            yy = y + frame_w
            for k, ((u, bg, fg, fc, t, i), h) in enumerate(zip(urg, hs)):
                cv.rect(nx + frame_w, yy, nw - 2 * frame_w, h, bg)
                cv.text(nx + frame_w + hpad, yy + pad, body(u, t, i), font, fg, markup=True,
                        max_w=nw - 2 * hpad - 2 * frame_w, wrap=True)
                yy += h
                if k < len(hs) - 1:
                    sc_ = {"frame": urg[1][3], "foreground": fg, "auto": fg}.get(sep) \
                        or parse_color(sep) or fg
                    cv.rect(nx + frame_w, yy, nw - 2 * frame_w, sep_h, sc_)
                    yy += sep_h
            y += total
        cv.text(x, y + T.spacing, f"width {width} · padding {pad}/{hpad} · frame {frame_w}px "
                f"{hexs(frame_col)} · font {g.get('font')} · {g.get('origin', 'top-right')} "
                f"offset {g.get('offset', '')}", T.small, T.dim, max_w=w)
    b = Block("notifications", measure, draw)
    b.min_w_fn = lambda cv: min(width, 420)
    return [b]


# --- launcher: rofi ---------------------------------------------------------

@element("launcher", "rofi: -dump-theme (resolved theme) + -dump-config (font)",
         "The launcher's list rows in their theme colours")
def el_launcher(ctx, out):
    if not first_existing("/usr/bin/rofi"):
        ctx.notes.append("launcher: rofi not installed")
        return []
    T = ctx.theme
    try:
        theme_txt = subprocess.run(["rofi", "-dump-theme"], capture_output=True, text=True,
                                   timeout=10).stdout
        conf_txt = subprocess.run(["rofi", "-dump-config"], capture_output=True, text=True,
                                  timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired) as e:
        ctx.notes.append(f"launcher: {e}")
        return []
    user_cfg = first_existing(XDG_CONFIG / "rofi/config.rasi")
    ctx.use(user_cfg or "rofi -dump-theme", "launcher")
    rules = parse_css(theme_txt)
    vars_ = rules.get("*", {})

    def val(sel, prop, default=None):
        v = rules.get(sel, {}).get(prop, default)
        for _ in range(5):
            m = re.fullmatch(r"\s*var\(\s*([\w-]+)\s*\)\s*", v or "")
            if not m:
                break
            v = vars_.get(m.group(1), default)
        return v

    m = re.search(r'^\s*font:\s*"([^"]+)"', conf_txt, re.M)
    font_str = m.group(1) if m else "mono 12"
    m2 = re.search(r'^\s*/\*\s*font:\s*"([^"]+)"', conf_txt, re.M)
    if not m and m2:
        font_str = m2.group(1)
    font = font_desc(font_str)
    items = [("normal.normal", "foot"), ("alternate.normal", "qutebrowser"),
             ("selected.normal", "xplr  (selected)"), ("normal.active", "pavucontrol  (active)"),
             ("normal.urgent", "kakoune  (urgent)")]
    win_bg = parse_color(val("window", "background-color", "#ffffff")) or (1, 1, 1, 1)
    border_c = parse_color(val("window", "border-color", vars_.get("border-color"))) or \
        parse_color(vars_.get("foreground", "#000")) or (0, 0, 0, 1)
    if "var(" in (vars_.get("border-color") or ""):
        border_c = parse_color(val("*", "border-color")) or border_c
    win_pad = px(val("window", "padding", "5"), 5)
    win_border = px(val("window", "border", "1"), 1)
    el_pad = px(val("element", "padding", "1px"), 1)
    lv_spacing = px(val("listview", "spacing", "2px"), 2)
    fg = parse_color(val("*", "normal-foreground")) or (0, 0, 0, 1)
    sepc = parse_color(val("*", "separatorcolor")) or fg
    fh = cv_h = None

    def measure(cv, w):
        nonlocal fh
        fh = cv.measure("Ag", font)[1]
        rows_h = len(items) * (fh + 2 * el_pad) + (len(items) - 1) * lv_spacing
        return T.row_h + T.spacing + 2 * (win_pad + win_border) + fh + 2 + 4 + rows_h + T.spacing

    def draw(cv, x, y, w):
        y += header_row(cv, T, x, y, w, "Launcher (rofi)", f"font {font_str} · -dump-theme")
        h = measure(cv, w) - T.row_h - 2 * T.spacing
        cv.rect(x, y, w, h, border_c)
        cv.rect(x + win_border, y + win_border, w - 2 * win_border, h - 2 * win_border, win_bg)
        ix, iy, iw = x + win_border + win_pad, y + win_border + win_pad, w - 2 * (win_border + win_pad)
        cv.text(ix, iy, "combi:", font, fg)
        pw = cv.measure("combi: ", font)[0]
        cv.text(ix + pw, iy, "Type to filter", font, parse_color("Gray"))
        iy += fh + 2
        cv.rect(ix, iy, iw, 2, sepc)   # listview border: 2px dash 0 0
        iy += 4
        for sel, label in items:
            bg = parse_color(val(f"element {sel}", "background-color")) or win_bg
            tc = parse_color(val(f"element {sel}", "text-color")) or fg
            cv.rect(ix, iy, iw, fh + 2 * el_pad, over(bg, win_bg))
            cv.text(ix + el_pad, iy + el_pad, label, font, tc)
            iy += fh + 2 * el_pad + lv_spacing
    b = Block("launcher", measure, draw)
    b.min_w_fn = lambda cv: 300
    return [b]


# --- lock screen: swaylock --------------------------------------------------

SWAYLOCK_DEFAULTS = {   # swaylock 1.8 main.c set_default_colors()
    "inside": "000000c0", "inside-clear": "e5a445c0", "inside-caps-lock": "000000c0",
    "inside-ver": "0072ffc0", "inside-wrong": "fa0000c0",
    "line": "000000ff", "line-clear": "000000ff", "line-caps-lock": "000000ff",
    "line-ver": "000000ff", "line-wrong": "000000ff",
    "ring": "337d00ff", "ring-clear": "e5a445ff", "ring-caps-lock": "e5a445ff",
    "ring-ver": "3300ffff", "ring-wrong": "7d3300ff",
    "text": "e5a445ff", "text-clear": "000000ff", "text-caps-lock": "e5a445ff",
    "text-ver": "000000ff", "text-wrong": "000000ff",
    "key-hl": "33db00ff", "bs-hl": "db3300ff", "separator": "000000ff",
    "color": "a3a3a3ff", "indicator-radius": "50", "indicator-thickness": "10",
    "font": "sans-serif",
}


@element("lockscreen", "swaylock: /etc/swaylock/config + ~/.config/swaylock/config",
         "The unlock indicator in each of its states")
def el_lockscreen(ctx, out):
    if not first_existing("/usr/bin/swaylock"):
        ctx.notes.append("lockscreen: swaylock not installed")
        return []
    T = ctx.theme
    paths = [Path("/etc/swaylock/config"), XDG_CONFIG / "swaylock/config"]
    user = first_existing(XDG_CONFIG / "swaylock/config", HOME / ".swaylock/config")
    paths = [user] if user else [Path("/etc/swaylock/config")]
    conf = {}
    for p in paths:
        if p and p.is_file():
            ctx.use(p, "lockscreen")
            for i, line in enumerate(p.read_text(errors="replace").splitlines(), 1):
                line = line.strip()
                if line and not line.startswith("#"):
                    k, _, v = line.partition("=")
                    conf[k.strip()] = (v.strip(), ref(p, i))

    def get(k):
        return conf[k][0] if k in conf else SWAYLOCK_DEFAULTS.get(k)
    radius = float(get("indicator-radius"))
    thick = float(get("indicator-thickness"))
    font = font_desc(get("font"), radius / 3)   # swaylock: font size = arc_radius / 3
    states = [("", "", "typing"), ("-clear", "Cleared", "cleared"),
              ("-ver", "Verifying", "verifying"), ("-wrong", "Wrong", "wrong"),
              ("-caps-lock", "Caps Lock", "caps lock")]
    d = 2 * (radius + thick)

    def per_row(w):
        return max(1, int((w + T.spacing) // (d + T.spacing)))

    def measure(cv, w):
        rows = math.ceil(len(states) / per_row(w))
        return T.row_h + T.spacing + rows * (d + T.line_h + T.spacing) + T.line_h * 2

    def draw(cv, x, y, w):
        y += header_row(cv, T, x, y, w, "Lock screen (swaylock)",
                        " + ".join(ref(p) for p in paths if p and p.is_file()) or DEFAULT)
        n = per_row(w)
        cr = cv.cr
        for i, (suf, txt, label) in enumerate(states):
            cx = x + (i % n) * (d + T.spacing) + d / 2
            cy = y + (i // n) * (d + T.line_h + T.spacing) + d / 2
            # swaylock render.c: inside, ring arc, highlight segment, two border lines
            cr.arc(cx, cy, radius - thick / 2, 0, 2 * math.pi)
            cr.set_source_rgba(*parse_color(get("inside" + suf)))
            cr.fill()
            cr.set_line_width(thick)
            cr.arc(cx, cy, radius, 0, 2 * math.pi)
            cr.set_source_rgba(*parse_color(get("ring" + suf)))
            cr.stroke()
            if suf in ("", "-caps-lock"):
                hl = parse_color(get("caps-lock-key-hl" if suf else "key-hl") or get("key-hl"))
                cr.arc(cx, cy, radius, -math.pi / 2, -math.pi / 2 + math.pi / 3)
                cr.set_source_rgba(*hl)
                cr.stroke()
            cr.set_line_width(2.0)
            cr.set_source_rgba(*parse_color(get("line" + suf)))
            cr.arc(cx, cy, radius - thick / 2, 0, 2 * math.pi)
            cr.stroke()
            cr.arc(cx, cy, radius + thick / 2, 0, 2 * math.pi)
            cr.stroke()
            if txt:
                cv.text(cx, cy - radius / 3 * 0.7, txt, font,
                        parse_color(get("text" + suf)), align="center")
            cv.text(cx, cy + d / 2, label, T.small, T.dim, align="center")
        rows = math.ceil(len(states) / n)
        y += rows * (d + T.line_h + T.spacing)
        bg = conf.get("image", (None,))[0]
        cv.text(x, y, f"radius {radius:g} · thickness {thick:g} · font {get('font')} · "
                f"{'image ' + Path(bg).name + ' (' + get('scaling') + ')' if bg else 'color ' + get('color')}",
                T.small, T.dim, max_w=w)
    b = Block("lockscreen", measure, draw)
    b.min_w_fn = lambda cv: d * 3 + T.spacing * 2
    return [b]


# --- nag bar: swaynag -------------------------------------------------------

SWAYNAG_TYPES = {   # sway/swaynag/types.c
    "defaults": {"font": "pango:Monospace 10", "button_background": "333333",
                 "details_background": "333333", "background": "323232", "text": "ffffff",
                 "button_text": "ffffff", "border": "222222", "border_bottom": "444444",
                 "bar_border_thickness": "2", "message_padding": "8",
                 "details_border_thickness": "3", "button_border_thickness": "3",
                 "button_gap": "20", "button_gap_close": "15", "button_margin_right": "2",
                 "button_padding": "3"},
    "error": {"button_background": "680A0A", "details_background": "680A0A",
              "background": "900000", "text": "FFFFFF", "button_text": "FFFFFF",
              "border": "D92424", "border_bottom": "470909"},
    "warning": {"button_background": "FFC100", "details_background": "FFC100",
                "background": "FFA800", "text": "000000", "button_text": "000000",
                "border": "AB7100", "border_bottom": "AB7100"},
}


@element("nagbar", "swaynag: types.c defaults + ~/.config/swaynag/config + the exit binding's flags",
         "The confirmation bar (e.g. exit sway) as swaynag draws it")
def el_nagbar(ctx, out):
    T = ctx.theme
    user = first_existing(XDG_CONFIG / "swaynag/config")
    ini = read_ini([user]) if user else {}
    if user:
        ctx.use(user, "nagbar")
    msg, buttons, ntype, bref = "swaynag", [], "error", DEFAULT
    for b in ctx.sway.bindings:
        if b.command.startswith("exec swaynag"):
            try:
                argv = shlex.split(b.command[5:])
            except ValueError:
                continue
            for i, a in enumerate(argv):
                if a in ("-t", "--type") and i + 1 < len(argv):
                    ntype = argv[i + 1]
                elif a in ("-m", "--message") and i + 1 < len(argv):
                    msg = argv[i + 1]
                elif a in ("-B", "-b", "-Z", "-z", "--button") and i + 1 < len(argv):
                    buttons.append(argv[i + 1])
            bref = b.ref
            break

    def props(t):
        p = dict(SWAYNAG_TYPES["defaults"])
        p.update(SWAYNAG_TYPES.get(t, {}))
        p.update({k: v[0] for k, v in ini.get("", {}).items()})
        p.update({k: v[0] for k, v in ini.get(t, {}).items()})
        return p

    shown = [(ntype, msg, buttons or ["Yes"])]
    if ntype != "error":
        shown.append(("error", "swaynag error type (config errors are reported like this)", []))

    def bar_h(cv, p):
        f = font_desc(p["font"].replace("pango:", ""))
        th = cv.measure("Ag", f)[1]
        mp, bp, bbt = int(p["message_padding"]), int(p["button_padding"]), int(p["button_border_thickness"])
        # swaynag render.c: height = max(message, buttons) + bar border
        return max(th + mp * 2, th + 2 * (bp + bbt) + mp) + int(p["bar_border_thickness"]), f, th

    def measure(cv, w):
        return T.row_h + T.spacing + sum(bar_h(cv, props(t))[0] + T.spacing for t, _, _ in shown) \
            + T.line_h

    def draw(cv, x, y, w):
        y += header_row(cv, T, x, y, w, "Nag bar (swaynag)", f"exit binding {bref}")
        for t, m, btns in shown:
            p = props(t)
            h, f, th = bar_h(cv, p)
            col = {k: parse_color(p[k]) for k in ("background", "text", "button_background",
                                                    "button_text", "border", "border_bottom")}
            bbt, bp = int(p["bar_border_thickness"]), int(p["button_padding"])
            bt, mp = int(p["button_border_thickness"]), int(p["message_padding"])
            cv.rect(x, y, w, h, col["background"])
            cv.rect(x, y + h - bbt, w, bbt, col["border_bottom"])
            # buttons right to left: close "X", then the -B buttons
            bx = x + w - int(p["button_margin_right"])
            for k, label in enumerate(["X"] + list(reversed(btns))):
                lw = cv.measure(label, f)[0] + 2 * (bp + bt)
                bx -= lw
                by = y + (h - bbt - (th + 2 * (bp + bt))) / 2
                cv.rect(bx, by, lw, th + 2 * (bp + bt), col["border"])
                cv.rect(bx + bt, by + bt, lw - 2 * bt, th + 2 * bp, col["button_background"])
                cv.text(bx + bt + bp, by + bt + bp, label, f, col["button_text"])
                bx -= int(p["button_gap_close"]) if k == 0 else int(p["button_gap"])
            cv.text(x + mp, y + (h - bbt - th) / 2, m, f, col["text"], max_w=bx - x - mp)
            y += h + T.spacing
        cv.text(x, y, f"font {props(ntype)['font']} · type {ntype}"
                f"{' · ' + ref(user) if user else ' · built-in types'}", T.small, T.dim, max_w=w)
    b = Block("nagbar", measure, draw)
    b.min_w_fn = lambda cv: 380
    return [b]


# --- terminal: foot ---------------------------------------------------------

@element("terminal", "foot: foot.ini [main] font/pad + [colors(-dark)] (explicit or documented default)",
         "The terminal's font, padding and 16-colour palette")
def el_terminal(ctx, out):
    if not first_existing("/usr/bin/foot"):
        ctx.notes.append("terminal: foot not installed")
        return []
    T = ctx.theme
    user = first_existing(XDG_CONFIG / "foot/foot.ini")
    system = first_existing(Path("/etc/xdg/foot/foot.ini"))
    paths = [p for p in (system, user) if p]
    for p in paths:
        ctx.use(p, "terminal")
    ini = read_ini(paths, comment_defaults=True)
    main = ini.get("main", {})
    colors = {}
    for sec in ("colors", "colors-dark"):
        colors.update(ini.get(sec, {}))
    font_spec, font_ref = main.get("font", ("monospace:size=8", DEFAULT))
    pad_spec, pad_ref = main.get("pad", ("0x0", DEFAULT))
    fam = font_spec.split(",")[0].split(":")[0]
    m = re.search(r"size=([\d.]+)", font_spec)
    size_pt = float(m.group(1)) if m else 8
    m = re.search(r"pixelsize=([\d.]+)", font_spec)
    size_px = float(m.group(1)) if m else size_pt * 96 / 72
    font = font_desc(fam, size_px)
    m = re.match(r"(\d+)x(\d+)", pad_spec)
    padx, pady = (int(m.group(1)), int(m.group(2))) if m else (0, 0)
    bg = parse_color(colors.get("background", ("242424",))[0]) or (0.14, 0.14, 0.14, 1)
    fg = parse_color(colors.get("foreground", ("ffffff",))[0]) or (1, 1, 1, 1)
    alpha = float(colors.get("alpha", ("1.0",))[0])
    pal = [(n, parse_color(colors.get(n, ("000000",))[0]) or (0, 0, 0, 1),
            colors.get(n, ("", ""))[1]) for n in [f"regular{i}" for i in range(8)] +
           [f"bright{i}" for i in range(8)]]

    def measure(cv, w):
        lh = cv.measure("Ag", font)[1]
        return T.row_h + T.spacing + 2 * pady + lh * 4 + 8 + T.line_h

    def draw(cv, x, y, w):
        y += header_row(cv, T, x, y, w, "Terminal (foot)", f"font {font_spec} · {font_ref}")
        lh = cv.measure("Ag", font)[1]
        h = 2 * pady + lh * 4 + 4
        cv.rect(x, y, w, h, over((*bg[:3], alpha), T.base))
        tx, ty = x + padx + 2, y + pady + 2
        cv.text(tx, ty, "~ $ printf palette", font, fg)
        ty += lh
        cell = (w - 2 * padx - 4) / 8
        for row in range(2):
            for i in range(8):
                n, c, _ = pal[row * 8 + i]
                cv.rect(tx + i * cell, ty, cell - 2, lh * 1.4 - 2, c)
                lum = 0.299 * c[0] + 0.587 * c[1] + 0.114 * c[2]
                cv.text(tx + i * cell + 2, ty, hexs(c)[1:], font, (0, 0, 0, 1) if lum > 0.55 else
                        (1, 1, 1, 1), max_w=cell - 4)
            ty += lh * 1.4
        cv.text(x, y + h + 2, f"bg {hexs(bg)} · fg {hexs(fg)} · alpha {alpha:g} · pad {pad_spec} "
                f"({pad_ref}) · palette: {pal[0][2] or DEFAULT}", T.small, T.dim, max_w=w)
    b = Block("terminal", measure, draw)
    b.min_w_fn = lambda cv: 380
    return [b]


# ===========================================================================
# elements config
# ===========================================================================

DEFAULT_ELEMENTS = ["header", "bindings", "sway.windows", "sway.layout", "sway.font",
                    "sway.decoration", "sway.gaps", "sway.behaviour", "bar",
                    "notifications", "launcher", "lockscreen", "nagbar", "terminal",
                    "sway.output", "sway.input", "sway.seat", "sources", "compose"]


def read_elements(path_override=None):
    path = Path(path_override) if path_override else first_existing(*ELEMENTS_PATHS)
    if not path:
        return DEFAULT_ELEMENTS, None
    names = []
    for i, line in enumerate(path.read_text().splitlines(), 1):
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        if line not in ELEMENTS:
            log(f"{ref(path, i)}: unknown element '{line}' (see sway_wp.py --list)")
            continue
        if line not in names:
            names.append(line)
    return names, path


# ===========================================================================
# layout: top blocks, newspaper columns, bottom blocks; zoom to fit
# ===========================================================================

def content_box(T, cv):
    pad = T.pad
    bar = T.bar_footprint()
    x, y, w, h = pad, pad, cv.lw - 2 * pad, cv.lh - 2 * pad
    pos = T.bar_position
    if pos == "top":
        y, h = bar / cv.zoom + pad, cv.lh - bar / cv.zoom - 2 * pad
    elif pos == "bottom":
        h = cv.lh - bar / cv.zoom - 2 * pad
    elif pos == "left":
        x, w = bar / cv.zoom + pad, cv.lw - bar / cv.zoom - 2 * pad
    elif pos == "right":
        w = cv.lw - bar / cv.zoom - 2 * pad
    return x, y, w, h


MIN_COL_W, MAX_COL_W = 260, 400


def plan(cv, T, blocks):
    """Return (placements, overflow names). The bar stays at its real size;
    everything else is in zoomed logical px."""
    x, y, w, h = content_box(T, cv)
    gap = T.spacing * 2
    placements = []
    top = [b for b in blocks if b.place == "top"]
    bottom = [b for b in blocks if b.place == "bottom"]
    flow = [b for b in blocks if b.place == "flow"]
    for b in top:
        bh = b.measure(cv, w)
        placements.append((b, x, y, w))
        y += bh + gap
        h -= bh + gap
    by = y + h
    for b in reversed(bottom):
        bh = b.measure(cv, w)
        by -= bh
        placements.append((b, x, by, w))
        by -= gap
    avail = by - y
    # column width: what the widest block needs, capped so that one long table
    # (values are ellipsized) cannot force the whole sheet into few columns
    min_w = max([min(getattr(b, "min_w_fn", lambda cv: b.min_w)(cv), MAX_COL_W)
                 for b in flow] + [MIN_COL_W])
    ncol = max(1, int((w + gap) // (min_w + gap)))
    col_w = (w - (ncol - 1) * gap) / ncol
    col, cy, overflow = 0, y, []
    for b in flow:
        bh = b.measure(cv, col_w)
        if cy > y and cy + bh > y + avail:
            col, cy = col + 1, y
        if col >= ncol or bh > avail:
            overflow.append(b.name)
            continue
        placements.append((b, x + col * (col_w + gap), cy, col_w))
        cy += bh + gap
    return placements, overflow


def render(ctx, out, names):
    T = ctx.theme
    scale = out.get("scale", 1.0) or 1.0
    mode = out.get("current_mode") or {}
    W = round((mode.get("width") or out["rect"]["width"]))
    H = round((mode.get("height") or out["rect"]["height"]))
    if out.get("transform") in ("90", "270", "flipped-90", "flipped-270"):
        W, H = H, W
    cv = Canvas(W, H, scale, out.get("subpixel_hinting", "unknown"))

    blocks = []
    deferred = None
    for n in names:
        if n == "sources":
            deferred = len(blocks)
            continue
        try:
            blocks += ELEMENTS[n]["fn"](ctx, out)
        except Exception as e:
            ctx.notes.append(f"{n}: {type(e).__name__}: {e}")
            log(f"element {n} failed:\n{traceback.format_exc()}")
    if deferred is not None:
        blocks[deferred:deferred] = el_sources(ctx, out)

    zoom = 1.0
    while True:
        cv.set_zoom(zoom)
        placements, overflow = plan(cv, T, blocks)
        if not overflow or zoom <= 0.5:
            break
        zoom = round(zoom - 0.05, 2)

    cv.cr.save()
    cv.cr.identity_matrix()
    cv.rect(0, 0, W, H, T.base)
    cv.cr.restore()
    for b, x, y, w in placements:
        try:
            b.draw(cv, x, y, w)
        except Exception as e:
            ctx.notes.append(f"{b.name}: draw failed: {e}")
            log(f"draw {b.name} failed:\n{traceback.format_exc()}")
    if overflow:
        ctx.notes.append("did not fit: " + ", ".join(overflow))
    return cv, zoom


# ===========================================================================
# main
# ===========================================================================

def list_elements():
    names, path = read_elements()
    print(f"elements config: {path or 'none found, using built-in default list'}")
    print(f"search order: {', '.join(str(p) for p in ELEMENTS_PATHS)}\n")
    for n, e in ELEMENTS.items():
        mark = "on " if n in names else "off"
        print(f"[{mark}] {n:<16} {e['doc']}\n{'':22}from: {e['source']}  ({e['place']})")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--list", action="store_true", help="list all elements and their sources")
    ap.add_argument("--dry-run", action="store_true",
                    help="render to ~/.cache/sway_wp/preview-*.png, do not set the wallpaper")
    ap.add_argument("--elements", metavar="FILE", help="use this elements config instead")
    args = ap.parse_args()

    if args.list:
        list_elements()
        return

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    names, epath = read_elements(args.elements)
    ipc = SwayIPC()
    ctx = Ctx(ipc)
    if epath:
        ctx.use(epath, "element list")

    bg_mode = "fill"
    first = True
    for out in ctx.outputs:
        cv, zoom = render(ctx, out, names)
        prefix = "preview" if args.dry_run else "wallpaper"
        path = CACHE_DIR / f"{prefix}-{out['name']}.png"
        cv.surface.write_to_png(str(path))
        if first and not args.dry_run:
            cv.surface.write_to_png(str(CACHE_DIR / "wallpaper.png"))
        first = False
        applied = "dry run"
        if not args.dry_run:
            res = ipc.command(f"output {out['name']} bg {path} {bg_mode}")
            applied = all(r.get("success") for r in res) if isinstance(res, list) else bool(res)
        print(f"{out['name']}: {cv.W}x{cv.H} zoom {zoom:.0%} -> {path} (applied: {applied})")

    print(f"elements: {', '.join(names)} (from {epath or 'built-in list'})")
    for n in ctx.notes:
        print(f"note: {n}")
    if ctx.notes:
        with open(LOG_FILE, "a") as f:
            f.write(f"\n=== {datetime.now().isoformat()} notes ===\n" + "\n".join(ctx.notes) + "\n")

    # Keep the bar in sync: SIGUSR2 is waybar's documented reload signal, so
    # one sway reload applies style.css to bar AND wallpaper.
    if not args.dry_run:
        subprocess.run(["pkill", "-USR2", "-x", "waybar"], check=False)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a") as f:
            f.write(f"\n=== {datetime.now().isoformat()} ===\n")
            f.write(traceback.format_exc())
        traceback.print_exc()
        sys.exit(1)
