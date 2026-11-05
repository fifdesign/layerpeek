"""LayerPeek - .procreate file reader for Windows: view layers, export PSD / PNG."""
import ctypes
try:  # crisp rendering on high-DPI screens (must happen before Tk is created)
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # per-monitor
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass
import os, sys, zipfile, plistlib, struct, io, threading, traceback
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import numpy as np
from PIL import Image, ImageTk
from tkinterdnd2 import TkinterDnD, DND_FILES

import procreate_reader as pr


# ---------------------------------------------------------------- loading
def load_procreate(path, flip, progress=None):
    z = zipfile.ZipFile(path)
    names = z.namelist()
    plist = plistlib.loads(z.read("Document.archive"))
    objs = plist["$objects"]
    root = pr.resolve(objs, plist["$top"]["root"])
    W, H = pr.parse_size(root["size"])
    tile = int(root.get("tileSize", 256))
    layers = []
    raw_layers = list(reversed(root["layers"]))  # Procreate lists top layer first; we keep bottom first
    for i, L in enumerate(raw_layers):
        if progress:
            progress(i, len(raw_layers))
        try:
            arr, n = pr.load_layer(z, L["UUID"], W, H, tile, names)
        except Exception as e:
            arr, n = np.zeros((H, W, 4), np.uint8), 0
            print("layer failed:", e)
        if flip:
            arr = np.ascontiguousarray(arr[::-1])
        layers.append(dict(name=str(L.get("name") or "Layer %d" % (i + 1)),
                           arr=arr, opacity=float(L.get("opacity", 1.0)),
                           visible=not bool(L.get("hidden", False)), chunks=n))
    thumb = None
    if "QuickLook/Thumbnail.png" in names:
        thumb = Image.open(io.BytesIO(z.read("QuickLook/Thumbnail.png"))).convert("RGBA")
    return W, H, layers, thumb


def _prep(L):
    """Per-layer cache: bounding box + premultiplied float32 crop (computed once)."""
    if "pm" not in L:
        a = L["arr"]
        ys = np.flatnonzero(a[..., 3].any(axis=1))
        xs = np.flatnonzero(a[..., 3].any(axis=0))
        if len(ys) == 0:
            L["box"], L["pm"] = None, None
        else:
            t, b, l, r = ys[0], ys[-1] + 1, xs[0], xs[-1] + 1
            c = a[t:b, l:r].astype(np.float32) * (1.0 / 255.0)
            c[..., :3] *= c[..., 3:4]  # premultiply
            L["box"], L["pm"] = (t, b, l, r), c
    return L


def composite(W, H, layers, only_visible=True):
    """Fast premultiplied 'over' compositing, touching only each layer's bounding box."""
    canvas = np.zeros((H, W, 4), np.float32)
    for L in layers:
        if only_visible and not L["visible"]:
            continue
        _prep(L)
        if L["pm"] is None:
            continue
        t, b, l, r = L["box"]
        reg = canvas[t:b, l:r]
        src = L["pm"] * L["opacity"] if L["opacity"] != 1.0 else L["pm"]
        reg *= (1.0 - src[..., 3:4])
        reg += src
    a = canvas[..., 3:4]
    canvas[..., :3] /= np.maximum(a, 1e-6)
    canvas *= 255.0
    canvas += 0.5
    return canvas.astype(np.uint8)


# ---------------------------------------------------------------- PSD writer
def write_psd(path, W, H, layers):
    def u16(v): return struct.pack(">H", v)
    def i16(v): return struct.pack(">h", v)
    def u32(v): return struct.pack(">I", v)
    def i32(v): return struct.pack(">i", v)

    records, chan_data = [], []
    for L in layers:  # bottom -> top, same order as PSD
        a = L["arr"]
        ys, xs = np.nonzero(a[..., 3])
        if len(ys):
            t, b, l, r = ys.min(), ys.max() + 1, xs.min(), xs.max() + 1
        else:
            t = b = l = r = 0
        crop = a[t:b, l:r]
        chans = [(0, crop[..., 0]), (1, crop[..., 1]), (2, crop[..., 2]), (-1, crop[..., 3])]
        name = L["name"].encode("latin-1", "replace")[:255]
        pas = bytes([len(name)]) + name
        pas += b"\0" * ((-len(pas)) % 4)
        extra = u32(0) + u32(0) + pas
        rec = i32(int(t)) + i32(int(l)) + i32(int(b)) + i32(int(r)) + u16(4)
        blobs = []
        for cid, data in chans:
            blob = u16(0) + np.ascontiguousarray(data).tobytes()
            rec += i16(cid) + u32(len(blob))
            blobs.append(blob)
        rec += b"8BIM" + b"norm" + bytes([max(0, min(255, round(L["opacity"] * 255)))]) + b"\0"
        rec += bytes([0 if L["visible"] else 2]) + b"\0" + u32(len(extra)) + extra
        records.append(rec)
        chan_data.append(b"".join(blobs))

    layer_info = i16(len(layers)) + b"".join(records) + b"".join(chan_data)
    if len(layer_info) % 2:
        layer_info += b"\0"
    layer_section = u32(len(layer_info)) + layer_info + u32(0)  # + empty global mask

    comp = composite(W, H, layers).astype(np.float32)
    a = comp[..., 3:4] / 255.0
    flat = (comp[..., :3] * a + 255.0 * (1 - a)).astype(np.uint8)  # over white

    with open(path, "wb") as f:
        f.write(b"8BPS" + u16(1) + b"\0" * 6 + u16(3) + u32(H) + u32(W) + u16(8) + u16(3))
        f.write(u32(0))  # color mode data
        f.write(u32(0))  # image resources
        f.write(u32(len(layer_section)) + layer_section)
        f.write(u16(0))  # raw merged image, planar RGB
        for c in range(3):
            f.write(np.ascontiguousarray(flat[..., c]).tobytes())


# ---------------------------------------------------------------- GUI
APP_NAME = "LayerPeek"
APP_VERSION = "1.0"

ABOUT_TEXT = [
    ("h1", "%s  %s" % (APP_NAME, APP_VERSION)),
    ("p", ".procreate file reader for Windows"),
    ("p", "Open Procreate (.procreate) files on Windows, inspect and toggle their layers, "
          "and export them as PSD or PNG. No iPad, Mac or Procreate needed."),
    ("h2", "Basic operations"),
    ("b", "Open files: drag .procreate files from Explorer onto the window, press Ctrl+O, or "
          "click Open. You can select several files at once. Each file opens in its own tab."),
    ("b", "Switch files: click a tab or press Ctrl+Tab. Click the ✕ on a tab (or middle-click "
          "it, or press Ctrl+W) to close it."),
    ("b", "Layers: the panel on the left lists all layers, top layer first. Click the checkbox "
          "to show or hide a layer; the preview updates immediately. Hidden layers stay hidden "
          "in exports."),
    ("b", "Zoom and pan: mouse wheel zooms, drag with the left mouse button pans. "
          "Fit fits the image to the window, 100% shows it at actual size."),
    ("b", "Flip and rotate: Flip vertically, Flip horizontally and Rotate 90° "
          "(clockwise or counter-clockwise) apply to all layers of the current file. "
          "If an image opens upside down, use Flip vertically."),
    ("h2", "Exporting"),
    ("b", "Export PSD: one Photoshop file with every layer kept separate, including names, "
          "opacity and visibility. Opens in Photoshop, Krita, GIMP and others."),
    ("b", "Export layers as PNG: one transparent PNG per layer, numbered from the bottom layer up."),
    ("b", "Export merged PNG: a single image of all currently visible layers, with transparency."),
    ("b", "Batch export: exports every open file at once as PSD, PNG (visible layers merged), "
          "or both, into one folder. Files are named after the originals. "
          "Flips and rotations you applied are included."),
    ("h2", "Keyboard shortcuts"),
    ("b", "Ctrl+O  open files        Ctrl+W  close current tab"),
    ("b", "Ctrl+Tab / Ctrl+Shift+Tab  next / previous tab        F1  this window"),
    ("h2", "Good to know"),
    ("b", "Your original .procreate files are never modified. Flips and rotations only affect "
          "what you see and export."),
    ("b", "Layer blend modes (Multiply, Overlay, ...) are not applied. Every layer is treated as "
          "Normal, so the merged image can differ from Procreate if you used other blend modes. "
          "Individual layers are exported exactly."),
    ("b", "Not supported: brushes, text layers, animation and time-lapse video."),
    ("h2", "Credits"),
    ("p", "Built with Python, Pillow, NumPy and tkinterdnd2. Unofficial tool: not affiliated with "
          "or endorsed by Savage Interactive, the makers of Procreate. Procreate is a trademark "
          "of its owner."),
]


def show_about(app):
    """Modal-less About / Help window."""
    if getattr(app, "_about", None) and app._about.winfo_exists():
        app._about.lift()
        app._about.focus_set()
        return
    dlg = tk.Toplevel(app)
    app._about = dlg
    dlg.title("About – " + APP_NAME)
    dlg.transient(app)
    ui = app.ui
    dlg.geometry("%dx%d" % (620 * ui, 720 * ui))
    try:  # logo at the top
        base = getattr(sys, "_MEIPASS", os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
        logo = Image.open(os.path.join(base, "assets", "layerpeek-logo.png")).resize(
            (int(72 * ui), int(72 * ui)), Image.LANCZOS)
        dlg._logo = ImageTk.PhotoImage(logo)
        ttk.Label(dlg, image=dlg._logo).pack(pady=(int(10 * ui), 0))
    except Exception:
        pass
    frame = ttk.Frame(dlg, padding=int(8 * ui))
    frame.pack(fill="both", expand=True)
    txt = tk.Text(frame, wrap="word", relief="flat", padx=int(14 * ui), pady=int(10 * ui),
                  font=("Segoe UI", 10), cursor="arrow", spacing3=int(4 * ui))
    sb = ttk.Scrollbar(frame, command=txt.yview)
    txt.configure(yscrollcommand=sb.set)
    sb.pack(side="right", fill="y")
    txt.pack(side="left", fill="both", expand=True)
    txt.tag_configure("h1", font=("Segoe UI", 18, "bold"), spacing3=int(8 * ui))
    txt.tag_configure("h2", font=("Segoe UI", 12, "bold"), spacing1=int(14 * ui), spacing3=int(4 * ui))
    txt.tag_configure("b", lmargin1=int(16 * ui), lmargin2=int(30 * ui))
    for kind, text in ABOUT_TEXT:
        if kind == "b":
            txt.insert("end", "•  " + text + "\n", "b")
        else:
            txt.insert("end", text + "\n", kind)
    txt.configure(state="disabled")
    ttk.Button(dlg, text="Close", command=dlg.destroy).pack(pady=(0, int(8 * ui)))
    dlg.bind("<Escape>", lambda e: dlg.destroy())


class DocView(ttk.Frame):
    """One open .procreate file: layer panel + canvas. Lives in a notebook tab."""

    def __init__(self, master, app, path, flip):
        super().__init__(master)
        self.app, self.path = app, path
        self.W = self.H = 0
        self.layers = []
        self.zoom, self.fit = 1.0, True
        self.merged = self.checker = self.tkimg = None
        self.ready, self.error, self._res = False, None, None

        body = ttk.PanedWindow(self, orient="horizontal")
        body.pack(fill="both", expand=True)
        left = ttk.Frame(body)
        self.tree = ttk.Treeview(left, columns=("vis", "name", "op"), show="headings",
                                 selectmode="browse", height=20)
        self.tree.heading("vis", text="\U0001F441"); self.tree.column("vis", width=int(34 * app.ui), anchor="center")
        self.tree.heading("name", text="Layer"); self.tree.column("name", width=int(190 * app.ui))
        self.tree.heading("op", text="Opac."); self.tree.column("op", width=int(50 * app.ui), anchor="center")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<Button-1>", self.on_tree_click)
        body.add(left, weight=0)
        self.canvas = tk.Canvas(body, bg="#3a3a3a", highlightthickness=0)
        body.add(self.canvas, weight=1)
        self._sash_done = False

        def place_sash(e):
            # a tab laid out in the background can collapse the layer panel; set it once visible
            if not self._sash_done and body.winfo_width() > 400:
                self._sash_done = True
                body.sashpos(0, int(300 * app.ui))
        body.bind("<Configure>", place_sash)
        self.canvas.bind("<ButtonPress-1>", lambda e: self.canvas.scan_mark(e.x, e.y))
        self.canvas.bind("<B1-Motion>", lambda e: self.canvas.scan_dragto(e.x, e.y, gain=1))
        self.canvas.bind("<MouseWheel>",
                         lambda e: self.set_zoom(self.zoom * (1.15 if e.delta > 0 else 1 / 1.15)))
        self.canvas.bind("<Configure>", lambda e: self.render() if self.fit and self.ready else None)
        self.canvas.create_text(20, 20, anchor="nw", fill="white", text="Loading …")

        def work():
            try:
                self._res = load_procreate(path, flip, None)
            except Exception as e:
                traceback.print_exc()
                self.error = e
        threading.Thread(target=work, daemon=True).start()
        self.after(50, self._poll)

    def _poll(self):
        if self._res is None and self.error is None:
            self.after(50, self._poll)
            return
        if self.error is not None:
            self.app.doc_failed(self, self.error)
            return
        self.W, self.H, self.layers, _ = self._res
        self.ready = True
        self.refresh_tree()
        self.render()
        self.app.update_status()

    def refresh_tree(self):
        self.tree.delete(*self.tree.get_children())
        for i in reversed(range(len(self.layers))):  # top layer first
            L = self.layers[i]
            self.tree.insert("", "end", iid=str(i),
                             values=("☑" if L["visible"] else "☐", L["name"],
                                     "%d%%" % round(L["opacity"] * 100)))

    def on_tree_click(self, e):
        if self.tree.identify_region(e.x, e.y) == "cell" and self.tree.identify_column(e.x) == "#1":
            row = self.tree.identify_row(e.y)
            if row:
                L = self.layers[int(row)]
                L["visible"] = not L["visible"]
                self.tree.set(row, "vis", "☑" if L["visible"] else "☐")
                self.merged = None
                self.render()

    def rotate_cw(self):
        self._rotate(-1)

    def rotate_ccw(self):
        self._rotate(1)

    def _rotate(self, k):
        for L in self.layers:
            L["arr"] = np.ascontiguousarray(np.rot90(L["arr"], k))
            L.pop("pm", None)
            L.pop("box", None)
        self.W, self.H = self.H, self.W
        self.merged = self.checker = None
        self.fit = True
        self.render()
        self.app.update_status()

    def flip_horizontal(self):
        self._flip(lambda a: a[:, ::-1])

    def flip_vertical(self):
        self._flip(lambda a: a[::-1])

    def _flip(self, fn):
        for L in self.layers:
            L["arr"] = np.ascontiguousarray(fn(L["arr"]))
            L.pop("pm", None)
            L.pop("box", None)
        self.merged = None
        self.render()

    def set_fit(self):
        self.fit = True
        self.render()

    def set_zoom(self, z):
        if not self.ready:
            return
        self.fit = False
        self.zoom = max(0.02, min(16, z))
        self.render()

    def render(self):
        if not self.layers:
            return
        cw, ch = self.canvas.winfo_width(), self.canvas.winfo_height()
        if cw < 20 or ch < 20:
            return
        if self.merged is None:
            self.merged = Image.fromarray(composite(self.W, self.H, self.layers), "RGBA")
        if self.fit:
            self.zoom = min(cw / self.W, ch / self.H)
        w, h = max(1, int(self.W * self.zoom)), max(1, int(self.H * self.zoom))
        comp = self.merged.resize((w, h), Image.BILINEAR if self.zoom < 1 else Image.NEAREST,
                                  reducing_gap=2.0 if self.zoom < 0.5 else None)
        if self.checker is None or self.checker.size != (w, h):
            yy, xx = np.mgrid[0:h, 0:w]
            cs = max(8, int(12 * self.app.ui))
            chk = (((yy // cs) + (xx // cs)) % 2 * 40 + 200).astype(np.uint8)
            self.checker = Image.fromarray(np.stack([chk, chk, chk, np.full_like(chk, 255)], -1), "RGBA")
        bg = self.checker.copy()
        bg.alpha_composite(comp)
        self.tkimg = ImageTk.PhotoImage(bg)
        self.canvas.delete("all")
        x, y = max(0, (cw - w) // 2), max(0, (ch - h) // 2)
        self.canvas.create_image(x, y, image=self.tkimg, anchor="nw")
        self.canvas.config(scrollregion=(0, 0, max(cw, w), max(ch, h)))

    def base(self):
        return os.path.splitext(os.path.basename(self.path))[0]

    # exports
    def export_psd(self):
        p = filedialog.asksaveasfilename(defaultextension=".psd", initialfile=self.base() + ".psd",
                                         filetypes=[("Photoshop", "*.psd")])
        if p:
            write_psd(p, self.W, self.H, self.layers)
            messagebox.showinfo("Done", "Saved PSD with %d layers." % len(self.layers))

    def export_pngs(self):
        d = filedialog.askdirectory(title="Choose a folder for the layer PNGs")
        if d:
            for i, L in enumerate(self.layers):
                Image.fromarray(L["arr"], "RGBA").save(
                    os.path.join(d, "%02d_%s.png" % (i + 1, pr.safe(L["name"]))))
            messagebox.showinfo("Done", "Saved %d transparent PNGs." % len(self.layers))

    def export_merged(self):
        p = filedialog.asksaveasfilename(defaultextension=".png", initialfile=self.base() + ".png",
                                         filetypes=[("PNG", "*.png")])
        if p:
            Image.fromarray(composite(self.W, self.H, self.layers), "RGBA").save(p)


class App(TkinterDnD.Tk):
    def __init__(self, paths=()):
        super().__init__()
        self.title("%s %s" % (APP_NAME, APP_VERSION))
        try:  # window + taskbar icon (works from source and from the PyInstaller exe)
            base = getattr(sys, "_MEIPASS", os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
            self.iconbitmap(default=os.path.join(base, "assets", "layerpeek.ico"))
        except Exception:
            pass
        self.ui = max(1.0, self.winfo_fpixels("1i") / 96.0)  # UI scale factor
        self.geometry("%dx%d" % (1150 * self.ui, 760 * self.ui))
        ttk.Style().configure("Treeview", rowheight=int(22 * self.ui))

        bar = ttk.Frame(self, padding=(4, 4, 4, 0))
        bar.pack(fill="x")
        bar2 = ttk.Frame(self, padding=4)
        bar2.pack(fill="x")
        for text, cmd in [("Open…", self.open_dialog), ("Close tab", self.close_current),
                          ("Export PSD…", lambda: self.cur("export_psd")),
                          ("Export layers as PNG…", lambda: self.cur("export_pngs")),
                          ("Export merged PNG…", lambda: self.cur("export_merged"))]:
            ttk.Button(bar, text=text, command=cmd).pack(side="left", padx=2)
        ttk.Button(bar, text="Batch export…", command=self.batch_export).pack(side="left", padx=10)
        ttk.Button(bar2, text="Flip vertically", command=lambda: self.cur("flip_vertical")).pack(side="left", padx=(10, 2))
        ttk.Button(bar2, text="Flip horizontally", command=lambda: self.cur("flip_horizontal")).pack(side="left", padx=2)
        ttk.Button(bar2, text="Rotate right 90°", command=lambda: self.cur("rotate_cw")).pack(side="left", padx=2)
        ttk.Button(bar2, text="Rotate left 90°", command=lambda: self.cur("rotate_ccw")).pack(side="left", padx=2)
        ttk.Button(bar2, text="About", command=lambda: show_about(self)).pack(side="right", padx=(2, 12))
        ttk.Button(bar2, text="Fit", command=lambda: self.cur("set_fit")).pack(side="right", padx=2)
        ttk.Button(bar2, text="100%", command=lambda: self.cur("set_zoom", 1.0)).pack(side="right", padx=2)

        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True)
        self.nb.enable_traversal()  # Ctrl+Tab / Ctrl+Shift+Tab
        self.nb.bind("<<NotebookTabChanged>>", lambda e: self.update_status())
        self.nb.bind("<Button-1>", self.on_tab_click, add="+")  # click the x on a tab to close it
        self.nb.bind("<Button-2>", self.on_middle_click)  # middle click closes a tab

        self.hint = ttk.Label(self, text="Drag .procreate files here from Explorer\nor press Ctrl+O",
                              anchor="center", justify="center", font=("Segoe UI", 14))
        self.hint.place(relx=0.5, rely=0.5, anchor="center")
        self.status = ttk.Label(self, text="No file open.", anchor="w", padding=3)
        self.status.pack(fill="x", side="bottom")

        self.drop_target_register(DND_FILES)
        self.dnd_bind("<<Drop>>", lambda e: self.open_paths(self.tk.splitlist(e.data)))
        self.bind("<Control-o>", lambda e: self.open_dialog())
        self.bind("<Control-w>", lambda e: self.close_current())
        self.bind("<F1>", lambda e: show_about(self))
        menubar = tk.Menu(self)
        helpmenu = tk.Menu(menubar, tearoff=0)
        helpmenu.add_command(label="About / How to use", accelerator="F1", command=lambda: show_about(self))
        menubar.add_cascade(label="Help", menu=helpmenu)
        self.config(menu=menubar)
        self.after(100, lambda: self.open_paths(paths))

    def docs(self):
        return [self.nametowidget(t) for t in self.nb.tabs()]

    def current(self):
        t = self.nb.select()
        return self.nametowidget(t) if t else None

    def cur(self, method, *a):
        d = self.current()
        if d is None or not d.ready:
            messagebox.showinfo("Nothing open", "Open a .procreate file first.")
            return
        getattr(d, method)(*a)

    def open_dialog(self):
        self.open_paths(filedialog.askopenfilenames(
            filetypes=[("Procreate", "*.procreate"), ("All", "*.*")]))

    def open_paths(self, paths):
        for p in paths:
            p = os.path.normpath(p)
            if not p.lower().endswith(".procreate"):
                messagebox.showwarning("Not a Procreate file", os.path.basename(p))
                continue
            existing = [d for d in self.docs() if os.path.normcase(d.path) == os.path.normcase(p)]
            if existing:
                self.nb.select(existing[0])
                continue
            d = DocView(self.nb, self, p, True)
            self.nb.add(d, text=" " + os.path.basename(p) + "   ✕ ")
            self.nb.select(d)
        self.sync_hint()
        self.update_status()

    def sync_hint(self):
        if self.nb.tabs():
            self.hint.place_forget()
        else:
            self.hint.place(relx=0.5, rely=0.5, anchor="center")

    def close_current(self):
        d = self.current()
        if d is not None:
            self.nb.forget(d)
            d.destroy()
        self.sync_hint()
        self.update_status()

    def _tab_right_edge(self, x, y, idx):
        """Find the right edge of tab `idx` by probing pixels (ttk.Notebook has no tab bbox)."""
        while True:
            try:
                if self.nb.index("@%d,%d" % (x + 1, y)) != idx:
                    return x
            except tk.TclError:
                return x
            x += 1
            if x > self.nb.winfo_width():
                return x

    def on_tab_click(self, e):
        try:
            idx = self.nb.index("@%d,%d" % (e.x, e.y))
        except tk.TclError:
            return
        if e.x >= self._tab_right_edge(e.x, e.y, idx) - int(28 * self.ui):  # the x at the tab's end
            self.nb.select(idx)
            self.close_current()
            return "break"

    def on_middle_click(self, e):
        try:
            idx = self.nb.index("@%d,%d" % (e.x, e.y))
        except tk.TclError:
            return
        self.nb.select(idx)
        self.close_current()

    def batch_export(self):
        docs = [d for d in self.docs() if d.ready]
        if not docs:
            messagebox.showinfo("Nothing open", "Open at least one .procreate file first.")
            return
        dlg = tk.Toplevel(self)
        dlg.title("Batch export")
        dlg.transient(self)
        dlg.resizable(False, False)
        pad = int(10 * self.ui)
        ttk.Label(dlg, text="Export all %d open file(s):" % len(docs)).pack(anchor="w", padx=pad, pady=(pad, 4))
        v_psd, v_png = tk.BooleanVar(value=True), tk.BooleanVar(value=False)
        ttk.Checkbutton(dlg, text="PSD (all layers, hidden layers stay hidden)", variable=v_psd).pack(anchor="w", padx=pad)
        ttk.Checkbutton(dlg, text="PNG (visible layers only, merged)", variable=v_png).pack(anchor="w", padx=pad)
        out = {}

        def go():
            if not (v_psd.get() or v_png.get()):
                messagebox.showinfo("Batch export", "Pick at least one format.", parent=dlg)
                return
            folder = filedialog.askdirectory(title="Choose the output folder", parent=dlg)
            if not folder:
                return
            out.update(folder=folder, psd=v_psd.get(), png=v_png.get())
            dlg.destroy()
        row = ttk.Frame(dlg)
        row.pack(fill="x", padx=pad, pady=pad)
        ttk.Button(row, text="Choose folder & export", command=go).pack(side="right")
        ttk.Button(row, text="Cancel", command=dlg.destroy).pack(side="right", padx=6)
        dlg.grab_set()
        self.wait_window(dlg)
        if not out:
            return

        used, done, errors = set(), 0, []
        for i, d in enumerate(docs, 1):
            self.status.config(text="Exporting %d / %d: %s …" % (i, len(docs), d.base()))
            self.update_idletasks()
            base, n = d.base(), 1
            while base.lower() in used:  # same name from different folders
                n += 1
                base = "%s_%d" % (d.base(), n)
            used.add(base.lower())
            try:
                if out["psd"]:
                    write_psd(os.path.join(out["folder"], base + ".psd"), d.W, d.H, d.layers)
                if out["png"]:
                    Image.fromarray(composite(d.W, d.H, d.layers), "RGBA").save(
                        os.path.join(out["folder"], base + ".png"))
                done += 1
            except Exception as e:
                errors.append("%s: %s" % (d.base(), e))
        self.update_status()
        msg = "Exported %d of %d file(s) to:\n%s" % (done, len(docs), out["folder"])
        if errors:
            msg += "\n\nFailed:\n" + "\n".join(errors)
        messagebox.showinfo("Batch export", msg)

    def doc_failed(self, d, err):
        messagebox.showerror("Could not open file", "%s\n\n%s" % (os.path.basename(d.path), err))
        self.nb.forget(d)
        d.destroy()
        self.sync_hint()
        self.update_status()

    def update_status(self):
        d = self.current()
        if d is None:
            self.status.config(text="No file open.")
        elif not d.ready:
            self.status.config(text="Loading %s …" % os.path.basename(d.path))
        else:
            self.status.config(text="%s  —  %d × %d px, %d layers    [%d file(s) open]" %
                               (os.path.basename(d.path), d.W, d.H, len(d.layers), len(self.nb.tabs())))
            d.render()


if __name__ == "__main__":
    App(sys.argv[1:]).mainloop()
