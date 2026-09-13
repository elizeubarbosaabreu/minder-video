#!/usr/bin/env python3
"""Converte mapas mentais (.minder, .mm ou texto indentado) em vídeos animados.

Com interface gráfica (tkinter):
    python3 app.py

Pela linha de comando:
    python3 app.py entrada.minder -o video.mp4
    python3 app.py entrada.txt --tema dark --tempo-por-no 0.6 --pausa-final 3

Origem dos dados:
    .minder   pacote do Minder (zip com map.xml) — usa o tema embutido
    .mm       mapa FreeMind (XML)
    .txt/.md  texto com indentação definindo os níveis (primeira linha = raiz)

O vídeo anima a construção do mapa: o nó central aparece primeiro e cada
ramo entra em sequência com suavização, terminando com uma pausa no mapa
completo. É possível escolher a duração por nó, a pausa final, o fade, a
resolução, os quadros por segundo (fps) e o tema.
"""

import argparse
import math
import os
import sys
import threading
import zipfile
import xml.etree.ElementTree as ET

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
except ImportError:  # ambientes sem interface
    tk = None

try:
    import numpy as np
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    np = Image = ImageDraw = ImageFont = None

try:
    from moviepy import VideoClip
except ImportError:
    VideoClip = None


def deps_error(exc_name=""):
    return (
        "Faltam dependências para gerar o vídeo%s.\n"
        "Rode no ambiente virtual do projeto:\n"
        "\n"
        "    source .venv/bin/activate      (Windows: .venv\\Scripts\\activate)\n"
        "    pip install -r requirements.txt\n"
        "\n"
        "Depois execute novamente: python app.py (ou 'minder-video')."
    ) % (" " + exc_name if exc_name else "")


# ---------------------------------------------------------------------------
# Fontes e cores
# ---------------------------------------------------------------------------

FONT_REGULAR = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FONT_EMOJI = "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf"

FONT_REGULAR_FALLBACKS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "C:/Windows/Fonts/arial.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
]
FONT_BOLD_FALLBACKS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "/System/Library/Fonts/Helvetica-Bold.ttf",
]

THEMES = {
    "default": "Padrão (claro)",
    "dark": "Escuro",
    "solarized-dark": "Solarized Escuro",
    "solarized-light": "Solarized Claro",
}

THEME_COLORS = {
    "default": {
        "name": "default",
        "background": "#F8F9FA",
        "foreground": "#374151",
        "root_background": "#e0e7ff",
        "root_foreground": "#3730a3",
        "connection_background": "#9aa4b5",
    },
    "dark": {
        "name": "dark",
        "background": "#333333",
        "foreground": "White",
        "root_background": "#d4d4d4",
        "root_foreground": "Black",
        "connection_background": "#7e8087",
    },
    "solarized-dark": {
        "name": "solarized_dark",
        "background": "#002B36",
        "foreground": "#93A1A1",
        "root_background": "#d4d4d4",
        "root_foreground": "#000000",
        "connection_background": "#7e8087",
    },
    "solarized-light": {
        "name": "solarized_light",
        "background": "#FDF6E3",
        "foreground": "#586E75",
        "root_background": "#839496",
        "root_foreground": "#FDF6E3",
        "connection_background": "#606060",
    },
}

NAMED = {
    "black": (0, 0, 0),
    "white": (255, 255, 255),
    "grey": (128, 128, 128),
    "gray": (128, 128, 128),
}

RESOLUTIONS = {
    "640×360": (640, 360),
    "854×480": (854, 480),
    "1280×720 (HD)": (1280, 720),
    "1920×1080 (Full HD)": (1920, 1080),
}

FPS_CHOICES = (24, 30, 60)

LAYOUT = {
    "row_h": 44,
    "node_h": 30,
    "node_h_root": 46,
    "gap": 32,
}

# ---------------------------------------------------------------------------
# Parsers: texto indentado, .mm (FreeMind) e .minder (zip com map.xml)
# ---------------------------------------------------------------------------


def parse_lines(lines):
    """Converte linhas indentadas em raízes de árvore ({id, text, children})."""
    entries = []
    for raw in lines:
        raw = raw.rstrip("\r\n")
        if not raw.strip():
            continue
        stripped = raw.lstrip()
        entries.append((len(raw) - len(stripped), stripped))
    if not entries:
        return []
    levels = {w: i for i, w in enumerate(sorted({w for w, _ in entries}))}
    roots = []
    stack = [(-1, None)]
    counter = 0
    for width, text in entries:
        level = levels[width]
        while stack[-1][0] >= level:
            stack.pop()
        parent = stack[-1][1]
        counter += 1
        node = {"id": counter, "text": text, "children": []}
        if parent is None:
            roots.append(node)
        else:
            parent["children"].append(node)
        stack.append((level, node))
    return roots


def _read_minder_xml(path):
    """Extrai o map.xml de um .minder: tar.gz, tar, zip, gzip puro ou XML sem compactação."""
    import gzip
    import io
    import tarfile

    raw = open(path, "rb").read()
    data = gzip.decompress(raw) if raw[:2] == b"\x1f\x8b" else raw

    if tarfile.is_tarfile(io.BytesIO(data)):
        with tarfile.open(fileobj=io.BytesIO(data)) as tf:
            name = tf.getnames()[0]
            return tf.extractfile(name).read().decode("utf-8", errors="replace")

    if zipfile.is_zipfile(io.BytesIO(data)):
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = zf.namelist()
            candidate = "map.xml" if "map.xml" in names else next(
                (n for n in names if n.endswith(".xml")), "map.xml"
            )
            return zf.read(candidate).decode("utf-8", errors="replace")

    return data.decode("utf-8", errors="replace")


def parse_mm(path):
    """Lê um mapa FreeMind (.mm). Retorna (roots, None)."""
    tree = ET.parse(path)
    root = tree.getroot()
    roots = []
    counter = 0

    def convert(el, is_root=False):
        nonlocal counter
        counter += 1
        node = {
            "id": counter,
            "text": el.get("TEXT") or "",
            "side": None if is_root else el.get("POSITION"),
            "children": [],
        }
        for child in el.findall("node"):
            node["children"].append(convert(child))
        return node

    for child in root.findall("node"):
        roots.append(convert(child, is_root=True))
    return roots, None


def parse_minder(path):
    """Lê um pacote .minder (zip com map.xml, gzip ou XML puro). Retorna (roots, theme_attr)."""
    xml = _read_minder_xml(path)
    root = ET.fromstring(xml)
    theme_el = root.find("theme")
    theme_attr = dict(theme_el.attrib) if theme_el is not None else None
    roots = []
    counter = 0

    def node_text(el):
        name = el.find("nodename")
        if name is None:
            return ""
        if name.text and name.text.strip():
            return name.text.strip()
        text_el = name.find("text")
        if text_el is None:
            return ""
        data = text_el.get("data")
        return data.strip() if data else ""

    def convert(el, side=None, level=0):
        nonlocal counter
        counter += 1
        this_side = side
        if level == 0 or el.get("side"):
            this_side = el.get("side")
        node = {
            "id": counter,
            "text": node_text(el),
            "side": this_side,
            "children": [],
        }
        nodes = el.find("nodes")
        for child in nodes.findall("node") if nodes is not None else []:
            node["children"].append(convert(child, this_side, level + 1))
        return node

    for child in root.findall("nodes/node"):
        roots.append(convert(child))
    return roots, theme_attr


def load_map(path):
    """Lê qualquer formato suportado e devolve (roots, theme_attr)."""
    lower = path.lower()
    if lower.endswith(".minder"):
        return parse_minder(path)
    if lower.endswith(".mm"):
        return parse_mm(path)
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        root = parse_lines(fh)
    return root, None


def assign_sides(roots):
    """Distribui os lados: filhos da raiz alternam direita/esquerda pelos
    tamanhos das subárvores (quando o arquivo não define lados variados);
    níveis mais profundos herdam o lado do pai."""

    def tree_h(n):  # altura = nº de folhas
        if not n["children"]:
            return 1
        return sum(tree_h(c) for c in n["children"])

    def balance(children):
        kids = sorted(children, key=lambda c: tree_h(c), reverse=True)
        h_l = h_r = 0
        for c in kids:
            if h_l <= h_r:
                c["side"], h_l = "left", h_l + tree_h(c)
            else:
                c["side"], h_r = "right", h_r + tree_h(c)

    def fill(node, level, inherited):
        if node.get("side") is None:
            node["side"] = inherited
        children = node["children"]
        if children:
            sides = {c.get("side") for c in children}
            uniform = len(sides) == 1  # todos iguais (ou todos ausentes)
            if uniform:
                if level == 0:
                    # raiz: distribui os ramos principais entre os dois lados
                    balance(children)
                else:
                    # galhos profundos seguem o lado do pai (coluna única por
                    # lado); evita que um nó "direita" antigo do arquivo
                    # apareça dentro de um galho esquerdo perto do centro
                    for c in children:
                        c["side"] = node.get("side") or c["side"]
        for c in children:
            fill(c, level + 1, node.get("side"))

    for r in roots:
        r.setdefault("side", None)
        fill(r, 0, None)


def theme_of(theme_attr):
    """Converte os atributos do tema do arquivo para o dict de cores de vídeo."""
    if not theme_attr:
        return None
    colors = {}
    for key in ("background", "foreground", "root_background", "root_foreground", "connection_background"):
        if theme_attr.get(key):
            colors[key] = theme_attr[key]
    colors["name"] = theme_attr.get("name", "custom")
    return colors


def to_rgb(color, default):
    if color is None:
        return default
    s = str(color).strip().lower()
    if s.startswith("#"):
        try:
            return tuple(int(s[i : i + 2], 16) for i in (1, 3, 5))
        except ValueError:
            return default
    return NAMED.get(s, default)


def key_by_label(mapping, label, default):
    return next((k for k, v in mapping.items() if v == label), default)


# ---------------------------------------------------------------------------
# Layout (estilo Minder, espaçamento adaptativo à largura dos nós)
# ---------------------------------------------------------------------------

ROW_H = LAYOUT["row_h"]
NODE_H = LAYOUT["node_h"]
NODE_H_ROOT = LAYOUT["node_h_root"]


def load_font(name, size, bold=False):
    paths = FONT_BOLD_FALLBACKS if bold else FONT_REGULAR_FALLBACKS
    for p in paths:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except OSError:
                continue
    return ImageFont.load_default()


def _draw_ref():
    if Image is None or ImageDraw is None:
        raise RuntimeError(deps_error("(Pillow)"))
    img = Image.new("RGB", (8, 8))
    return ImageDraw.Draw(img)


def measure_text(text, size, bold=False):
    """Largura de um texto (considerando emojis) na fonte usada no layout."""
    font = load_font("", size, bold)
    width = 0.0
    for run, is_emoji in _runs(text):
        if is_emoji:
            width += size * 1.25 * len(run)
        else:
            try:
                width += font.getbbox(run)[2]
            except Exception:
                width += len(run) * size * 0.6
    return width


def _runs(text):
    """Separa o texto em trechos comuns e trechos de emoji."""
    runs = []
    cur = []
    cur_emoji = None

    def flush():
        nonlocal cur
        if cur:
            runs.append(("".join(cur), cur_emoji))
            cur = []

    for ch in text:
        cp = ord(ch)
        emoji = cp >= 0x1F000 or 0x2700 <= cp <= 0x27BF or 0xFE0F == cp or 0x2B00 <= cp <= 0x2BFF
        if cur_emoji is None:
            cur_emoji = emoji
        elif cur_emoji != emoji:
            flush()
            cur_emoji = emoji
        cur.append(ch)
    flush()
    if not runs:
        runs.append((text, False))
    return runs


def static_layout(roots, cx):
    """Calcula x/y/w/h de cada nó (espaçamento adaptativo, sem sobreposição)."""
    ref = _draw_ref()
    seq = {"n": 0}

    def size_all(node, level):
        seq["n"] += 1
        node["_n"] = seq["n"]
        is_root = level == 0
        font_size = 20 if is_root else 13.5
        lbl_w = measure_text(node["text"], font_size, bold=is_root) + (46 if is_root else 34)
        node["w"] = max(72, math.ceil(lbl_w))
        node["h"] = NODE_H_ROOT if is_root else NODE_H
        for c in node["children"]:
            size_all(c, level + 1)

    def tree_height(n):
        if not n["children"]:
            return 1
        return sum(tree_height(c) for c in n["children"])

    for r in roots:
        r["x"] = cx
        r["_parent"] = None
        r["level"] = 0
        size_all(r, 0)

    def rec(node, level, top):
        if level == 0:
            node["x"] = cx
        else:
            p = node["_parent"]
            s = -1 if node["side"] == "left" else 1
            node["x"] = p["x"] + s * ((p["w"] + node["w"]) / 2 + LAYOUT["gap"])
        h = tree_height(node) * ROW_H
        node["y"] = top + h / 2
        child_top = node["y"] - (sum(tree_height(c) for c in node["children"]) * ROW_H) / 2
        child_top += (NODE_H_ROOT - NODE_H) / 2 if level == 0 else 0
        for c in node["children"]:
            c["_parent"] = node
            c["level"] = level + 1
            rec(c, level + 1, child_top)
            child_top += tree_height(c) * ROW_H

    for r in roots:
        r["level"] = 0
        rec(r, 0, 0)

    # Segurança: mantém os galhos longe da caixa da raiz (raiz larga + nó
    # profundo estreito poderiam se sobrepor perto do centro).
    margin = 26

    def push_x(node, dx):
        node["x"] += dx
        for c in node["children"]:
            push_x(c, dx)

    for r in roots:
        half_r = r["w"] / 2
        stack = list(r["children"])
        seen = 0
        while seen < len(stack):
            node = stack[seen]; seen += 1
            stack.extend(node["children"])
            sign = 1 if node["x"] >= r["x"] else -1
            need = r["x"] + sign * (half_r + node["w"] / 2 + margin)
            if sign * node["x"] < sign * need:
                push_x(node, need - node["x"])
    return ref


# ---------------------------------------------------------------------------
# Renderizador de frames
# ---------------------------------------------------------------------------


class MapVideoRenderer:
    def __init__(self, roots, colors, width, height, fps, step, fade, pause, presentation=True, borders=False):
        self.roots = roots
        self.colors = colors
        self.width = width
        self.height = height
        self.fps = fps
        self.step = max(fade, step)
        self.fade = fade
        self.pause = pause
        self.presentation = presentation
        self.borders = borders

        self.bg = to_rgb(colors.get("background"), (248, 250, 251))
        self.fg = to_rgb(colors.get("foreground"), (55, 65, 81))
        self.root_bg = to_rgb(colors.get("root_background"), (224, 231, 255))
        self.root_fg = to_rgb(colors.get("root_foreground"), (55, 48, 163))
        self.conn = to_rgb(colors.get("connection_background"), (154, 164, 181))

        nodes = []
        def collect(n):
            nodes.append(n)
            for c in n["children"]:
                collect(c)
        for r in self.roots:
            collect(r)
        self.nodes = nodes
        self.order = [n for r in self.roots for n in self.dfs_order(r)]
        self.order_index = {n["_n"]: i for i, n in enumerate(self.order)}
        self._emoji_cache = {}
        self.total = max(0.5, (len(self.order) - 1) * self.step + self.pause)
        self._fit()

    def order_index_of(self, node):
        try:
            return self.order_index[node["_n"]]
        except KeyError:
            return 0

    def dfs_order(self, node):
        out = [node]
        for c in node["children"]:
            out.extend(self.dfs_order(c))  # noqa: INP001
        return out

    def _fit(self):
        min_x = min(n["x"] - n["w"] / 2 for n in self.nodes)
        max_x = max(n["x"] + n["w"] / 2 for n in self.nodes)
        min_y = min(n["y"] - n["h"] / 2 for n in self.nodes)
        max_y = max(n["y"] + n["h"] / 2 for n in self.nodes)
        margin = 46
        bw = max(1.0, max_x - min_x)
        bh = max(1.0, max_y - min_y)
        scale = min((self.width - 2 * margin) / bw, (self.height - 2 * margin) / bh)
        self.scale = max(0.01, scale)
        self.base_x = min_x
        self.base_y = min_y
        out_w = bw * self.scale
        out_h = bh * self.scale
        self.off_x = (self.width - out_w) / 2 - min_x * self.scale
        self.off_y = (self.height - out_h) / 2 - min_y * self.scale
        self.fit_scale = self.scale
        self.fit_cx = min_x + bw / 2
        self.fit_cy = min_y + bh / 2

    def _box_of(self, n):
        return (n["x"] - n["w"] / 2, n["x"] + n["w"] / 2,
                n["y"] - n["h"] / 2, n["y"] + n["h"] / 2)

    def _union_target(self, a, b):
        """Enquadra a união das caixas dos dois nós vizinhos na animação, de
        forma que o que já está na tela continue visível durante a transição,
        mantendo o zoom próximo ao ramo em foco."""
        pa, pb = self._box_of(a), self._box_of(b)
        mnx = min(pa[0], pb[0]); mxx = max(pa[1], pb[1])
        mny = min(pa[2], pb[2]); mxy = max(pa[3], pb[3])
        bw = max(30, mxx - mnx)
        bh = max(30, mxy - mny)
        m = 90
        s = min((self.width - 2 * m) / bw, (self.height - 2 * m) / bh)
        s = min(max(s, self.fit_scale), self.fit_scale * 8)
        return ((mnx + mxx) / 2, (mny + mxy) / 2, s)

    def _camera(self, t):
        """Zoom guiado: título em tela cheia → navega pelos ramos mantendo o
        anterior visível → mapa completo, exibido apenas nos quadros finais
        (último nó + pausa)."""
        if not self.presentation:
            return
        last = len(self.order) - 1
        idx = min(last, int(t / self.step))
        if idx >= last:
            s = self.fit_scale
            self.scale = s
            self.off_x = self.width / 2 - self.fit_cx * s
            self.off_y = self.height / 2 - self.fit_cy * s
            return
        u = max(0.0, min(1.0, (t - idx * self.step) / self.step))
        if idx == 1 and self.step > 0.7:
            hold = 0.6
            u = max(0.0, min(1.0, (t - hold) / (self.step - hold)))
        e = u * u * (3 - 2 * u)
        pa = self.order[max(0, idx - 2)]
        ca = self._union_target(pa, self.order[max(0, idx - 1)])
        cb = self._union_target(self.order[max(0, idx - 1)], self.order[idx])
        s = ca[2] + (cb[2] - ca[2]) * e
        cx = ca[0] + (cb[0] - ca[0]) * e
        cy = ca[1] + (cb[1] - ca[1]) * e
        self.scale = s
        self.off_x = self.width / 2 - cx * s
        self.off_y = self.height / 2 - cy * s

    def w2x(self, wx):
        return wx * self.scale + self.off_x

    def w2y(self, wy):
        return wy * self.scale + self.off_y

    def font(self, size, bold=False):
        return load_font("", max(4, int(size * self.scale)), bold)

    def emoji_font(self, size=None):
        if getattr(self, "_emoji_font", None) is None:
            try:
                self._emoji_font = ImageFont.truetype(FONT_EMOJI, 109)
            except (OSError, TypeError, ValueError):
                self._emoji_font = False
        return self._emoji_font or None

    def emoji_image(self, run, size):
        """Renderiza um trecho de emoji (fonte bitmap CBDT, strike 109px)
        em imagem RGBA do tamanho alvo, com cache."""
        h = max(3, int(size * self.scale))
        key = (run, h)
        if key in self._emoji_cache:
            return self._emoji_cache[key]
        f = self.emoji_font()
        if f is None:
            return None
        try:
            tmp = Image.new("RGBA", (109 * len(run) + 40, 128), (0, 0, 0, 0))
            td = ImageDraw.Draw(tmp)
            td.text((8, 10), run, font=f, embedded_color=True)
            bbox = tmp.getbbox()
            if not bbox:
                self._emoji_cache[key] = None
                return None
            glyph = tmp.crop(bbox)
            w = max(1, int(round(glyph.width * h / glyph.height)))
            img = glyph.resize((w, h), Image.LANCZOS)
            self._emoji_cache[key] = img
            return img
        except Exception:
            self._emoji_cache[key] = None
            return None

    @staticmethod
    def state(t, start, fade):
        u = (t - start) / fade
        if u <= 0:
            return 0.0
        if u >= 1:
            return 1.0
        return u * u * (3 - 2 * u)  # smoothstep

    def draw_text(self, draw, cx_px, cy_px, text, size, fill, bold=False):
        """Texto centrado, com suporte a emojis adjacentes."""
        runs = _runs(text)
        f = self.font(size, bold)
        total = sum(
            size * 1.25 * len(run) if is_e else max(4, f.getbbox(run)[2])
            for run, is_e in runs
        )
        x = cx_px - total / 2
        asc, desc = f.getmetrics() if hasattr(f, "getmetrics") else (size, 0)
        y = cy_px - (asc + desc) / 2 + asc * 0.15
        for run, is_e in runs:
            if is_e:
                im = self.emoji_image("".join(run), size)
                if im is not None:
                    box = (
                        int(round(x - (size * 1.25 * len(run) - im.width) / 2)),
                        int(round(cy_px - im.height / 2)),
                    )
                    draw._image.paste(im, box, im)
                x += size * 1.25 * len(run)
            else:
                draw.text((x, y), run, font=f, fill=fill)
                x += max(4, f.getbbox(run)[2])

    def bezier_points(self, x0, y0, x1, y1, segments=26):
        d = abs(x1 - x0)
        x2 = x0 + d * 0.4
        x3 = x1 - d * 0.4
        pts = []
        for i in range(segments + 1):
            u = i / segments
            inv = 1 - u
            a, b, c, d_m = inv**3, 3 * inv * inv * u, 3 * inv * u * u, u**3
            pts.append(
                (
                    self.w2x(a * x0 + b * x2 + c * x3 + d_m * x1),
                    self.w2y(a * y0 + b * y0 + c * y1 + d_m * y1),
                )
            )
        return pts

    def frame(self, t):
        self._camera(t)
        img = Image.new("RGBA", (self.width, self.height), self.bg + (255,))
        draw = ImageDraw.Draw(img)

        for node, start in self.visible():
            progress = self.state(t, start, self.fade)
            if progress <= 0 or node.get("_parent") is None:
                continue
            alpha = int(255 * progress)
            p = node["_parent"]
            node_dir = 1 if node["side"] == "right" else -1
            pts = self.bezier_points(
                p["x"] + node_dir * p["w"] / 2, p["y"],
                node["x"] - node_dir * node["w"] / 2, node["y"],
            )
            cut = max(2, int(len(pts) * progress))
            width = max(2, int(2.2 * self.scale))
            for (x1, y1), (x2, y2) in zip(pts[:cut], pts[1 : cut + 1]):
                draw.line((x1, y1, x2, y2), fill=self.conn + (alpha,), width=width)

        for node, start in self.visible():
            progress = self.state(t, start, self.fade)
            if progress <= 0:
                continue
            alpha = int(255 * progress)
            scale = 0.6 + 0.4 * progress
            _NodeGeom(node, scale)._draw(draw, alpha, self)

        return np.asarray(img.convert("RGB"))

    def visible(self):
        for n in self.order:
            start = self.order_index_of(n) * self.step
            yield n, start


class _NodeGeom:
    def __init__(self, node, scale):
        self.node = node
        self.k = scale
        self.w = node["w"] * scale
        self.h = node["h"] * scale
        self.level = node["level"]

    def _draw(self, draw, alpha, r):
        n = self.node
        x = r.w2x(n["x"])
        y = r.w2y(n["y"])
        half_w = self.w / 2
        half_h = self.h / 2
        x0, y0, x1, y1 = x - half_w, y - half_h, x + half_w, y + half_h
        is_root = self.level == 0
        if is_root:
            # Título SEMPRE sem borda/contorno, mesmo quando --bordas está ativo
            # para os galhos: o título tem só o preenchimento suave.
            fill = r.root_bg + (alpha,)
            radius = min(11, self.h * 0.26)
            draw.rounded_rectangle((x0, y0, x1, y1), radius=radius, fill=fill, outline=None)
            text_size = 20 * self.k
            fill_text = r.root_fg + (alpha,)
        else:
            if r.borders:
                outline = r.conn + (alpha,)
                radius = min(7, self.h * 0.3)
                draw.rounded_rectangle((x0, y0, x1, y1), radius=radius, fill=None, outline=outline, width=max(1, int(2 * r.scale)))
            text_size = 13.5 * self.k
            fill_text = r.fg + (alpha,)
        r.draw_text(draw, x, y, n["text"], text_size, fill_text, bold=is_root)


# ---------------------------------------------------------------------------
# Codificação de vídeo (moviepy + ffmpeg)
# ---------------------------------------------------------------------------


def render_video(roots, colors, out, width=1280, height=720, fps=30,
                 step=0.45, fade=0.35, pause=2.0, presentation=True, borders=False, progress_cb=None):
    if Image is None or np is None:
        raise RuntimeError(deps_error("(Pillow/numpy)"))
    if VideoClip is None:
        raise RuntimeError(deps_error("(moviepy)"))
    static_layout(roots, 0)  # calcula x/y/w/h dos nós em coordenadas de mundo
    renderer = MapVideoRenderer(roots, colors, width, height, fps, step, fade, pause,
                                presentation=presentation, borders=borders)
    total = renderer.total

    def make_frame(t):
        if progress_cb and int(t * fps) % fps == 0:
            progress_cb(min(1.0, t / total))
        return renderer.frame(t)

    clip = VideoClip(make_frame, duration=total).with_fps(fps)
    codec = "libvpx-vp9" if out.lower().endswith(".webm") else "libx264"
    kwargs = {
        "filename": out,
        "codec": codec,
        "fps": fps,
        "audio": False,
        "logger": None,
        "preset": "medium",
    }
    if codec == "libx264":
        kwargs["ffmpeg_params"] = ["-pix_fmt", "yuv420p"]
    if codec == "libvpx-vp9":
        kwargs["bitrate"] = "4000k"
        kwargs["ffmpeg_params"] = ["-pix_fmt", "yuv420p"]
    clip.write_videofile(**kwargs)
    clip.close()
    if progress_cb:
        progress_cb(1.0)


# ---------------------------------------------------------------------------
# Linha de comando
# ---------------------------------------------------------------------------


def cli(argv=None):
    parser = argparse.ArgumentParser(
        description="Gera um vídeo animado a partir de um mapa mental (.minder, .mm) ou texto indentado."
    )
    parser.add_argument("entrada", nargs="?", help="arquivo .minder, .mm, .txt ou .md")
    parser.add_argument("-o", "--output", help="vídeo de saída (padrão: <entrada>.mp4/.webm)")
    parser.add_argument("--tema", choices=list(THEMES), default=None,
                        help="tema das cores (padrão: o tema do arquivo ou 'default')")
    parser.add_argument("--tempo-por-no", type=float, default=0.45,
                        help="segundos entre a entrada de cada nó (padrão: %(default)s)")
    parser.add_argument("--pausa-final", type=float, default=2.0,
                        help="segundos exibindo o mapa completo (padrão: %(default)s)")
    parser.add_argument("--camera", choices=("apresentacao", "panoramica"), default="apresentacao",
                        help="apresentacao: título em tela cheia, navega pelos ramos e só abre o mapa "
                             "completo no final; panoramica: vista fixa do mapa inteiro (padrão: %(default)s)")
    parser.add_argument("--bordas", action="store_true",
                        help="mantém as caixas (bordas) ao redor dos galhos (padrão: sem bordas)")
    parser.add_argument("--fade", type=float, default=0.35,
                        help="duração da transição de entrada de cada nó (padrão: %(default)s)")
    parser.add_argument("--largura", type=int, default=1280, help="largura do vídeo em pixels")
    parser.add_argument("--altura", type=int, default=720, help="altura do vídeo em pixels")
    parser.add_argument("--fps", type=int, default=30, choices=FPS_CHOICES,
                        help="quadros por segundo (padrão: %(default)s)")
    parser.add_argument("--listar-resolucoes", action="store_true",
                        help="mostra as resoluções predefinidas e sai")
    args = parser.parse_args(argv)

    if args.listar_resolucoes:
        for name, (w, h) in RESOLUTIONS.items():
            print("%5d x %5d  %s" % (w, h, name))
        return

    if np is None or Image is None or VideoClip is None:
        sys.stderr.write(deps_error() + "\n")
        sys.exit(1)

    if not args.entrada:
        parser.error("informe o arquivo de entrada (.minder, .mm, .txt ou .md)")

    roots, theme_attr = load_map(args.entrada)
    if not roots:
        parser.error("nenhum conteúdo reconhecido no arquivo de entrada")
    assign_sides(roots)

    colors = theme_of(theme_attr)
    theme_key = args.tema or "default"
    if theme_key == "default" and theme_attr and colors:
        pass  # tema do arquivo (.minder) prevalece quando --tema não é informado
    else:
        colors = dict(THEME_COLORS[theme_key])

    base, _ = os.path.splitext(args.entrada)
    out = args.output or (base + ".mp4")
    ext = os.path.splitext(out)[1].lower()
    if ext not in (".mp4", ".webm"):
        out += ".mp4"

    print("Nós: %d | Duração estimada: %.1f s | Formato: %dx%d @ %dfps" % (
        len(list(_all_nodes(roots))), _estimate(roots, args.tempo_por_no, args.fade, args.pausa_final),
        args.largura, args.altura, args.fps))
    render_video(roots, colors, out,
                 width=args.largura, height=args.altura, fps=args.fps,
                 step=args.tempo_por_no, fade=args.fade, pause=args.pausa_final,
                 presentation=(args.camera == "apresentacao"),
                 borders=args.bordas,
                 progress_cb=lambda p: _show_progress(p, out))
    print("")


def _all_nodes(roots):
    out = []

    def walk(n):
        out.append(n)
        for c in n["children"]:
            walk(c)
    for r in roots:
        walk(r)
    return out


def _count_nodes(roots):
    return sum(1 for _ in _all_nodes(roots))


def _estimate(roots, step, fade, pause):
    n = _count_nodes(roots)
    return max(0.5, (n - 1) * max(fade, step) + pause)


def _show_progress(p, out=None):
    bar_w = 30
    filled = int(bar_w * p)
    fmt = "\r[%s%s] %3d%%" % ("#" * filled, "-" * (bar_w - filled), int(p * 100))
    sys.stdout.write(fmt)
    sys.stdout.flush()
    if p >= 1:
        sys.stdout.write("\n")


# ---------------------------------------------------------------------------
# Interface gráfica (tkinter)
# ---------------------------------------------------------------------------


def run_gui():
    root = tk.Tk()
    MindMapVideoApp(root)
    root.mainloop()


class MindMapVideoApp:
    def __init__(self, root):
        self.root = root
        self.current_path = None
        self.roots = []
        self.file_theme = None
        self.worker = None

        root.title("Mapa mental -> Vídeo animado (Minder)")
        root.geometry("820x620")
        root.minsize(680, 520)

        toolbar = ttk.Frame(root, padding=(8, 6))
        toolbar.pack(side=tk.TOP, fill=tk.X)
        ttk.Button(toolbar, text="Carregar mapa...", command=self.load_file).pack(side=tk.LEFT)
        ttk.Button(toolbar, text="Carregar texto indentado...", command=self.load_text_file).pack(side=tk.LEFT, padx=(6, 0))
        self.lbl_file = ttk.Label(toolbar, text="Nenhum arquivo carregado", anchor=tk.W)
        self.lbl_file.pack(side=tk.LEFT, padx=(12, 0), fill=tk.X, expand=True)
        self.btn_generate = ttk.Button(toolbar, text="Gerar vídeo", command=self.generate)
        self.btn_generate.pack(side=tk.RIGHT)

        opts = ttk.LabelFrame(root, text="Opções do vídeo", padding=(10, 8))
        opts.pack(side=tk.TOP, fill=tk.X, padx=8, pady=(0, 4))

        row1 = ttk.Frame(opts)
        row1.pack(fill=tk.X, pady=2)
        ttk.Label(row1, text="Tema:").grid(row=0, column=0, sticky=tk.W)
        self.var_theme = tk.StringVar(value=THEMES["default"])
        cbo = ttk.Combobox(row1, state="readonly", values=list(THEMES.values()),
                           textvariable=self.var_theme, width=18)
        cbo.grid(row=0, column=1, padx=(2, 16), sticky=tk.W)

        ttk.Label(row1, text="Tempo por nó (s):").grid(row=0, column=2, sticky=tk.W)
        self.var_step = tk.DoubleVar(value=0.45)
        ttk.Spinbox(row1, from_=0.1, to=5.0, increment=0.05,
                    textvariable=self.var_step, width=6).grid(row=0, column=3, padx=(2, 16))

        ttk.Label(row1, text="Fade (s):").grid(row=0, column=4, sticky=tk.W)
        self.var_fade = tk.DoubleVar(value=0.35)
        ttk.Spinbox(row1, from_=0.05, to=3.0, increment=0.05,
                    textvariable=self.var_fade, width=6).grid(row=0, column=5)

        row2 = ttk.Frame(opts)
        row2.pack(fill=tk.X, pady=2)
        ttk.Label(row2, text="Pausa final (s):").grid(row=0, column=0, sticky=tk.W)
        self.var_pause = tk.DoubleVar(value=2.0)
        ttk.Spinbox(row2, from_=0, to=30, increment=0.5,
                    textvariable=self.var_pause, width=6).grid(row=0, column=1, padx=(2, 16))

        ttk.Label(row2, text="Resolução:").grid(row=0, column=2, sticky=tk.W)
        self.var_res = tk.StringVar(value="1280×720 (HD)")
        ttk.Combobox(row2, state="readonly", values=list(RESOLUTIONS),
                     textvariable=self.var_res, width=18).grid(row=0, column=3, padx=(2, 16))

        ttk.Label(row2, text="FPS:").grid(row=0, column=4, sticky=tk.W)
        self.var_fps = tk.IntVar(value=30)
        ttk.Combobox(row2, state="readonly", values=[str(f) for f in FPS_CHOICES],
                     textvariable=self.var_fps, width=6).grid(row=0, column=5)

        ttk.Label(row2, text="Formato:").grid(row=0, column=6, sticky=tk.W)
        self.var_ext = tk.StringVar(value="MP4")
        ttk.Combobox(row2, state="readonly", values=["MP4", "WEBM"],
                     textvariable=self.var_ext, width=8).grid(row=0, column=7, padx=(2, 0))

        row3 = ttk.Frame(opts)
        row3.pack(fill=tk.X, pady=2)
        self.var_pres = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            row3,
            text="Apresentação — título em tela cheia, navega pelos ramos e só abre o mapa completo no final",
            variable=self.var_pres,
        ).pack(side=tk.LEFT)
        self.var_borders = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            row3,
            text="Bordas nos galhos",
            variable=self.var_borders,
        ).pack(side=tk.LEFT, padx=(16, 0))

        self.lbl_total = ttk.Label(root, text="", anchor=tk.W, padding=(10, 0))
        self.lbl_total.pack(side=tk.TOP, fill=tk.X)

        body = ttk.LabelFrame(root, text="Mapa reconhecido", padding=(6, 6))
        body.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 4))
        self.tree = ttk.Treeview(body, show="tree", columns=("side",), height=10)
        self.tree.column("#0", width=460)
        self.tree.heading("side", text="Lado")
        self.tree.column("side", width=120, anchor=tk.CENTER)
        vsb = ttk.Scrollbar(body, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.LEFT, fill=tk.Y)

        bar = ttk.Frame(root, padding=(8, 4))
        bar.pack(side=tk.BOTTOM, fill=tk.X)
        self.progress = ttk.Progressbar(bar, mode="indeterminate")
        self.progress.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.status = ttk.Label(bar, text="Pronto. Carregue um .minder, .mm ou texto indentado.",
                                anchor=tk.W, padding=(8, 0))
        self.status.pack(side=tk.BOTTOM, fill=tk.X)

        for var in (self.var_step, self.var_fade, self.var_pause):
            var.trace_add("write", lambda *_: self.update_estimate())

        if Image is None or np is None or VideoClip is None:
            self.btn_generate.state(["disabled"])
            self.status.config(
                text="Dependências ausentes — rode no venv: source .venv/bin/activate && pip install -r requirements.txt"
            )

    def set_file(self, path):
        self.current_path = path
        self.lbl_file.config(text=os.path.basename(path))

    def load_file(self):
        path = filedialog.askopenfilename(
            title="Carregar mapa mental",
            filetypes=[
                ("Mapas mentais", "*.minder *.mm"),
                ("Todos os arquivos", "*.*"),
            ],
        )
        if not path:
            return
        self._open(path)

    def load_text_file(self):
        path = filedialog.askopenfilename(
            title="Carregar texto indentado",
            filetypes=[("Textos", "*.txt *.md *.outline"), ("Todos os arquivos", "*.*")],
        )
        if not path:
            return
        self._open(path)

    def _open(self, path):
        try:
            roots, theme_attr = load_map(path)
        except Exception as exc:
            messagebox.showerror("Erro ao ler o arquivo", "%s\n%s" % (path, exc))
            return
        if not roots:
            messagebox.showwarning("Mapa vazio", "Nenhum nó reconhecido neste arquivo.")
            return
        assign_sides(roots)
        self.roots = roots
        self.file_theme = theme_of(theme_attr)
        self.set_file(path)
        self._populate_tree()
        if self.file_theme:
            self.status.config(text="Mapa carregado com %d nó(s). Usando o tema do arquivo." % self._count())
        else:
            self.status.config(text="Mapa carregado com %d nó(s)." % self._count())
        combo_label = self.theme_label_of_file()
        if combo_label:
            self.var_theme.set(combo_label)
        self.update_estimate()

    def _count(self):
        return _count_nodes(self.roots)

    def _populate_tree(self):
        self.tree.delete(*self.tree.get_children())
        self._items = {}

        def add(parent_iid, node):
            label = node["text"] or "(sem texto)"
            side = "Raiz" if node["side"] is None else (node["side"] == "right" and "Direita" or "Esquerda")
            iid = self.tree.insert(parent_iid, "end", text="  " * node.get("level", 0) + label, values=(side,))
            for c in node["children"]:
                add(iid, c)

        for r in self.roots:
            add("", r)
        if self.roots:
            first = self.tree.get_children("")
            if first:
                self.tree.item(first[0], open=True)

    def update_estimate(self):
        if not self.roots:
            return
        n = self._count()
        total = _estimate(self.roots, self.var_step.get(), self.var_fade.get(), self.var_pause.get())
        self.lbl_total.config(text="%d nó(s) · duração estimada: %.1f s" % (n, total))

    def generate(self):
        if not self.roots:
            messagebox.showwarning("Nada para gerar", "Carregue um mapa mental primeiro.")
            return
        if self.worker and self.worker.is_alive():
            return

        colors = self._merge_colors(None)

        base = os.path.splitext(os.path.basename(self.current_path or "mapa"))[0]
        ext = (self.var_ext.get() or "MP4").lower()
        initial = base + "." + ext
        out = filedialog.asksaveasfilename(
            title="Salvar vídeo",
            initialfile=initial,
            defaultextension="." + ext,
            filetypes=[("Vídeo MP4", "*.mp4"), ("Vídeo WebM", "*.webm")],
        )
        if not out:
            return

        res = RESOLUTIONS.get(self.var_res.get(), (1280, 720))
        width, height = res
        fps = int(str(self.var_fps.get()).split()[0])
        step = self.var_step.get()
        fade = max(0.05, self.var_fade.get())
        pause = max(0, float(self.var_pause.get()))

        self._set_running(True)
        total = _estimate(self.roots, step, fade, pause)
        self.status.config(text="Gerando vídeo ~%.0f s... (aguarde)" % total)

        def work():
            try:
                render_video(self.roots, colors, out,
                             width=width, height=height, fps=fps,
                             step=step, fade=fade, pause=pause,
                             presentation=self.var_pres.get(),
                             progress_cb=lambda p: self._progress(out, p))
                self.root.after(0, self._done_video, out)
            except Exception as exc:
                self.root.after(0, self._fail_video, exc)

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def _merge_colors(self, base):
        label = self.var_theme.get()
        if self.file_theme and label == self.theme_label_of_file():
            out = dict(self.file_theme)
        else:
            out = dict(THEME_COLORS[key_by_label(THEMES, label, "default")])
        return out

    def theme_label_of_file(self):
        if not self.file_theme:
            return None
        name = self.file_theme.get("name")
        for key, colors in THEME_COLORS.items():
            if colors["name"] == name:
                return THEMES[key]
        return None

    def _progress(self, out, p):
        self.root.after(0, lambda: self.status.config(text="Gerando vídeo... %d%%" % int(p * 100)))

    def _done_video(self, out):
        self._set_running(False)
        self.status.config(text="Vídeo gerado: %s" % out)
        self.progress.stop()
        resp = messagebox.askyesno("Vídeo gerado", "Vídeo pronto em:\n%s\n\nAbrir com o reprodutor padrão?" % out)
        if resp:
            self._open_external(out)

    def _fail_video(self, exc):
        self._set_running(False)
        self.progress.stop()
        self.status.config(text="Falha ao gerar o vídeo.")
        messagebox.showerror("Falha ao gerar o vídeo", str(exc))

    def _set_running(self, running):
        if running:
            self.progress.start(12)
        else:
            self.progress.stop()

    @staticmethod
    def _open_external(path):
        import subprocess
        try:
            if sys.platform.startswith("win"):
                os.startfile(path)
            else:
                subprocess.Popen(["xdg-open", path],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass


if __name__ == "__main__":
    if len(sys.argv) > 1:
        cli()
    else:
        if tk is None:
            sys.stderr.write("tkinter não está disponível neste ambiente.\n")
            sys.exit(1)
        run_gui()