import os
import sys
import json
import io
import base64
import subprocess
import re
import configparser
from dataclasses import dataclass
from datetime import datetime
from urllib import request
from typing import Dict, List, Tuple

import mss
import numpy as np
from PIL import Image, ImageOps

from deep_translator import GoogleTranslator
from PySide6 import QtCore, QtGui, QtWidgets

# EasyOCR（必須）
try:
    import easyocr  # type: ignore
    EASYOCR_AVAILABLE = True
except Exception:
    EASYOCR_AVAILABLE = False


# ------------------------------
# App constants / paths
# ------------------------------
APP_VERSION = "0.6.2-ttslink-no-tesseract-dict-settings"

TICK_INTERVAL_MS = 250
MAX_TEXT_LEN = 1500
OVERLAY_BG_ALPHA = 220

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DEBUG_DIR = os.path.join(BASE_DIR, "debug")
DEBUG_LATEST_PATH = os.path.join(DEBUG_DIR, "latest.png")
DEBUG_LOG_PATH = os.path.join(DEBUG_DIR, "debug_log.txt")

# Settings / Dictionary files
SETTINGS_PATH = os.path.join(BASE_DIR, "settings.json")
NAMES_INI_PATH = os.path.join(BASE_DIR, "names.ini")

# TTS Link default
DEFAULT_TTS_ENDPOINT = "http://127.0.0.1:8765/text"


# ------------------------------
# DPI helper (Windows)
# ------------------------------
def enable_dpi_awareness_windows():
    if sys.platform != "win32":
        return
    try:
        import ctypes
        user32 = ctypes.windll.user32
        user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        try:
            import ctypes
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


# ------------------------------
# Data structures
# ------------------------------
@dataclass
class CaptureRect:
    left: int
    top: int
    width: int
    height: int

    def is_valid(self) -> bool:
        return self.width > 5 and self.height > 5


# ------------------------------
# Settings store (persist UI state)
# ------------------------------
class SettingsStore:
    def __init__(self, path: str):
        self.path = path
        self.data: dict = {}

    def load(self) -> None:
        try:
            if os.path.exists(self.path):
                with open(self.path, "r", encoding="utf-8") as f:
                    self.data = json.load(f)
            else:
                self.data = {}
        except Exception:
            self.data = {}

    def save(self) -> None:
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def get(self, key: str, default=None):
        return self.data.get(key, default)

    def set(self, key: str, value) -> None:
        self.data[key] = value


# ------------------------------
# Name Dictionary (INI)
#
# sections:
#  [gender]  name = male/female/neutral
#  [alias]   wrong = correct   (OCRゆれ補正)
#  [reading] 日本語名 = Romaji 等  (JA->EN用読み強制)
# ------------------------------
class NameDictionary:
    SECTION_GENDER = "gender"
    SECTION_ALIAS = "alias"
    SECTION_READING = "reading"

    def __init__(self, path: str):
        self.path = path
        self.gender: Dict[str, str] = {}
        self.alias: Dict[str, str] = {}
        self.reading: Dict[str, str] = {}

        self.load()

    def _make_default(self) -> None:
        cp = configparser.ConfigParser(interpolation=None)
        cp.optionxform = str  # 大文字小文字を維持（ただし内部はlower化してもよいが、表示用に保持）
        cp[self.SECTION_GENDER] = {
            "John": "male",
            "Emma": "female",
        }
        cp[self.SECTION_ALIAS] = {
            "Erma": "Emma",  # OCRゆれ例
        }
        cp[self.SECTION_READING] = {
            "帝国歌劇団": "Teikokukagekidan",
        }
        self._write_config(cp)

    def _write_config(self, cp: configparser.ConfigParser) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            cp.write(f)

    def _safe_read_config(self) -> configparser.ConfigParser:
        cp = configparser.ConfigParser(interpolation=None, delimiters=("=",))
        cp.optionxform = str

        if not os.path.exists(self.path):
            self._make_default()

        # configparser.ParsingError 対策：壊れている行を除去して読み直す
        try:
            cp.read(self.path, encoding="utf-8")
            return cp
        except configparser.ParsingError:
            # 壊れたINIを救済：section/headerは維持し、"key=value" 以外の行は捨てる
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    lines = f.readlines()
            except Exception:
                self._make_default()
                cp.read(self.path, encoding="utf-8")
                return cp

            cleaned: List[str] = []
            current_section = None
            section_re = re.compile(r"^\s*\[([^\]]+)\]\s*$")
            for line in lines:
                m = section_re.match(line)
                if m:
                    current_section = m.group(1).strip()
                    cleaned.append(line)
                    continue
                # コメント/空行
                if not line.strip() or line.lstrip().startswith(("#", ";")):
                    cleaned.append(line)
                    continue
                # section内の key=value のみ採用
                if current_section and ("=" in line):
                    cleaned.append(line)
                else:
                    # 捨てる
                    continue

            # バックアップして書き直し
            try:
                bak = self.path + ".bak"
                if not os.path.exists(bak):
                    with open(bak, "w", encoding="utf-8") as f:
                        f.writelines(lines)
                with open(self.path, "w", encoding="utf-8") as f:
                    f.writelines(cleaned)
            except Exception:
                pass

            cp = configparser.ConfigParser(interpolation=None, delimiters=("=",))
            cp.optionxform = str
            cp.read(self.path, encoding="utf-8")
            return cp

    def load(self) -> None:
        cp = self._safe_read_config()
        self.gender = {}
        self.alias = {}
        self.reading = {}

        if cp.has_section(self.SECTION_GENDER):
            for k, v in cp.items(self.SECTION_GENDER):
                self.gender[k.strip()] = (v or "").strip().lower()

        if cp.has_section(self.SECTION_ALIAS):
            for k, v in cp.items(self.SECTION_ALIAS):
                self.alias[k.strip()] = (v or "").strip()

        if cp.has_section(self.SECTION_READING):
            for k, v in cp.items(self.SECTION_READING):
                self.reading[k.strip()] = (v or "").strip()

    def save(self) -> None:
        cp = configparser.ConfigParser(interpolation=None, delimiters=("=",))
        cp.optionxform = str
        cp[self.SECTION_GENDER] = {k: v for k, v in self.gender.items()}
        cp[self.SECTION_ALIAS] = {k: v for k, v in self.alias.items()}
        cp[self.SECTION_READING] = {k: v for k, v in self.reading.items()}
        self._write_config(cp)

    # ---------- usage helpers ----------
    def correct_aliases(self, text: str) -> str:
        if not text:
            return text
        # 長いキーから先に置換（誤爆を減らす）
        for wrong, right in sorted(self.alias.items(), key=lambda x: len(x[0]), reverse=True):
            if wrong and right:
                text = text.replace(wrong, right)
        return text

    def apply_jp_reading(self, text: str) -> str:
        """JA->EN向け：日本語名をローマ字などに強制置換"""
        if not text:
            return text
        for jp, romaji in sorted(self.reading.items(), key=lambda x: len(x[0]), reverse=True):
            if jp and romaji:
                text = text.replace(jp, romaji)
        return text

    def get_gender(self, name: str) -> str:
        if not name:
            return "neutral"
        # 辞書はキーそのまま、比較はlowerでも見る
        key = name.strip()
        if key in self.gender:
            g = self.gender.get(key, "neutral")
            return g if g in ("male", "female", "neutral") else "neutral"
        # 大文字小文字違いを救済
        for k, v in self.gender.items():
            if k.strip().lower() == key.lower():
                g = v
                return g if g in ("male", "female", "neutral") else "neutral"
        return "neutral"


# ------------------------------
# Dictionary editor dialog
# ------------------------------
class DictEditorDialog(QtWidgets.QDialog):
    def __init__(self, nd: NameDictionary, parent=None):
        super().__init__(parent)
        self.setWindowTitle("辞書編集（names.ini）")
        self.resize(720, 420)
        self.nd = nd

        self.tabs = QtWidgets.QTabWidget()

        # --- Gender tab
        self.tbl_gender = QtWidgets.QTableWidget(0, 2)
        self.tbl_gender.setHorizontalHeaderLabels(["Name", "gender (male/female/neutral)"])
        self.tbl_gender.horizontalHeader().setStretchLastSection(True)

        # --- Alias tab
        self.tbl_alias = QtWidgets.QTableWidget(0, 2)
        self.tbl_alias.setHorizontalHeaderLabels(["wrong (OCR揺れ)", "correct"])
        self.tbl_alias.horizontalHeader().setStretchLastSection(True)

        # --- Reading tab
        self.tbl_reading = QtWidgets.QTableWidget(0, 2)
        self.tbl_reading.setHorizontalHeaderLabels(["日本語名", "読み（英語/ローマ字など）"])
        self.tbl_reading.horizontalHeader().setStretchLastSection(True)

        self.tabs.addTab(self._wrap_table("口調判定用（任意）", self.tbl_gender), "gender")
        self.tabs.addTab(self._wrap_table("OCRゆれ補正（任意）", self.tbl_alias), "alias")
        self.tabs.addTab(self._wrap_table("JA→EN 読み強制（重要）", self.tbl_reading), "reading")

        # Buttons
        self.btn_add = QtWidgets.QPushButton("行追加")
        self.btn_del = QtWidgets.QPushButton("行削除")
        self.btn_reload = QtWidgets.QPushButton("再読込")
        self.btn_save = QtWidgets.QPushButton("保存")
        self.btn_close = QtWidgets.QPushButton("閉じる")

        self.btn_add.clicked.connect(self.on_add)
        self.btn_del.clicked.connect(self.on_del)
        self.btn_reload.clicked.connect(self.on_reload)
        self.btn_save.clicked.connect(self.on_save)
        self.btn_close.clicked.connect(self.reject)

        row = QtWidgets.QHBoxLayout()
        row.addWidget(self.btn_add)
        row.addWidget(self.btn_del)
        row.addStretch(1)
        row.addWidget(self.btn_reload)
        row.addWidget(self.btn_save)
        row.addWidget(self.btn_close)

        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(self.tabs)
        layout.addLayout(row)

        self.populate()

    def _wrap_table(self, hint: str, table: QtWidgets.QTableWidget) -> QtWidgets.QWidget:
        w = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(w)
        v.setContentsMargins(8, 8, 8, 8)
        lab = QtWidgets.QLabel(hint)
        lab.setStyleSheet("QLabel { color: #666; }")
        v.addWidget(lab)
        v.addWidget(table, 1)
        return w

    def _current_table(self) -> QtWidgets.QTableWidget:
        idx = self.tabs.currentIndex()
        if idx == 0:
            return self.tbl_gender
        if idx == 1:
            return self.tbl_alias
        return self.tbl_reading

    def populate(self):
        self._set_rows(self.tbl_gender, [(k, v) for k, v in self.nd.gender.items()])
        self._set_rows(self.tbl_alias, [(k, v) for k, v in self.nd.alias.items()])
        self._set_rows(self.tbl_reading, [(k, v) for k, v in self.nd.reading.items()])

    def _set_rows(self, table: QtWidgets.QTableWidget, rows: List[Tuple[str, str]]):
        table.setRowCount(0)
        for a, b in rows:
            r = table.rowCount()
            table.insertRow(r)
            table.setItem(r, 0, QtWidgets.QTableWidgetItem(a))
            table.setItem(r, 1, QtWidgets.QTableWidgetItem(b))

    def on_add(self):
        table = self._current_table()
        r = table.rowCount()
        table.insertRow(r)
        table.setItem(r, 0, QtWidgets.QTableWidgetItem(""))
        table.setItem(r, 1, QtWidgets.QTableWidgetItem(""))

    def on_del(self):
        table = self._current_table()
        rows = sorted({i.row() for i in table.selectedIndexes()}, reverse=True)
        for r in rows:
            table.removeRow(r)

    def on_reload(self):
        self.nd.load()
        self.populate()

    def on_save(self):
        self.nd.gender = self._read_table(self.tbl_gender, lower_second=True)
        self.nd.alias = self._read_table(self.tbl_alias, lower_second=False)
        self.nd.reading = self._read_table(self.tbl_reading, lower_second=False)
        self.nd.save()
        self.accept()

    def _read_table(self, table: QtWidgets.QTableWidget, lower_second: bool) -> Dict[str, str]:
        d: Dict[str, str] = {}
        for r in range(table.rowCount()):
            k_item = table.item(r, 0)
            v_item = table.item(r, 1)
            k = (k_item.text() if k_item else "").strip()
            v = (v_item.text() if v_item else "").strip()
            if not k or not v:
                continue
            if lower_second:
                v = v.lower()
            d[k] = v
        return d


# ------------------------------
# Region selector
# ------------------------------
class RegionSelector(QtWidgets.QWidget):
    rectSelected = QtCore.Signal(QtCore.QRect)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Select Region")
        self.setWindowFlag(QtCore.Qt.WindowType.FramelessWindowHint, True)
        self.setWindowFlag(QtCore.Qt.WindowType.WindowStaysOnTopHint, True)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setCursor(QtCore.Qt.CursorShape.CrossCursor)

        vg = QtGui.QGuiApplication.primaryScreen().virtualGeometry()
        self.setGeometry(vg)
        self.showFullScreen()

        self._origin_local: QtCore.QPoint | None = None
        self._current_local: QtCore.QPoint | None = None

    def paintEvent(self, event: QtGui.QPaintEvent) -> None:
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QtGui.QColor(0, 0, 0, 60))

        if self._origin_local and self._current_local:
            r_local = QtCore.QRect(self._origin_local, self._current_local).normalized()
            painter.fillRect(r_local, QtGui.QColor(0, 0, 0, 0))

            pen = QtGui.QPen(QtGui.QColor(0, 180, 255, 220), 2)
            painter.setPen(pen)
            painter.drawRect(r_local)

            text = f"{r_local.width()} x {r_local.height()}"
            painter.setPen(QtGui.QColor(255, 255, 255, 240))
            painter.drawText(r_local.topLeft() + QtCore.QPoint(6, -6), text)

    def mousePressEvent(self, event: QtGui.QMouseEvent) -> None:
        if event.button() == QtCore.Qt.MouseButton.LeftButton:
            self._origin_local = event.position().toPoint()
            self._current_local = self._origin_local
            self.update()

    def mouseMoveEvent(self, event: QtGui.QMouseEvent) -> None:
        if self._origin_local:
            self._current_local = event.position().toPoint()
            self.update()

    def mouseReleaseEvent(self, event: QtGui.QMouseEvent) -> None:
        if event.button() == QtCore.Qt.MouseButton.LeftButton and self._origin_local:
            self._current_local = event.position().toPoint()
            r_local = QtCore.QRect(self._origin_local, self._current_local).normalized()

            tl_global = self.mapToGlobal(r_local.topLeft())
            br_global = self.mapToGlobal(r_local.bottomRight())
            r_global = QtCore.QRect(tl_global, br_global).normalized()

            self._origin_local = None
            self._current_local = None
            self.update()

            self.rectSelected.emit(r_global)
            self.close()

    def keyPressEvent(self, event: QtGui.QKeyEvent) -> None:
        if event.key() == QtCore.Qt.Key.Key_Escape:
            self.close()


# ------------------------------
# Overlay window
# ------------------------------
class OverlayWindow(QtWidgets.QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Translation Overlay")
        self.setWindowFlag(QtCore.Qt.WindowType.FramelessWindowHint, True)
        self.setWindowFlag(QtCore.Qt.WindowType.WindowStaysOnTopHint, True)
        self.setWindowFlag(QtCore.Qt.WindowType.Tool, True)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TranslucentBackground, True)

        self._dragging = False
        self._drag_offset = QtCore.QPoint(0, 0)
        self._collapsed = False
        self._expanded_size = QtCore.QSize(520, 240)

        self.title = QtWidgets.QLabel("Translation")
        self.title.setStyleSheet("QLabel { color: white; font-weight: 600; }")

        self.btn_collapse = QtWidgets.QToolButton()
        self.btn_collapse.setText("＿")
        self.btn_collapse.setToolTip("折りたたみ / 展開")

        self.btn_hide = QtWidgets.QToolButton()
        self.btn_hide.setText("×")
        self.btn_hide.setToolTip("隠す（非表示）")

        self.btn_collapse.clicked.connect(self.toggle_collapse)
        self.btn_hide.clicked.connect(self.hide_overlay)

        bar = QtWidgets.QHBoxLayout()
        bar.setContentsMargins(10, 8, 10, 0)
        bar.addWidget(self.title)
        bar.addStretch(1)
        bar.addWidget(self.btn_collapse)
        bar.addWidget(self.btn_hide)

        self._label = QtWidgets.QLabel("")
        self._label.setWordWrap(True)
        self._label.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)

        self.panel = QtWidgets.QFrame()
        self.panel.setStyleSheet(f"""
            QFrame {{
                background: rgba(0, 0, 0, {OVERLAY_BG_ALPHA});
                border: 1px solid rgba(255, 255, 255, 100);
                border-radius: 10px;
            }}
            QLabel {{
                background: transparent;
                color: white;
                font-size: 16px;
            }}
            QToolButton {{
                background: rgba(255,255,255,30);
                color: white;
                border: 1px solid rgba(255,255,255,60);
                border-radius: 6px;
                padding: 2px 6px;
            }}
            QToolButton:hover {{
                background: rgba(255,255,255,60);
            }}
        """)

        panel_layout = QtWidgets.QVBoxLayout(self.panel)
        panel_layout.setContentsMargins(0, 0, 0, 10)
        panel_layout.addLayout(bar)
        panel_layout.addWidget(self._label, 1)
        panel_layout.setSpacing(6)

        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self.panel)

        self.resize(self._expanded_size)
        self._label.setText("（翻訳結果がここに表示されます）")

    def set_text(self, text: str):
        if not self._collapsed:
            self._label.setText(text if text.strip() else "（文字が検出されません）")

    def set_text_cache(self, text: str):
        self._label.setText(text if text.strip() else "（文字が検出されません）")

    def toggle_collapse(self):
        if not self._collapsed:
            self._expanded_size = self.size()
            self._label.setVisible(False)
            self._collapsed = True
            self.btn_collapse.setText("▢")
            self.resize(self._expanded_size.width(), 44)
        else:
            self._label.setVisible(True)
            self._collapsed = False
            self.btn_collapse.setText("＿")
            self.resize(self._expanded_size)

    def hide_overlay(self):
        self.hide()

    def mousePressEvent(self, event: QtGui.QMouseEvent) -> None:
        if event.button() == QtCore.Qt.MouseButton.LeftButton:
            self._dragging = True
            self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event: QtGui.QMouseEvent) -> None:
        if self._dragging:
            self.move(event.globalPosition().toPoint() - self._drag_offset)
            event.accept()

    def mouseReleaseEvent(self, event: QtGui.QMouseEvent) -> None:
        if event.button() == QtCore.Qt.MouseButton.LeftButton:
            self._dragging = False
            event.accept()


# ------------------------------
# Ollama helpers
# ------------------------------
def _ollama_call(model: str, base_url: str, system: str, prompt: str, temperature: float,
                 images_b64: list[str] | None = None, timeout_sec: int = 75) -> str:
    payload = {
        "model": model,
        "prompt": prompt,
        "system": system,
        "stream": False,
        "options": {
            "temperature": float(temperature),
            "top_p": 0.9,
            "num_predict": 280,
        },
    }
    if images_b64:
        payload["images"] = images_b64

    url = base_url.rstrip("/") + "/api/generate"
    data = json.dumps(payload).encode("utf-8")
    req = request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")

    with request.urlopen(req, timeout=timeout_sec) as resp:
        body = resp.read().decode("utf-8", errors="replace")
    obj = json.loads(body)
    return (obj.get("response") or "").strip()


def ollama_fix_ocr_text(text: str, lang: str, model: str, base_url: str, timeout_sec: int = 35) -> str:
    if lang == "en":
        system = (
            "You are an OCR post-processor for English game subtitles.\n"
            "Fix OCR errors while preserving meaning. Do NOT add new information.\n"
            "Output ONLY the corrected English text."
        )
    else:
        system = (
            "You are an OCR post-processor for Japanese text.\n"
            "Fix OCR noise while preserving meaning. Do NOT add new information.\n"
            "Output ONLY the corrected Japanese text."
        )
    prompt = f"OCR_TEXT:\n{text}"
    return _ollama_call(model=model, base_url=base_url, system=system, prompt=prompt,
                       temperature=0.05, timeout_sec=timeout_sec)


def _img_to_b64_png(img: Image.Image) -> str:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("utf-8")


def ollama_vision_ocr(img: Image.Image, lang: str, model: str, base_url: str, pixel_hint: bool,
                     timeout_sec: int = 75) -> str:
    work = img.convert("RGB")
    if pixel_hint:
        g = work.convert("L")
        g = g.resize((g.width * 3, g.height * 3), resample=Image.Resampling.NEAREST)
        g = ImageOps.autocontrast(g)
        work = g.convert("RGB")

    max_side = max(work.width, work.height)
    if max_side > 1024:
        scale = 1024 / max_side
        work = work.resize((int(work.width * scale), int(work.height * scale)),
                           resample=Image.Resampling.BILINEAR)

    img_b64 = _img_to_b64_png(work)

    system = "You are a strict OCR engine. Follow the rules exactly."
    prompt = (
        "OCR TASK:\n"
        "Extract the EXACT text from the image.\n"
        "Do NOT translate.\n"
        "Do NOT add commentary.\n"
        "Preserve punctuation.\n"
        "If multiple lines, separate with newlines.\n"
        "Output ONLY the extracted text."
        f"\n\n(language={lang})"
    )

    return _ollama_call(
        model=model,
        base_url=base_url,
        system=system,
        prompt=prompt,
        temperature=0.0,
        images_b64=[img_b64],
        timeout_sec=timeout_sec,
    )


# ------------------------------
# Tone helpers (existing)
# ------------------------------
_MALE_NAMES = {
    "john", "michael", "david", "james", "robert", "william", "thomas", "daniel",
    "mark", "paul", "kevin", "brian", "george", "peter", "jack", "alexander",
    "ryan", "jason", "matt", "matthew", "chris", "christopher", "andrew",
}
_FEMALE_NAMES = {
    "mary", "linda", "patricia", "jennifer", "elizabeth", "susan", "jessica", "sarah",
    "emily", "anna", "amy", "laura", "victoria", "kate", "katie", "olivia",
    "emma", "chloe", "lily", "grace", "mia",
}


def extract_leading_name(text: str) -> str | None:
    if not text:
        return None
    first_line = text.strip().splitlines()[0].strip()
    m = re.match(r"^([A-Za-z][A-Za-z0-9_]{1,20})\s*[:\-—]\s+.+", first_line)
    if not m:
        return None
    return m.group(1).strip()


def guess_gender_from_name(name: str) -> str:
    if not name:
        return "neutral"
    key = name.strip().lower()
    if key in _MALE_NAMES:
        return "male"
    if key in _FEMALE_NAMES:
        return "female"
    if key.endswith(("a", "ia", "na", "elle", "ine", "y")):
        return "female"
    if key.endswith(("o", "us", "er", "son", "man")):
        return "male"
    return "neutral"


def japanese_tone_instruction(tone: str) -> str:
    if tone == "male":
        return (
            "Use a natural casual masculine tone in Japanese, but do not overdo role language.\n"
            "Avoid archaic or exaggerated speech. Do not add new information."
        )
    if tone == "female":
        return (
            "Use a natural casual feminine tone in Japanese, but do not overdo role language.\n"
            "Avoid stereotypical or exaggerated speech. Do not add new information."
        )
    return (
        "Use a natural neutral Japanese tone suitable for subtitles.\n"
        "Do not add new information."
    )


# ------------------------------
# TTS sender
# ------------------------------
def http_post_text(endpoint: str, text: str, timeout_sec: float = 0.25) -> None:
    payload = json.dumps({"text": text}).encode("utf-8")
    req = request.Request(
        endpoint,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with request.urlopen(req, timeout=timeout_sec) as resp:
        resp.read()


# ------------------------------
# Main Window
# ------------------------------
class MainWindow(QtWidgets.QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"OCR → Translate Overlay v{APP_VERSION}")
        self.setMinimumWidth(820)
        self.resize(900, 680)

        # Persisted settings
        self.settings = SettingsStore(SETTINGS_PATH)
        self.settings.load()

        # Dictionary
        self.name_dict = NameDictionary(NAMES_INI_PATH)

        # Buttons
        self.btn_pick = QtWidgets.QPushButton("範囲選択")
        self.btn_start = QtWidgets.QPushButton("開始")
        self.btn_stop = QtWidgets.QPushButton("停止")
        self.btn_stop.setEnabled(False)

        self.btn_overlay_show = QtWidgets.QPushButton("翻訳ウィンドウ表示")
        self.btn_overlay_show.clicked.connect(self.show_overlay)

        self.btn_exit = QtWidgets.QPushButton("終了（Ollama解放）")
        self.btn_exit.clicked.connect(self.exit_with_ollama_cleanup)

        self.chk_debug = QtWidgets.QCheckBox("debug保存")
        self.chk_debug.setChecked(bool(self.settings.get("debug_enabled", True)))

        self.chk_kill_ollama = QtWidgets.QCheckBox("終了時にOllamaプロセスも停止（他アプリに影響）")
        self.chk_kill_ollama.setChecked(bool(self.settings.get("kill_ollama", False)))

        # Translation options
        self.tr_mode = QtWidgets.QComboBox()
        self.tr_mode.addItem("EN→JA", ("en", "ja"))
        self.tr_mode.addItem("JA→EN", ("ja", "en"))

        self.tr_engine = QtWidgets.QComboBox()
        self.tr_engine.addItem("Google", "google")
        self.tr_engine.addItem("Ollama", "ollama")

        self.tr_style = QtWidgets.QComboBox()
        self.tr_style.addItem("一般", "general")
        self.tr_style.addItem("ゲーム", "game")

        self.ollama_url = QtWidgets.QLineEdit(self.settings.get("ollama_url", "http://127.0.0.1:11434"))
        self.ollama_model = QtWidgets.QLineEdit(self.settings.get("ollama_model", "gemma3:4b"))

        self.chk_ocr_fix = QtWidgets.QCheckBox("OCR後補正(LLM)")
        self.chk_ocr_fix.setChecked(bool(self.settings.get("ocr_fix_enabled", True)))

        self.chk_llm_ocr_pixel_hint = QtWidgets.QCheckBox("LLM OCR ドット補正")
        self.chk_llm_ocr_pixel_hint.setChecked(bool(self.settings.get("llm_ocr_pixel_hint", True)))

        self.sp_llm_ocr_ms = QtWidgets.QSpinBox()
        self.sp_llm_ocr_ms.setRange(500, 20000)
        self.sp_llm_ocr_ms.setValue(int(self.settings.get("llm_ocr_interval_ms", 2500)))
        self.sp_llm_ocr_ms.setSuffix("ms")

        self.sp_translate_ms = QtWidgets.QSpinBox()
        self.sp_translate_ms.setRange(500, 20000)
        self.sp_translate_ms.setValue(int(self.settings.get("translate_interval_ms", 3500)))
        self.sp_translate_ms.setSuffix("ms")

        self.sp_stable_required = QtWidgets.QSpinBox()
        self.sp_stable_required.setRange(0, 10)
        self.sp_stable_required.setValue(int(self.settings.get("stable_required", 2)))

        # OCR mode（EasyOCRデフォルト）
        self.ocr_mode = QtWidgets.QComboBox()
        if EASYOCR_AVAILABLE:
            self.ocr_mode.addItem("EasyOCR（推奨）", "easyocr")
        else:
            self.ocr_mode.addItem("EasyOCR(未インストール)", "easyocr_unavailable")
            self.ocr_mode.model().item(0).setEnabled(False)
        self.ocr_mode.addItem("LLM OCR（Ollama Vision）", "llm_vision_ocr")

        saved_ocr_mode = self.settings.get("ocr_mode", "easyocr")
        self._set_combo_by_data(self.ocr_mode, saved_ocr_mode, fallback_index=0)

        # Tone option
        self.chk_tone = QtWidgets.QCheckBox("口調を調整（推測）※EN→JA & Ollamaのみ")
        self.chk_tone.setChecked(bool(self.settings.get("tone_enabled", False)))

        self.cmb_tone = QtWidgets.QComboBox()
        self.cmb_tone.addItem("自動", "auto")
        self.cmb_tone.addItem("男性口調", "male")
        self.cmb_tone.addItem("女性口調", "female")
        self.cmb_tone.addItem("中立", "neutral")
        self._set_combo_by_data(self.cmb_tone, self.settings.get("tone_mode", "auto"), fallback_index=0)
        self.cmb_tone.setEnabled(self.chk_tone.isChecked())
        self.chk_tone.toggled.connect(lambda on: self.cmb_tone.setEnabled(bool(on)))

        # -------- Dictionary UI --------
        self.grp_dict = QtWidgets.QGroupBox("辞書（names.ini）")
        self.grp_dict.setCheckable(True)
        self.grp_dict.setChecked(bool(self.settings.get("dict_enabled", True)))

        self.chk_dict_alias = QtWidgets.QCheckBox("OCRゆれ補正（alias）を使う")
        self.chk_dict_alias.setChecked(bool(self.settings.get("dict_alias_enabled", True)))

        self.chk_dict_reading = QtWidgets.QCheckBox("JA→EN で読みを強制（reading）")
        self.chk_dict_reading.setChecked(bool(self.settings.get("dict_reading_enabled", True)))

        self.btn_dict_edit = QtWidgets.QPushButton("辞書編集…")
        self.btn_dict_edit.clicked.connect(self.open_dict_editor)

        self.btn_dict_reload = QtWidgets.QPushButton("再読込")
        self.btn_dict_reload.clicked.connect(self.reload_dictionary)

        dict_row = QtWidgets.QHBoxLayout()
        dict_row.addWidget(self.chk_dict_alias)
        dict_row.addWidget(self.chk_dict_reading)
        dict_row.addStretch(1)
        dict_row.addWidget(self.btn_dict_reload)
        dict_row.addWidget(self.btn_dict_edit)

        dict_layout = QtWidgets.QVBoxLayout(self.grp_dict)
        dict_layout.addLayout(dict_row)

        # -------- TTS Link UI --------
        self.grp_tts = QtWidgets.QGroupBox("TTS連携（別アプリへ送信）")
        self.grp_tts.setCheckable(True)
        self.grp_tts.setChecked(bool(self.settings.get("tts_enabled", False)))

        self.tts_endpoint = QtWidgets.QLineEdit(self.settings.get("tts_endpoint", DEFAULT_TTS_ENDPOINT))
        self.tts_send_mode = QtWidgets.QComboBox()
        self.tts_send_mode.addItem("翻訳結果を送る", "translated")
        self.tts_send_mode.addItem("OCR原文を送る", "ocr")
        self.tts_send_mode.addItem("補正後（翻訳入力）を送る", "fixed")
        self._set_combo_by_data(self.tts_send_mode, self.settings.get("tts_send_mode", "translated"), fallback_index=0)

        self.sp_tts_send_ms = QtWidgets.QSpinBox()
        self.sp_tts_send_ms.setRange(200, 20000)
        self.sp_tts_send_ms.setValue(int(self.settings.get("tts_send_interval_ms", 2500)))
        self.sp_tts_send_ms.setSuffix("ms")

        self.chk_tts_only_when_changed = QtWidgets.QCheckBox("同じ内容は送らない")
        self.chk_tts_only_when_changed.setChecked(bool(self.settings.get("tts_only_when_changed", True)))

        self.btn_tts_test = QtWidgets.QPushButton("テスト送信")
        self.btn_tts_test.clicked.connect(self.on_tts_test_send)

        tts_form = QtWidgets.QFormLayout()
        tts_form.addRow("送信先URL", self.tts_endpoint)
        tts_form.addRow("送信対象", self.tts_send_mode)
        tts_form.addRow("送信間隔", self.sp_tts_send_ms)

        tts_row = QtWidgets.QHBoxLayout()
        tts_row.addWidget(self.chk_tts_only_when_changed)
        tts_row.addStretch(1)
        tts_row.addWidget(self.btn_tts_test)

        tts_layout = QtWidgets.QVBoxLayout(self.grp_tts)
        tts_layout.addLayout(tts_form)
        tts_layout.addLayout(tts_row)

        # Status
        self.rect_label = QtWidgets.QLabel("未選択")
        self.status = QtWidgets.QLabel("待機中")

        # Text panels
        self.grp_text = QtWidgets.QGroupBox("OCRテキスト（開閉できます）")
        self.grp_text.setCheckable(True)
        self.grp_text.setChecked(bool(self.settings.get("text_panel_open", True)))

        self.raw_text = QtWidgets.QPlainTextEdit()
        self.raw_text.setReadOnly(True)
        self.raw_text.setMinimumHeight(90)

        self.fixed_text = QtWidgets.QPlainTextEdit()
        self.fixed_text.setReadOnly(True)
        self.fixed_text.setMinimumHeight(90)

        text_split = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)
        left = QtWidgets.QWidget()
        lyt = QtWidgets.QVBoxLayout(left)
        lyt.setContentsMargins(0, 0, 0, 0)
        lyt.addWidget(QtWidgets.QLabel("OCR原文"))
        lyt.addWidget(self.raw_text)

        right = QtWidgets.QWidget()
        ryt = QtWidgets.QVBoxLayout(right)
        ryt.setContentsMargins(0, 0, 0, 0)
        ryt.addWidget(QtWidgets.QLabel("補正後（翻訳入力）"))
        ryt.addWidget(self.fixed_text)

        text_split.addWidget(left)
        text_split.addWidget(right)
        text_split.setStretchFactor(0, 1)
        text_split.setStretchFactor(1, 1)

        grp_layout = QtWidgets.QVBoxLayout(self.grp_text)
        grp_layout.addWidget(text_split)

        # Layout grid
        grid = QtWidgets.QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(6)

        r = 0
        grid.addWidget(QtWidgets.QLabel("翻訳"), r, 0)
        grid.addWidget(self.tr_mode, r, 1)
        grid.addWidget(QtWidgets.QLabel("エンジン"), r, 2)
        grid.addWidget(self.tr_engine, r, 3)
        r += 1

        grid.addWidget(QtWidgets.QLabel("スタイル"), r, 0)
        grid.addWidget(self.tr_style, r, 1)
        grid.addWidget(QtWidgets.QLabel("OCR"), r, 2)
        grid.addWidget(self.ocr_mode, r, 3)
        r += 1

        grid.addWidget(self.chk_ocr_fix, r, 0, 1, 2)
        grid.addWidget(self.chk_llm_ocr_pixel_hint, r, 2, 1, 2)
        r += 1

        grid.addWidget(QtWidgets.QLabel("LLM OCR間隔"), r, 0)
        grid.addWidget(self.sp_llm_ocr_ms, r, 1)
        grid.addWidget(QtWidgets.QLabel("翻訳間隔"), r, 2)
        grid.addWidget(self.sp_translate_ms, r, 3)
        r += 1

        grid.addWidget(QtWidgets.QLabel("安定回数"), r, 0)
        grid.addWidget(self.sp_stable_required, r, 1)
        grid.addWidget(self.chk_debug, r, 2)
        grid.addWidget(self.btn_overlay_show, r, 3)
        r += 1

        grid.addWidget(self.chk_tone, r, 0, 1, 3)
        grid.addWidget(self.cmb_tone, r, 3)
        r += 1

        grid.addWidget(self.chk_kill_ollama, r, 0, 1, 4)
        r += 1

        grid.addWidget(QtWidgets.QLabel("Ollama URL"), r, 0)
        grid.addWidget(self.ollama_url, r, 1, 1, 3)
        r += 1

        grid.addWidget(QtWidgets.QLabel("Model"), r, 0)
        grid.addWidget(self.ollama_model, r, 1, 1, 3)
        r += 1

        grid.addWidget(QtWidgets.QLabel("範囲"), r, 0)
        grid.addWidget(self.rect_label, r, 1, 1, 3)

        btn_row = QtWidgets.QHBoxLayout()
        btn_row.addWidget(self.btn_pick)
        btn_row.addStretch(1)
        btn_row.addWidget(self.btn_start)
        btn_row.addWidget(self.btn_stop)
        btn_row.addWidget(self.btn_exit)

        layout = QtWidgets.QVBoxLayout(self)
        layout.addLayout(grid)
        layout.addLayout(btn_row)
        layout.addWidget(self.grp_dict)   # 辞書
        layout.addWidget(self.grp_tts)    # TTS
        layout.addWidget(QtWidgets.QLabel("ステータス"))
        layout.addWidget(self.status)
        layout.addWidget(self.grp_text)

        # Overlay
        self.overlay = OverlayWindow()
        self.overlay.show()

        # Timer
        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(TICK_INTERVAL_MS)
        self.timer.timeout.connect(self.tick)

        # Runtime state
        self._busy = False
        self._last_ocr = ""
        self._last_fixed = ""
        self._last_translated_source = ""
        self._stable_count = 0
        self._last_llm_ocr_at = datetime.min
        self._last_translate_at = datetime.min
        self._last_overlay_text = ""
        self._easy_readers = {}

        # TTS send state
        self._last_tts_sent_text = ""
        self._last_tts_sent_at = datetime.min

        # Selection
        self.selector: RegionSelector | None = None
        self.selected_qt_global: QtCore.QRect | None = None
        self.mss_virtual = None

        # events
        self.btn_pick.clicked.connect(self.pick_region)
        self.btn_start.clicked.connect(self.start)
        self.btn_stop.clicked.connect(self.stop)

        self.grp_text.toggled.connect(self.on_group_toggled)
        self.on_group_toggled(self.grp_text.isChecked())

        # restore translate mode/engine/style
        self._restore_combo_states()

        if not EASYOCR_AVAILABLE:
            self.status.setText("EasyOCRが未インストールです。pip install easyocr してください。")

    # ---------- UI helpers ----------
    def _set_combo_by_data(self, combo: QtWidgets.QComboBox, data_value, fallback_index: int = 0):
        for i in range(combo.count()):
            if combo.itemData(i) == data_value:
                combo.setCurrentIndex(i)
                return
        combo.setCurrentIndex(fallback_index)

    def _restore_combo_states(self):
        # tr_mode
        saved = self.settings.get("tr_mode", ("en", "ja"))
        if isinstance(saved, list):
            saved = tuple(saved)
        for i in range(self.tr_mode.count()):
            if self.tr_mode.itemData(i) == saved:
                self.tr_mode.setCurrentIndex(i)
                break

        self._set_combo_by_data(self.tr_engine, self.settings.get("tr_engine", "google"), 0)
        self._set_combo_by_data(self.tr_style, self.settings.get("tr_style", "general"), 0)

    def _save_settings(self):
        self.settings.set("debug_enabled", self.chk_debug.isChecked())
        self.settings.set("kill_ollama", self.chk_kill_ollama.isChecked())
        self.settings.set("ocr_fix_enabled", self.chk_ocr_fix.isChecked())
        self.settings.set("llm_ocr_pixel_hint", self.chk_llm_ocr_pixel_hint.isChecked())
        self.settings.set("llm_ocr_interval_ms", int(self.sp_llm_ocr_ms.value()))
        self.settings.set("translate_interval_ms", int(self.sp_translate_ms.value()))
        self.settings.set("stable_required", int(self.sp_stable_required.value()))
        self.settings.set("ocr_mode", self.ocr_mode.currentData())

        self.settings.set("tone_enabled", self.chk_tone.isChecked())
        self.settings.set("tone_mode", self.cmb_tone.currentData())

        self.settings.set("ollama_url", self.ollama_url.text().strip())
        self.settings.set("ollama_model", self.ollama_model.text().strip())  # ← 前回のモデル名保持

        self.settings.set("tr_mode", list(self.tr_mode.currentData()))
        self.settings.set("tr_engine", self.tr_engine.currentData())
        self.settings.set("tr_style", self.tr_style.currentData())

        self.settings.set("dict_enabled", self.grp_dict.isChecked())
        self.settings.set("dict_alias_enabled", self.chk_dict_alias.isChecked())
        self.settings.set("dict_reading_enabled", self.chk_dict_reading.isChecked())

        self.settings.set("tts_enabled", self.grp_tts.isChecked())
        self.settings.set("tts_endpoint", self.tts_endpoint.text().strip())
        self.settings.set("tts_send_mode", self.tts_send_mode.currentData())
        self.settings.set("tts_send_interval_ms", int(self.sp_tts_send_ms.value()))
        self.settings.set("tts_only_when_changed", self.chk_tts_only_when_changed.isChecked())

        self.settings.set("text_panel_open", self.grp_text.isChecked())
        self.settings.save()

    # ---------- close ----------
    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        try:
            self._save_settings()
        except Exception:
            pass
        try:
            self.exit_with_ollama_cleanup(quit_app=False)
        except Exception:
            pass
        event.accept()

    # ---------- dictionary ----------
    def open_dict_editor(self):
        dlg = DictEditorDialog(self.name_dict, self)
        if dlg.exec() == QtWidgets.QDialog.DialogCode.Accepted:
            # 保存はダイアログ内で済んでいるので再読み込みだけ
            self.reload_dictionary()

    def reload_dictionary(self):
        try:
            self.name_dict.load()
            self.status.setText("辞書を再読込しました。")
        except Exception as e:
            self.status.setText(f"辞書の再読込に失敗: {e}")

    def apply_dictionary_pipeline(self, text: str, src: str, tgt: str) -> str:
        """
        辞書適用の流れ：
          1) alias（OCRゆれ補正）
          2) reading（JA->EN時の日本語名読み強制）
        """
        if not text:
            return text
        if not self.grp_dict.isChecked():
            return text

        out = text
        if self.chk_dict_alias.isChecked():
            out = self.name_dict.correct_aliases(out)

        if self.chk_dict_reading.isChecked() and (src == "ja" and tgt == "en"):
            out = self.name_dict.apply_jp_reading(out)

        return out

    # ---------- group toggle ----------
    def on_group_toggled(self, checked: bool):
        self.grp_text.setVisible(checked)
        self.adjustSize()

    # ---------- overlay show ----------
    def show_overlay(self):
        self.overlay.show()
        self.overlay.raise_()
        self.overlay.activateWindow()
        self.overlay.set_text_cache(self._last_overlay_text or "（翻訳結果がここに表示されます）")

    # ---------- debug ----------
    def ensure_debug_dir(self):
        if self.chk_debug.isChecked():
            os.makedirs(DEBUG_DIR, exist_ok=True)

    def log_debug(self, msg: str):
        if not self.chk_debug.isChecked():
            return
        self.ensure_debug_dir()
        line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n"
        try:
            with open(DEBUG_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(line)
        except Exception:
            pass

    # ---------- Ollama cleanup ----------
    def unload_ollama_model(self):
        model = self.ollama_model.text().strip()
        if not model:
            return
        try:
            r = subprocess.run(["ollama", "stop", model], capture_output=True, text=True, timeout=12)
            self.log_debug(f"ollama stop {model} rc={r.returncode} out={r.stdout.strip()} err={r.stderr.strip()}")
        except FileNotFoundError:
            self.log_debug("ollama command not found. PATHにollamaが必要です。")
        except Exception as e:
            self.log_debug(f"ollama stop failed: {e}")

    def kill_ollama_process(self):
        if sys.platform != "win32":
            return
        try:
            r = subprocess.run(["taskkill", "/IM", "ollama.exe", "/F"], capture_output=True, text=True, timeout=12)
            self.log_debug(f"taskkill ollama.exe rc={r.returncode} out={r.stdout.strip()} err={r.stderr.strip()}")
        except Exception as e:
            self.log_debug(f"taskkill failed: {e}")

    def exit_with_ollama_cleanup(self, quit_app: bool = True):
        try:
            self.stop()
        except Exception:
            pass

        self.status.setText("終了処理中…（Ollama解放）")
        QtWidgets.QApplication.processEvents()
        self.unload_ollama_model()

        if self.chk_kill_ollama.isChecked():
            self.kill_ollama_process()

        if quit_app:
            QtWidgets.QApplication.quit()

    # ---------- region ----------
    def pick_region(self):
        self.status.setText("範囲選択中（ドラッグ / ESCでキャンセル）")
        self.selector = RegionSelector()
        self.selector.rectSelected.connect(self.on_region_selected)
        self.selector.destroyed.connect(lambda: setattr(self, "selector", None))
        self.selector.show()

    def on_region_selected(self, r_qt_global: QtCore.QRect):
        self.selected_qt_global = r_qt_global
        self.rect_label.setText(
            f"({r_qt_global.left()},{r_qt_global.top()}) {r_qt_global.width()}x{r_qt_global.height()}"
        )
        self.status.setText("範囲が選択されました。開始できます。")

    # ---------- start/stop ----------
    def start(self):
        if not EASYOCR_AVAILABLE:
            self.status.setText("EasyOCRが未インストールです。pip install easyocr してください。")
            return

        if not self.selected_qt_global or self.selected_qt_global.width() < 6 or self.selected_qt_global.height() < 6:
            self.status.setText("処理範囲が未選択です。")
            return

        with mss.mss() as sct:
            self.mss_virtual = sct.monitors[0]

        self.ensure_debug_dir()

        self._last_ocr = ""
        self._last_fixed = ""
        self._last_translated_source = ""
        self._stable_count = 0
        self._last_llm_ocr_at = datetime.min
        self._last_translate_at = datetime.min
        self._last_overlay_text = ""

        self._last_tts_sent_text = ""
        self._last_tts_sent_at = datetime.min

        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.btn_pick.setEnabled(False)

        self.timer.start()
        self.status.setText(f"実行中… v{APP_VERSION}")

        # 保存（開始時点でのUI値を残す）
        self._save_settings()

    def stop(self):
        self.timer.stop()
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.btn_pick.setEnabled(True)
        self.status.setText("停止しました。")

        # 保存
        self._save_settings()

    # ---------- tts test ----------
    def on_tts_test_send(self):
        text = self._last_overlay_text or "テスト送信です"
        self.try_send_tts(text, force=True)

    # ---------- translate & OCR ----------
    def _ollama_ready(self) -> tuple[str, str]:
        base_url = self.ollama_url.text().strip() or "http://127.0.0.1:11434"
        model = self.ollama_model.text().strip() or "gemma3:4b"
        return base_url, model

    def get_google_translator(self):
        src, tgt = self.tr_mode.currentData()
        return GoogleTranslator(source=src, target=tgt)

    def _tone_for_current_text(self, text: str) -> str:
        mode_src, mode_tgt = self.tr_mode.currentData()
        if not (mode_src == "en" and mode_tgt == "ja"):
            return "neutral"
        if not self.chk_tone.isChecked():
            return "neutral"

        sel = self.cmb_tone.currentData()
        if sel in ("male", "female", "neutral"):
            return sel

        # 自動：まず辞書(gender)を試し、なければ従来推測
        name = extract_leading_name(text)
        if not name:
            return "neutral"

        # aliasも適用した上で判定（Erma→Emma など）
        if self.grp_dict.isChecked() and self.chk_dict_alias.isChecked():
            name = self.name_dict.correct_aliases(name)

        g = self.name_dict.get_gender(name)
        if g in ("male", "female", "neutral") and g != "neutral":
            return g

        return guess_gender_from_name(name)

    def translate_text(self, text: str) -> str:
        src, tgt = self.tr_mode.currentData()
        engine = self.tr_engine.currentData()
        style = self.tr_style.currentData()

        # 翻訳入力に辞書適用（重要）
        text = self.apply_dictionary_pipeline(text, src, tgt)

        if engine == "google":
            return self.get_google_translator().translate(text)

        base_url, model = self._ollama_ready()

        tone = self._tone_for_current_text(text)
        tone_hint = japanese_tone_instruction(tone)
        temp = 0.25 if style == "game" else 0.10

        system = (
            "You are a translator for on-screen subtitles.\n"
            "Output ONLY the translation. Do not add commentary.\n"
            "Preserve meaning accurately.\n"
            + tone_hint
        )
        prompt = f"Translate from {src} to {tgt}.\n\nTEXT:\n{text}"
        return _ollama_call(model=model, base_url=base_url, system=system, prompt=prompt,
                           temperature=temp, timeout_sec=75)

    def get_easy_reader(self, src_lang: str):
        if src_lang not in self._easy_readers:
            self.status.setText("EasyOCR初期化中…")
            QtWidgets.QApplication.processEvents()
            self._easy_readers[src_lang] = easyocr.Reader([src_lang], gpu=False)
        return self._easy_readers[src_lang]

    def qt_rect_to_mss_rect(self, r_qt_global: QtCore.QRect) -> CaptureRect:
        assert self.mss_virtual is not None
        qt_vg = QtGui.QGuiApplication.primaryScreen().virtualGeometry()
        mv = self.mss_virtual

        scale_x = mv["width"] / qt_vg.width()
        scale_y = mv["height"] / qt_vg.height()

        left = int((r_qt_global.left() - qt_vg.left()) * scale_x + mv["left"])
        top = int((r_qt_global.top() - qt_vg.top()) * scale_y + mv["top"])
        width = int(r_qt_global.width() * scale_x)
        height = int(r_qt_global.height() * scale_y)
        return CaptureRect(left, top, width, height)

    @staticmethod
    def capture_region(rect: CaptureRect) -> Image.Image:
        with mss.mss() as sct:
            monitor = {"left": rect.left, "top": rect.top, "width": rect.width, "height": rect.height}
            shot = sct.grab(monitor)
            return Image.frombytes("RGB", shot.size, shot.rgb)

    def _ms_since(self, t: datetime) -> int:
        return int((datetime.now() - t).total_seconds() * 1000)

    # ---------- TTS sending ----------
    def get_tts_send_text(self, ocr: str, fixed: str, translated: str) -> str:
        mode = self.tts_send_mode.currentData()
        if mode == "ocr":
            return ocr
        if mode == "fixed":
            return fixed
        return translated

    def try_send_tts(self, text: str, force: bool = False):
        if not self.grp_tts.isChecked():
            return
        endpoint = self.tts_endpoint.text().strip()
        if not endpoint:
            return

        interval_ms = int(self.sp_tts_send_ms.value())
        if not force and self._ms_since(self._last_tts_sent_at) < interval_ms:
            return

        if self.chk_tts_only_when_changed.isChecked() and (not force) and text == self._last_tts_sent_text:
            return

        try:
            http_post_text(endpoint, text, timeout_sec=0.25)
            self._last_tts_sent_text = text
            self._last_tts_sent_at = datetime.now()
        except Exception:
            self.log_debug(f"TTS send failed to {endpoint}")

    # ---------- tick loop ----------
    def tick(self):
        if self._busy:
            return
        self._busy = True
        try:
            if not self.selected_qt_global or not self.mss_virtual:
                return

            cap = self.qt_rect_to_mss_rect(self.selected_qt_global)
            if not cap.is_valid():
                return

            img = self.capture_region(cap)

            if self.chk_debug.isChecked():
                self.ensure_debug_dir()
                try:
                    img.save(DEBUG_LATEST_PATH)
                except Exception:
                    pass

            src, tgt = self.tr_mode.currentData()
            mode = self.ocr_mode.currentData()
            base_url, model = self._ollama_ready()

            ocr_text = self._last_ocr

            # OCR
            if mode == "llm_vision_ocr":
                if self._ms_since(self._last_llm_ocr_at) >= int(self.sp_llm_ocr_ms.value()):
                    try:
                        self.status.setText("LLM OCR…")
                        QtWidgets.QApplication.processEvents()
                        ocr_text = ollama_vision_ocr(
                            img=img,
                            lang=src,
                            model=model,
                            base_url=base_url,
                            pixel_hint=self.chk_llm_ocr_pixel_hint.isChecked(),
                            timeout_sec=75,
                        )
                        self._last_llm_ocr_at = datetime.now()
                    except Exception as e:
                        self.log_debug(f"LLM OCR error: {e}")
                        ocr_text = self._last_ocr
            else:
                # EasyOCR
                try:
                    reader = self.get_easy_reader(src)
                    arr = np.array(img.convert("RGB"))
                    results = reader.readtext(arr, detail=0, paragraph=True)
                    ocr_text = "\n".join(results)
                except Exception as e:
                    self.log_debug(f"EasyOCR error: {e}")
                    ocr_text = self._last_ocr

            ocr_text = (ocr_text or "").strip()[:MAX_TEXT_LEN].strip()

            # 辞書（alias）をOCR表示にも反映（任意）
            if self.grp_dict.isChecked() and self.chk_dict_alias.isChecked():
                ocr_text = self.name_dict.correct_aliases(ocr_text)

            # stability
            if ocr_text and ocr_text == self._last_ocr:
                self._stable_count += 1
            elif ocr_text and ocr_text != self._last_ocr:
                self._stable_count = 0

            # update OCR panel
            if ocr_text != self._last_ocr:
                self._last_ocr = ocr_text
                if self.grp_text.isChecked():
                    self.raw_text.setPlainText(ocr_text)

            if not ocr_text:
                self._last_overlay_text = "（文字が検出されません）"
                self.overlay.set_text_cache(self._last_overlay_text)
                self.overlay.set_text(self._last_overlay_text)
                return

            # OCR fix (LLM)
            fixed = ocr_text
            if self.chk_ocr_fix.isChecked():
                try:
                    fixed2 = ollama_fix_ocr_text(
                        text=ocr_text,
                        lang=src,
                        model=model,
                        base_url=base_url,
                        timeout_sec=35,
                    )
                    if fixed2.strip():
                        fixed = fixed2.strip()
                except Exception as e:
                    self.log_debug(f"OCR fix error: {e}")
                    fixed = ocr_text

            # 辞書（alias/reading）を翻訳入力（fixed）に適用（表示にも反映）
            fixed_for_translate = self.apply_dictionary_pipeline(fixed, src, tgt)

            if fixed_for_translate != self._last_fixed:
                self._last_fixed = fixed_for_translate
                if self.grp_text.isChecked():
                    self.fixed_text.setPlainText(fixed_for_translate)

            stable_required = int(self.sp_stable_required.value())
            stable_ok = (stable_required == 0) or (self._stable_count >= stable_required)

            # translate
            if stable_ok and self._ms_since(self._last_translate_at) >= int(self.sp_translate_ms.value()):
                if fixed_for_translate and fixed_for_translate != self._last_translated_source:
                    self.status.setText("翻訳…")
                    QtWidgets.QApplication.processEvents()
                    try:
                        translated = self.translate_text(fixed_for_translate)
                    except Exception as e:
                        translated = f"翻訳エラー: {e}"

                    self._last_translate_at = datetime.now()
                    self._last_translated_source = fixed_for_translate
                    self._last_overlay_text = translated

                    self.overlay.set_text_cache(translated)
                    self.overlay.set_text(translated)

            # send to TTS app
            send_text = self.get_tts_send_text(self._last_ocr, self._last_fixed, self._last_overlay_text)
            self.try_send_tts(send_text, force=False)

            tone_state = "OFF"
            if self.chk_tone.isChecked():
                tone_state = self.cmb_tone.currentData()

            self.status.setText(
                f"実行中 OCR={mode} model={self.ollama_model.text().strip()} 安定={self._stable_count} 口調={tone_state} 辞書={'ON' if self.grp_dict.isChecked() else 'OFF'} TTS={'ON' if self.grp_tts.isChecked() else 'OFF'}"
            )

        finally:
            self._busy = False


def main():
    enable_dpi_awareness_windows()
    app = QtWidgets.QApplication(sys.argv)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
