"""Minia — choisis une image de ta vidéo et elle devient sa miniature Windows."""

from __future__ import annotations

import ctypes
import os
import random
import sys
from pathlib import Path

from PySide6.QtCore import (QByteArray, QBuffer, QIODevice, QObject, QPointF, QRectF,
                            QRunnable, QSize, Qt, QThreadPool, QTimer, Signal)
from PySide6.QtGui import (QColor, QFont, QIcon, QImage, QKeySequence, QLinearGradient,
                           QPainter, QPainterPath, QPalette, QPen, QPixmap, QPolygonF,
                           QShortcut)
from PySide6.QtWidgets import (QAbstractButton, QApplication, QButtonGroup, QFileDialog,
                               QFrame, QGridLayout, QHBoxLayout, QLabel, QMainWindow,
                               QMessageBox, QPushButton, QSizePolicy, QStackedWidget,
                               QVBoxLayout, QWidget)

import core

FRAME_COUNT = 12
COLUMNS = 4

# Palette
BG = "#121317"
SURFACE = "#1b1c22"
SURFACE_2 = "#23252d"
BORDER = "#2d3039"
BORDER_HOVER = "#474b59"
TEXT = "#e8e9ee"
MUTED = "#8c8f9c"
ACCENT = "#7b6cff"
ACCENT_HOVER = "#8f82ff"
SUCCESS = "#4cc38a"
DANGER = "#ff6b6b"

STYLE = f"""
QMainWindow, QDialog, QMessageBox {{ background: {BG}; }}
QWidget {{ color: {TEXT}; font-family: "Segoe UI Variable Text", "Segoe UI"; font-size: 10pt; }}
QLabel {{ background: transparent; }}
QLabel#title {{ font-size: 14pt; font-weight: 600; }}
QLabel#hero {{ font-size: 26pt; font-weight: 700; }}
QLabel#muted, QLabel#section {{ color: {MUTED}; }}
QLabel#section {{ font-size: 8.5pt; font-weight: 600; letter-spacing: 1px; }}
QLabel#ok {{ color: {SUCCESS}; }}
QLabel#err {{ color: {DANGER}; }}
QFrame#panel {{ background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 14px; }}
QPushButton {{
    background: {SURFACE_2}; border: 1px solid {BORDER}; border-radius: 8px;
    padding: 8px 16px;
}}
QPushButton:hover {{ background: #2b2e38; border-color: {BORDER_HOVER}; }}
QPushButton:pressed {{ background: #1f2128; }}
QPushButton:disabled {{ color: #5b5e6a; border-color: #25272e; }}
QPushButton#primary {{
    background: {ACCENT}; border: none; color: white; font-weight: 600; padding: 11px 16px;
}}
QPushButton#primary:hover {{ background: {ACCENT_HOVER}; }}
QPushButton#primary:disabled {{ background: #2a2b40; color: #6c6f8c; }}
QPushButton#link {{
    background: transparent; border: none; color: {MUTED}; padding: 2px 0; text-align: left;
}}
QPushButton#link:hover {{ color: {TEXT}; text-decoration: underline; }}
QToolTip {{ background: {SURFACE_2}; color: {TEXT}; border: 1px solid {BORDER}; padding: 4px 6px; }}
QMessageBox QLabel {{ color: {TEXT}; }}
"""


def fmt_time(seconds: float) -> str:
    seconds = int(max(0, seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def image_to_jpeg(img: QImage, quality: int = 92) -> bytes:
    data = QByteArray()
    buf = QBuffer(data)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.convertToFormat(QImage.Format.Format_RGB888).save(buf, "JPG", quality)
    return bytes(data)


def make_icon_image(size: int = 256) -> QImage:
    """Icône de l'application dessinée en code (pas de fichier externe)."""
    img = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    s = size / 256
    grad = QLinearGradient(0, 0, size, size)
    grad.setColorAt(0, QColor("#9a8cff"))
    grad.setColorAt(1, QColor("#5b4bf0"))
    bg = QPainterPath()
    bg.addRoundedRect(QRectF(8 * s, 8 * s, 240 * s, 240 * s), 56 * s, 56 * s)
    p.fillPath(bg, grad)
    frame = QRectF(52 * s, 70 * s, 152 * s, 116 * s)
    p.setPen(QPen(QColor("white"), 14 * s, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                  Qt.PenJoinStyle.RoundJoin))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawRoundedRect(frame, 18 * s, 18 * s)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor("white"))
    p.drawEllipse(QPointF(160 * s, 106 * s), 14 * s, 14 * s)
    clip = QPainterPath()
    clip.addRoundedRect(frame.adjusted(7 * s, 7 * s, -7 * s, -7 * s), 11 * s, 11 * s)
    p.setClipPath(clip)
    p.drawPolygon(QPolygonF([QPointF(60 * s, 186 * s), QPointF(108 * s, 122 * s),
                             QPointF(144 * s, 168 * s), QPointF(164 * s, 146 * s),
                             QPointF(204 * s, 186 * s)]))
    p.end()
    return img


def save_icon(path: str) -> None:
    make_icon_image(256).save(path)


# =========================================================================== tâches de fond

class JobSignals(QObject):
    loaded = Signal(int, float, QImage)          # gen, durée, cover actuelle
    frame = Signal(int, int, float, QImage)      # gen, index, instant, image
    failed = Signal(int, int, str)               # gen, index, message
    applied = Signal(str, bool, str)             # chemin, succès, message


class LoadJob(QRunnable):
    def __init__(self, path: str, gen: int, signals: JobSignals):
        super().__init__()
        self.path, self.gen, self.signals = path, gen, signals

    def run(self):
        try:
            duration = core.probe_duration(self.path)
            cover = core.read_cover(self.path)
            img = QImage.fromData(cover) if cover else QImage()
            self.signals.loaded.emit(self.gen, duration, img)
        except Exception as exc:  # noqa: BLE001
            self.signals.failed.emit(self.gen, -1, str(exc))


class FrameJob(QRunnable):
    def __init__(self, path: str, gen: int, index: int, t: float, signals: JobSignals):
        super().__init__()
        self.path, self.gen, self.index, self.t, self.signals = path, gen, index, t, signals

    def run(self):
        try:
            t = self.t
            data = core.extract_frame(self.path, t)
            if not data and t > 2:  # trop près de la fin : on recule un peu
                t = max(0.0, t - 2)
                data = core.extract_frame(self.path, t)
            img = QImage.fromData(data) if data else QImage()
            if img.isNull():
                raise core.MiniaError("image illisible")
            self.signals.frame.emit(self.gen, self.index, t, img)
        except Exception as exc:  # noqa: BLE001
            self.signals.failed.emit(self.gen, self.index, str(exc))


class ApplyJob(QRunnable):
    def __init__(self, path: str, jpeg: bytes, signals: JobSignals):
        super().__init__()
        self.path, self.jpeg, self.signals = path, jpeg, signals

    def run(self):
        try:
            core.write_cover(self.path, self.jpeg)
            self.signals.applied.emit(self.path, True, "")
        except Exception as exc:  # noqa: BLE001
            self.signals.applied.emit(self.path, False, str(exc))


# =========================================================================== widgets

class ImageCanvas:
    """Dessine une image ajustée dans un cadre arrondi (avec cache de mise à l'échelle)."""

    def __init__(self, radius: float = 10):
        self.img: QImage | None = None
        self.radius = radius
        self._cache: QPixmap | None = None

    def set_image(self, img: QImage | None):
        self.img = img if img is not None and not img.isNull() else None
        self._cache = None

    def paint(self, p: QPainter, rect: QRectF, dpr: float, placeholder: str = "") -> QPainterPath:
        path = QPainterPath()
        path.addRoundedRect(rect, self.radius, self.radius)
        p.fillPath(path, QColor("#0d0e11"))
        if self.img is not None:
            target = QSize(self.img.width(), self.img.height()).scaled(
                QSize(int(rect.width()), int(rect.height())), Qt.AspectRatioMode.KeepAspectRatio)
            wanted = QSize(round(target.width() * dpr), round(target.height() * dpr))
            if self._cache is None or self._cache.size() != wanted:
                self._cache = QPixmap.fromImage(self.img.scaled(
                    wanted, Qt.AspectRatioMode.IgnoreAspectRatio,
                    Qt.TransformationMode.SmoothTransformation))
                self._cache.setDevicePixelRatio(dpr)
            x = rect.x() + (rect.width() - target.width()) / 2
            y = rect.y() + (rect.height() - target.height()) / 2
            p.save()
            p.setClipPath(path)
            p.drawPixmap(QPointF(x, y), self._cache)
            p.restore()
        elif placeholder:
            p.setPen(QColor(MUTED))
            p.drawText(rect, Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap, placeholder)
        return path


class ImageView(QWidget):
    """Widget d'aperçu : une image ou un texte de remplacement."""

    def __init__(self, placeholder: str = "", radius: float = 10, parent=None):
        super().__init__(parent)
        self.canvas = ImageCanvas(radius)
        self.placeholder = placeholder

    def set_image(self, img: QImage | None):
        self.canvas.set_image(img)
        self.update()

    def image(self) -> QImage | None:
        return self.canvas.img

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
        path = self.canvas.paint(p, QRectF(self.rect()).adjusted(1, 1, -1, -1),
                                 self.devicePixelRatioF(), self.placeholder)
        p.setPen(QPen(QColor(BORDER), 1))
        p.drawPath(path)


class AspectImageView(ImageView):
    """ImageView qui garde un ratio 16:9 en fonction de sa largeur."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        sp = QSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        sp.setHeightForWidth(True)
        self.setSizePolicy(sp)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, w):
        return int(w * 9 / 16)

    def sizeHint(self):
        return QSize(320, 180)


class FrameTile(QAbstractButton):
    """Vignette cliquable d'une image de la vidéo."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumSize(150, 90)
        self.canvas = ImageCanvas(radius=10)
        self.t = 0.0
        self.state = "loading"  # loading | ready | error
        self._hover = False

    def reset(self):
        self.state = "loading"
        self.canvas.set_image(None)
        self.setEnabled(False)
        self.update()

    def set_frame(self, img: QImage, t: float):
        self.canvas.set_image(img)
        self.t = t
        self.state = "ready"
        self.setEnabled(True)
        self.update()

    def set_error(self):
        self.state = "error"
        self.setEnabled(False)
        self.update()

    def image(self) -> QImage | None:
        return self.canvas.img

    def enterEvent(self, e):
        self._hover = True
        self.update()
        super().enterEvent(e)

    def leaveEvent(self, e):
        self._hover = False
        self.update()
        super().leaveEvent(e)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
        rect = QRectF(self.rect()).adjusted(3, 3, -3, -3)
        placeholder = {"loading": "Chargement…", "error": "Image indisponible"}.get(self.state, "")
        path = self.canvas.paint(p, rect, self.devicePixelRatioF(), placeholder)

        if self.state == "ready":
            label = fmt_time(self.t)
            f = QFont(self.font())
            f.setPointSizeF(8.5)
            f.setBold(True)
            p.setFont(f)
            w = p.fontMetrics().horizontalAdvance(label) + 14
            badge = QRectF(rect.right() - w - 8, rect.bottom() - 26, w, 20)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(0, 0, 0, 170))
            p.drawRoundedRect(badge, 6, 6)
            p.setPen(QColor("white"))
            p.drawText(badge, Qt.AlignmentFlag.AlignCenter, label)

        if self.isChecked():
            p.setPen(QPen(QColor(ACCENT), 3))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(path)
            # pastille de sélection
            c = QPointF(rect.left() + 18, rect.top() + 18)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(ACCENT))
            p.drawEllipse(c, 10, 10)
            p.setPen(QPen(QColor("white"), 2.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                          Qt.PenJoinStyle.RoundJoin))
            p.drawPolyline(QPolygonF([c + QPointF(-4.5, 0), c + QPointF(-1.2, 3.4),
                                      c + QPointF(4.8, -3.4)]))
        else:
            hovered = self._hover and self.state == "ready"
            p.setPen(QPen(QColor(BORDER_HOVER if hovered else BORDER), 1.5 if hovered else 1))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(path)


class DropZone(QWidget):
    """Grande zone « glisse tes vidéos ici », cliquable pour parcourir."""

    clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.active = False
        self._hover = False
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumSize(520, 300)

    def set_active(self, on: bool):
        self.active = on
        self.update()

    def enterEvent(self, e):
        self._hover = True
        self.update()

    def leaveEvent(self, e):
        self._hover = False
        self.update()

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton and self.rect().contains(e.position().toPoint()):
            self.clicked.emit()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(2, 2, -2, -2)
        hot = self.active or self._hover
        fill = QColor(ACCENT) if self.active else QColor(SURFACE)
        if self.active:
            fill.setAlpha(28)
        p.setBrush(fill)
        pen = QPen(QColor(ACCENT if hot else BORDER_HOVER), 2, Qt.PenStyle.CustomDashLine)
        pen.setDashPattern([5, 4])
        p.setPen(pen)
        p.drawRoundedRect(r, 18, 18)

        # pictogramme : cercle + flèche
        cx, cy = r.center().x(), r.center().y() - 34
        circle = QColor(ACCENT)
        circle.setAlpha(60 if hot else 35)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(circle)
        p.drawEllipse(QPointF(cx, cy), 34, 34)
        p.setPen(QPen(QColor(ACCENT_HOVER if hot else ACCENT), 3.2, Qt.PenStyle.SolidLine,
                      Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        p.drawLine(QPointF(cx, cy - 13), QPointF(cx, cy + 11))
        p.drawPolyline(QPolygonF([QPointF(cx - 10, cy + 1), QPointF(cx, cy + 11),
                                  QPointF(cx + 10, cy + 1)]))

        f = QFont(self.font())
        f.setPointSizeF(13)
        f.setBold(True)
        p.setFont(f)
        p.setPen(QColor(TEXT))
        p.drawText(QRectF(r.left(), cy + 48, r.width(), 28), Qt.AlignmentFlag.AlignCenter,
                   "Relâche pour ajouter" if self.active else "Glisse tes vidéos ici")
        f.setPointSizeF(9.5)
        f.setBold(False)
        p.setFont(f)
        p.setPen(QColor(MUTED))
        p.drawText(QRectF(r.left(), cy + 78, r.width(), 22), Qt.AlignmentFlag.AlignCenter,
                   "ou clique pour parcourir  ·  MP4, M4V, MOV  ·  dossiers acceptés")


class ElidedLabel(QLabel):
    def __init__(self, text="", parent=None):
        super().__init__(parent)
        self._full = text
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setText(text)

    def setText(self, text):
        self._full = text
        self.setToolTip(text)
        self._elide()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._elide()

    def _elide(self):
        super().setText(self.fontMetrics().elidedText(self._full, Qt.TextElideMode.ElideMiddle,
                                                      max(10, self.width())))


# =========================================================================== fenêtre

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Minia")
        self.setWindowIcon(QIcon(QPixmap.fromImage(make_icon_image())))
        self.resize(1240, 780)
        self.setMinimumSize(900, 600)
        self.setAcceptDrops(True)

        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(max(2, min(6, os.cpu_count() or 4)))
        self.signals = JobSignals()
        self.signals.loaded.connect(self._on_loaded)
        self.signals.frame.connect(self._on_frame)
        self.signals.failed.connect(self._on_failed)
        self.signals.applied.connect(self._on_applied)

        self.queue: list[str] = []
        self.pos = -1
        self.current: str | None = None
        self.duration = 0.0
        self.load_gen = 0
        self.frame_gen = 0
        self.busy = False
        self.round = 0
        self.selected: int | None = None

        self.stack = QStackedWidget()
        self.stack.addWidget(self._build_home())
        self.stack.addWidget(self._build_editor())
        self.setCentralWidget(self.stack)

        self._dots = 0
        self._busy_timer = QTimer(self, interval=400, timeout=self._tick_busy)

        QShortcut(QKeySequence("Ctrl+O"), self, activated=self.browse)
        QShortcut(QKeySequence(Qt.Key.Key_Return), self, activated=self.apply)
        QShortcut(QKeySequence(Qt.Key.Key_Enter), self, activated=self.apply)
        QShortcut(QKeySequence("R"), self, activated=lambda: self.request_frames(False))

    # ------------------------------------------------------------------ construction UI

    def _build_home(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(48, 40, 48, 40)
        lay.addStretch(1)
        hero = QLabel("Minia", objectName="hero", alignment=Qt.AlignmentFlag.AlignCenter)
        sub = QLabel("Choisis une image de ta vidéo : elle devient sa miniature dans Windows.",
                     objectName="muted", alignment=Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(hero)
        lay.addWidget(sub)
        lay.addSpacing(28)
        self.drop = DropZone()
        self.drop.clicked.connect(self.browse)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(self.drop, 6)
        row.addStretch(1)
        lay.addLayout(row, 4)
        lay.addSpacing(18)
        note = QLabel("La vidéo n'est jamais réencodée : seule la miniature intégrée au fichier change.",
                      objectName="muted", alignment=Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(note)
        lay.addStretch(1)
        return page

    def _build_editor(self) -> QWidget:
        page = QWidget()
        root = QVBoxLayout(page)
        root.setContentsMargins(24, 20, 24, 16)
        root.setSpacing(16)

        # en-tête
        head = QHBoxLayout()
        titles = QVBoxLayout()
        titles.setSpacing(2)
        self.file_title = ElidedLabel()
        self.file_title.setObjectName("title")
        self.file_meta = ElidedLabel()
        self.file_meta.setObjectName("muted")
        titles.addWidget(self.file_title)
        titles.addWidget(self.file_meta)
        head.addLayout(titles, 1)
        self.skip_btn = QPushButton("Passer à la suivante")
        self.skip_btn.clicked.connect(self.next_video)
        self.change_btn = QPushButton("Autre vidéo…")
        self.change_btn.clicked.connect(self.browse)
        self.close_btn = QPushButton("Fermer")
        self.close_btn.clicked.connect(self.close_all)
        for b in (self.skip_btn, self.change_btn, self.close_btn):
            head.addWidget(b)
        root.addLayout(head)

        body = QHBoxLayout()
        body.setSpacing(18)

        # grille d'images
        grid_panel = QFrame(objectName="panel")
        gl = QVBoxLayout(grid_panel)
        gl.setContentsMargins(14, 14, 14, 14)
        gl.setSpacing(10)
        gh = QHBoxLayout()
        gh.addWidget(QLabel("IMAGES DE LA VIDÉO", objectName="section"))
        gh.addStretch(1)
        self.more_btn = QPushButton("↻  Autres images")
        self.more_btn.setToolTip("Propose une nouvelle sélection d'images (touche R)")
        self.more_btn.clicked.connect(lambda: self.request_frames(False))
        gh.addWidget(self.more_btn)
        gl.addLayout(gh)
        grid = QGridLayout()
        grid.setSpacing(8)
        self.tiles: list[FrameTile] = []
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        for i in range(FRAME_COUNT):
            tile = FrameTile()
            tile.clicked.connect(lambda _=False, i=i: self.select(i))
            self.group.addButton(tile, i)
            grid.addWidget(tile, i // COLUMNS, i % COLUMNS)
            self.tiles.append(tile)
        gl.addLayout(grid, 1)
        body.addWidget(grid_panel, 1)

        # panneau latéral
        side = QFrame(objectName="panel")
        side.setFixedWidth(340)
        sl = QVBoxLayout(side)
        sl.setContentsMargins(16, 14, 16, 16)
        sl.setSpacing(10)
        sl.addWidget(QLabel("NOUVELLE MINIATURE", objectName="section"))
        self.preview = AspectImageView("Clique sur une image\npour la choisir", radius=10)
        sl.addWidget(self.preview)
        self.preview_info = QLabel("", objectName="muted")
        sl.addWidget(self.preview_info)
        self.apply_btn = QPushButton("Appliquer comme miniature", objectName="primary")
        self.apply_btn.setToolTip("Entrée")
        self.apply_btn.clicked.connect(self.apply)
        sl.addWidget(self.apply_btn)
        self.result = QLabel("", wordWrap=True)
        sl.addWidget(self.result)

        sl.addSpacing(12)
        sl.addWidget(QLabel("MINIATURE ACTUELLE", objectName="section"))
        self.current_view = AspectImageView("Aucune miniature intégrée\n(Windows en choisit une)",
                                            radius=10)
        sl.addWidget(self.current_view)
        sl.addStretch(1)
        self.cache_btn = QPushButton("La miniature ne change pas dans l'Explorateur ?",
                                     objectName="link")
        self.cache_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.cache_btn.clicked.connect(self.clear_cache)
        sl.addWidget(self.cache_btn)
        body.addWidget(side)

        root.addLayout(body, 1)
        return page

    # ------------------------------------------------------------------ fichiers

    def browse(self):
        if self.busy:
            return
        files, _ = QFileDialog.getOpenFileNames(
            self, "Choisir des vidéos", "", "Vidéos (*.mp4 *.m4v *.mov);;Tous les fichiers (*)")
        if files:
            self.add_files(files, replace=True)

    def add_files(self, paths: list[str], replace: bool = False):
        found, rejected = [], []
        for raw in paths:
            p = Path(raw)
            if p.is_dir():
                found += sorted(str(f) for f in p.rglob("*")
                                if f.is_file() and f.suffix.lower() in core.SUPPORTED)
            elif p.is_file() and p.suffix.lower() in core.SUPPORTED:
                found.append(str(p))
            else:
                rejected.append(p.name)
        if rejected:
            QMessageBox.information(
                self, "Format non pris en charge",
                "Ces fichiers ont été ignorés (seuls MP4, M4V et MOV sont pris en charge) :\n\n"
                + "\n".join(rejected[:15]) + ("\n…" if len(rejected) > 15 else ""))
        if not found:
            return
        if replace or self.current is None:
            self.queue = found
            self.load(0)
        else:
            self.queue += [f for f in found if f not in self.queue]
            self._update_header()

    def load(self, index: int):
        self.pos = index
        self.current = self.queue[index]
        self.duration = 0.0
        self.load_gen += 1
        self.frame_gen += 1
        self.round = 0
        self._clear_selection()
        for t in self.tiles:
            t.reset()
        self.current_view.set_image(None)
        self.current_view.placeholder = "Lecture…"
        self.result.setText("")
        self._update_header()
        self.stack.setCurrentIndex(1)
        self.pool.start(LoadJob(self.current, self.load_gen, self.signals))

    def next_video(self):
        if self.busy:
            return
        if self.pos + 1 < len(self.queue):
            self.load(self.pos + 1)

    def close_all(self):
        if self.busy:
            return
        self.queue, self.pos, self.current = [], -1, None
        self.load_gen += 1
        self.frame_gen += 1
        self.stack.setCurrentIndex(0)

    def _update_header(self):
        if not self.current:
            return
        p = Path(self.current)
        self.file_title.setText(p.name)
        parts = []
        if len(self.queue) > 1:
            parts.append(f"Vidéo {self.pos + 1} / {len(self.queue)}")
        if self.duration:
            parts.append(fmt_time(self.duration))
        try:
            parts.append(f"{p.stat().st_size / 1024**2:,.0f} Mo".replace(",", " "))
        except OSError:
            pass
        parts.append(str(p.parent))
        self.file_meta.setText("  ·  ".join(parts))
        self.skip_btn.setVisible(self.pos + 1 < len(self.queue))

    # ------------------------------------------------------------------ images

    def request_frames(self, first: bool):
        if not self.current or self.stack.currentIndex() != 1 or self.busy:
            return
        self.frame_gen += 1
        self.round += 1
        self._clear_selection()
        d = self.duration
        for i, tile in enumerate(self.tiles):
            tile.reset()
            if d > 0:
                r = 0.5 if first else random.uniform(0.1, 0.9)
                t = d * (0.02 + 0.96 * (i + r) / FRAME_COUNT)
                t = min(t, max(0.0, d - 0.25))
            else:
                t = i * 2.0
            self.pool.start(FrameJob(self.current, self.frame_gen, i, t, self.signals))

    def _on_loaded(self, gen: int, duration: float, cover: QImage):
        if gen != self.load_gen:
            return
        self.duration = duration
        self.current_view.placeholder = "Aucune miniature intégrée\n(Windows en choisit une)"
        self.current_view.set_image(cover)
        self._update_header()
        self.request_frames(True)

    def _on_frame(self, gen: int, index: int, t: float, img: QImage):
        if gen == self.frame_gen:
            self.tiles[index].set_frame(img, t)

    def _on_failed(self, gen: int, index: int, msg: str):
        if index == -1 and gen == self.load_gen:
            self.current_view.placeholder = ""
            self.current_view.update()
            for t in self.tiles:
                t.set_error()
            self._show_result(False, msg)
        elif index >= 0 and gen == self.frame_gen:
            self.tiles[index].set_error()

    def _clear_selection(self):
        self.group.setExclusive(False)
        for t in self.tiles:
            t.setChecked(False)
        self.group.setExclusive(True)
        self.selected = None
        self.preview.set_image(None)
        self.preview_info.setText("")
        self.apply_btn.setEnabled(False)

    def select(self, index: int):
        tile = self.tiles[index]
        if tile.state != "ready":
            return
        self.selected = index
        tile.setChecked(True)
        img = tile.image()
        self.preview.set_image(img)
        self.preview_info.setText(f"Instant {fmt_time(tile.t)}  ·  {img.width()} × {img.height()}")
        self.apply_btn.setEnabled(not self.busy)
        self.result.setText("")

    # ------------------------------------------------------------------ application

    def apply(self):
        if self.busy or self.selected is None or not self.current or self.stack.currentIndex() != 1:
            return
        img = self.tiles[self.selected].image()
        if img is None:
            return
        self._set_busy(True)
        self.pool.start(ApplyJob(self.current, image_to_jpeg(img), self.signals))

    def _on_applied(self, path: str, ok: bool, msg: str):
        self._set_busy(False)
        if ok:
            if path == self.current and self.selected is not None:
                self.current_view.set_image(self.tiles[self.selected].image())
            self._show_result(True, f"Miniature appliquée à {Path(path).name}")
            if self.pos + 1 < len(self.queue):
                QTimer.singleShot(900, self.next_video)
        else:
            self._show_result(False, msg)

    def _set_busy(self, on: bool):
        self.busy = on
        for w in (self.apply_btn, self.more_btn, self.change_btn, self.skip_btn,
                  self.close_btn, self.cache_btn):
            w.setEnabled(not on)
        for t in self.tiles:
            t.setEnabled(not on and t.state == "ready")
        if on:
            self._dots = 0
            self.result.setObjectName("muted")
            self._restyle(self.result)
            self._tick_busy()
            self._busy_timer.start()
        else:
            self._busy_timer.stop()
            self.apply_btn.setEnabled(self.selected is not None)

    def _tick_busy(self):
        self._dots = (self._dots + 1) % 4
        self.result.setText("Écriture de la miniature" + "." * self._dots +
                            "\nQuelques secondes pour les gros fichiers.")

    def _show_result(self, ok: bool, msg: str):
        self.result.setObjectName("ok" if ok else "err")
        self._restyle(self.result)
        self.result.setText(("✓  " if ok else "✕  ") + msg)

    @staticmethod
    def _restyle(w: QWidget):
        w.style().unpolish(w)
        w.style().polish(w)

    def clear_cache(self):
        answer = QMessageBox.question(
            self, "Vider le cache des miniatures",
            "Windows garde parfois l'ancienne miniature en mémoire.\n\n"
            "Minia peut vider ce cache : l'Explorateur et la barre des tâches vont "
            "disparaître une ou deux secondes puis revenir. Les fenêtres de l'Explorateur "
            "ouvertes seront fermées.\n\nContinuer ?")
        if answer != QMessageBox.StandardButton.Yes:
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            core.clear_thumbnail_cache()
        finally:
            QApplication.restoreOverrideCursor()
        self._show_result(True, "Cache des miniatures vidé.")

    # ------------------------------------------------------------------ glisser-déposer

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls() and not self.busy:
            e.acceptProposedAction()
            self.drop.set_active(True)

    def dragLeaveEvent(self, e):
        self.drop.set_active(False)

    def dropEvent(self, e):
        self.drop.set_active(False)
        paths = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
        if paths:
            e.acceptProposedAction()
            self.add_files(paths, replace=True)

    def closeEvent(self, e):
        if self.busy:
            QMessageBox.warning(self, "Patiente un instant",
                                "Une miniature est en cours d'écriture. "
                                "Ferme Minia une fois l'opération terminée.")
            e.ignore()
            return
        self.load_gen += 1
        self.frame_gen += 1
        self.pool.clear()
        super().closeEvent(e)


# =========================================================================== démarrage

def dark_palette() -> QPalette:
    pal = QPalette()
    roles = {
        QPalette.ColorRole.Window: BG, QPalette.ColorRole.WindowText: TEXT,
        QPalette.ColorRole.Base: SURFACE, QPalette.ColorRole.AlternateBase: SURFACE_2,
        QPalette.ColorRole.Text: TEXT, QPalette.ColorRole.Button: SURFACE_2,
        QPalette.ColorRole.ButtonText: TEXT, QPalette.ColorRole.Highlight: ACCENT,
        QPalette.ColorRole.HighlightedText: "#ffffff", QPalette.ColorRole.ToolTipBase: SURFACE_2,
        QPalette.ColorRole.ToolTipText: TEXT, QPalette.ColorRole.PlaceholderText: MUTED,
        QPalette.ColorRole.Link: ACCENT_HOVER,
    }
    for role, color in roles.items():
        pal.setColor(role, QColor(color))
    pal.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor("#5b5e6a"))
    pal.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor("#5b5e6a"))
    return pal


def dark_title_bar(window: QWidget):
    if not core.IS_WINDOWS:
        return
    try:
        value = ctypes.c_int(1)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            int(window.winId()), 20, ctypes.byref(value), ctypes.sizeof(value))
    except Exception:  # noqa: BLE001
        pass


def main():
    if core.IS_WINDOWS:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Minia.ThumbnailEditor")
    app = QApplication(sys.argv)
    app.setApplicationName("Minia")
    app.setStyle("Fusion")
    try:
        app.styleHints().setColorScheme(Qt.ColorScheme.Dark)
    except AttributeError:
        pass
    app.setPalette(dark_palette())
    app.setStyleSheet(STYLE)

    window = MainWindow()
    dark_title_bar(window)
    window.show()

    try:
        core.find_tool("ffmpeg")
        core.find_tool("ffprobe")
    except core.MiniaError as exc:
        QMessageBox.critical(window, "FFmpeg manquant", str(exc))

    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if args:  # « Ouvrir avec » ou glisser sur Minia.exe
        window.add_files(args, replace=True)

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
