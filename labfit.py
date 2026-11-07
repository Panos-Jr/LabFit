# -*- coding: utf-8 -*-
"""
Plot & Fit GUI for the y1 physics lab  -  v3.0

Original author: Wolfgang Theis (2019).  Spreadsheet / robustness rewrite 2026.

Run:
    python labfit.py [-x] [-i]

    -x   Pre-assign a delta-X column (columns A,B,C,D = X,Y,dX,dY).
         (You can also pick any column for any role at runtime.)
    -i   Infer uncertainties from the scatter of the data if no dY is given.

From a script / console:
    import numpy as np, y1labfit
    x = np.linspace(-10, 10, 30)
    y = 0.1 * x + 0.5 * np.random.rand(30)
    labfit.main(x=x, y=y, xerr=None, yerr=np.ones(30) * 0.05)

Spreadsheet
    * 6 columns (A-F).  Choose which column is X, Y, dX, dY with the role boxes.
    * Cells accept numbers, or formulas:  =A2*2   =1/sqrt(B3)   =(A2+A3)/2   A2^2
      $A$1 style absolute references work.  ln() is natural log, log10() base 10.
    * Ctrl+V / Ctrl+C / Ctrl+X, Del, Ctrl+D (fill down; formulas shift like Excel),
      Enter moves down, everything is undoable (Ctrl+Z / Ctrl+Y).
    * "Fill column" writes a formula down a whole column in one go.
    * Select cells to see Count / Sum / Mean / Std in the status bar.
"""

from __future__ import annotations

import ast
import csv
import io
import math
import operator as _op
import os
import re
import sys
import traceback
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Callable, Optional

import numpy as np
from scipy import stats as _stats
from scipy.optimize import curve_fit
from scipy.odr import ODR, Model, RealData

from PyQt5.QtCore import Qt, QAbstractTableModel, QModelIndex, QThread, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QFont, QImage, QKeySequence, QPainter
from PyQt5.QtPrintSupport import QPrintDialog, QPrinter
from PyQt5.QtWidgets import (
    QAbstractItemDelegate, QAbstractItemView, QAction, QApplication, QCheckBox,
    QComboBox, QFileDialog, QFormLayout, QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QMainWindow, QMenu, QMessageBox, QPlainTextEdit, QPushButton,
    QScrollArea, QSizePolicy, QSplitter, QTableView, QUndoCommand, QUndoStack,
    QVBoxLayout, QWidget,
)

import matplotlib
matplotlib.use('Qt5Agg')
from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.backends.backend_qt5agg import NavigationToolbar2QT as NavigationToolbar
from matplotlib.figure import Figure

sys.setrecursionlimit(max(sys.getrecursionlimit(), 5000))

import warnings
from scipy.optimize import OptimizeWarning
warnings.simplefilter('ignore', OptimizeWarning)
warnings.filterwarnings('ignore', message='Polyfit may be poorly conditioned')
warnings.filterwarnings('ignore', category=RuntimeWarning)

NCOLS = 6
LETTERS = 'ABCDEF'
INITIAL_ROWS = 500
MAX_ROWS = 200_000
ROLE_KEYS = ('x', 'y', 'dx', 'dy')
ROLE_LABELS = {'x': 'X', 'y': 'Y', 'dx': 'ΔX', 'dy': 'ΔY'}

def _parent():
    return QApplication.activeWindow()


def show_info(text: str, title: str = "Notice") -> None:
    QMessageBox.information(_parent(), title, text)


def show_warn(text: str, title: str = "Warning") -> None:
    QMessageBox.warning(_parent(), title, text)


def get_reply(text: str, title: str = "Confirm") -> bool:
    return QMessageBox.question(_parent(), title, text,
                                QMessageBox.Ok | QMessageBox.Cancel) == QMessageBox.Ok


_NUM_RE = re.compile(r'^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$')

def resource_path(relative_path):
    try:
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)

def parse_number(text) -> Optional[float]:
    """Strict number parser. Accepts 1.5, 1,5 (decimal comma), -3e-4, unicode minus.
    Returns None for anything else (text, 'nan', 'inf', '5 cm', '1 2' ...)."""
    if text is None:
        return None
    s = str(text).strip().replace('−', '-').replace(' ', '').replace(' ', '')
    if not s:
        return None
    if ',' in s and '.' not in s and s.count(',') == 1:
        s = s.replace(',', '.')
    if not _NUM_RE.match(s):
        return None
    try:
        v = float(s)
    except ValueError:
        return None
    return v if math.isfinite(v) else None


def error_string(v: float, e: float, sig: int = 2) -> str:
    """'(1.23 \\pm 0.04) 10^{3}' style string for matplotlib mathtext."""
    if not np.isfinite(v):
        return 'n/a'
    if not np.isfinite(e) or e <= 0:
        return f'{v:#.4g}'
    try:
        estr = f'{e:#.{sig}g}'.split('e')
        mant = estr[0].rstrip('.')
        dec = max(0, len(mant) - 2)
        if len(estr) > 1:
            exp = int(estr[1])
            return f'({v / 10.0 ** exp:.{dec}f} $\\pm$ {mant}) 10$^{{{exp}}}$'
        return f'({v:.{dec}f} $\\pm$ {mant})'
    except Exception:
        return f'{v:#.4g}'

class CellError(Exception):
    """Any problem with a formula or a referenced cell. str(e) is user-facing."""


_BIN = {ast.Add: _op.add, ast.Sub: _op.sub, ast.Mult: _op.mul,
        ast.Div: _op.truediv, ast.Mod: _op.mod, ast.FloorDiv: _op.floordiv}
_UN = {ast.UAdd: _op.pos, ast.USub: _op.neg}
_FUNCS = {
    'sqrt': math.sqrt, 'abs': abs, 'exp': math.exp, 'ln': math.log, 'log': math.log,
    'log10': math.log10, 'log2': math.log2, 'sin': math.sin, 'cos': math.cos,
    'tan': math.tan, 'asin': math.asin, 'acos': math.acos, 'atan': math.atan,
    'atan2': math.atan2, 'sinh': math.sinh, 'cosh': math.cosh, 'tanh': math.tanh,
    'floor': math.floor, 'ceil': math.ceil, 'round': round, 'min': min, 'max': max,
    'pow': math.pow, 'rad': math.radians, 'deg': math.degrees, 'sign': lambda v: float((v > 0) - (v < 0)),
}
_CONSTS = {'pi': math.pi, 'e': math.e, 'tau': math.tau}
_CELL_NAME = re.compile(r'^([A-Fa-f])(\d+)$')
# For rewriting references in a formula text (fill down / fill column)
_REF_RE = re.compile(r'(?<![A-Za-z0-9_.$])(\$?)([A-Fa-f])(\$?)(\d+)(?![A-Za-z0-9_(])')


@lru_cache(maxsize=60000)
def _parse(body: str):
    s = (body.replace('−', '-').replace('^', '**').replace('$', '')
         .replace('×', '*').replace('÷', '/').replace('·', '*').strip())
    if not s:
        raise CellError('empty formula')
    if len(s) > 500:
        raise CellError('formula too long')
    try:
        return ast.parse(s, mode='eval').body
    except (SyntaxError, ValueError, RecursionError):
        raise CellError('syntax error')


def _ev(node, get_cell: Callable[[int, int], float]) -> float:
    if isinstance(node, ast.Constant):
        v = node.value
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise CellError('only numbers are allowed')
        return float(v)
    if isinstance(node, ast.Name):
        m = _CELL_NAME.match(node.id)
        if m:
            row = int(m.group(2))
            if row < 1 or row > MAX_ROWS:
                raise CellError(f'row out of range in {node.id}')
            return get_cell(row - 1, LETTERS.index(m.group(1).upper()))
        key = node.id.lower()
        if key in _CONSTS:
            return _CONSTS[key]
        raise CellError(f'unknown name "{node.id}"')
    if isinstance(node, ast.BinOp):
        a, b = _ev(node.left, get_cell), _ev(node.right, get_cell)
        try:
            if isinstance(node.op, ast.Pow):
                return math.pow(a, b)
            fn = _BIN.get(type(node.op))
            if fn is None:
                raise CellError('unsupported operator')
            return float(fn(a, b))
        except ZeroDivisionError:
            raise CellError('division by zero')
        except OverflowError:
            raise CellError('number too large')
        except ValueError:
            raise CellError('invalid arithmetic (e.g. negative number to a fractional power)')
    if isinstance(node, ast.UnaryOp):
        fn = _UN.get(type(node.op))
        if fn is None:
            raise CellError('unsupported operator')
        return float(fn(_ev(node.operand, get_cell)))
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.keywords:
            raise CellError('unsupported function call')
        fn = _FUNCS.get(node.func.id.lower())
        if fn is None:
            raise CellError(f'unknown function "{node.func.id}"')
        args = [_ev(a, get_cell) for a in node.args]
        if not args:
            raise CellError(f'{node.func.id}() needs an argument')
        try:
            return float(fn(*args))
        except ZeroDivisionError:
            raise CellError('division by zero')
        except OverflowError:
            raise CellError('number too large')
        except (ValueError, TypeError):
            raise CellError(f'{node.func.id}(): invalid argument')
    raise CellError('unsupported syntax')


def evaluate_expression(expr: str, get_cell: Callable[[int, int], float]) -> float:
    """Evaluate '=1/sqrt(A2)' (leading '=' optional). get_cell(row0, col0) -> float
    must raise CellError for empty/bad cells. Always returns a finite float or raises CellError."""
    s = (expr or '').strip()
    if s.startswith('='):
        s = s[1:]
    try:
        v = _ev(_parse(s), get_cell)
    except CellError:
        raise
    except RecursionError:
        raise CellError('formula too deeply nested')
    except Exception as exc:  # never let anything else escape
        raise CellError(f'{type(exc).__name__}')
    if not math.isfinite(v):
        raise CellError('result is not finite')
    return v


def no_cells(r, c):
    raise CellError('cell references are not allowed here')


def shift_formula(text: str, drow: int) -> str:
    """Move relative row references down by drow (Excel-style fill)."""
    def rep(m):
        row = int(m.group(4))
        if not m.group(3):          # relative row
            row += drow
        if row < 1:
            raise CellError('reference moved above row 1')
        return f'{m.group(1)}{m.group(2).upper()}{m.group(3)}{row}'
    return _REF_RE.sub(rep, text)




_EMPTY = ('empty', None)


@dataclass
class Extracted:
    data: np.ndarray                    # (n, 4): x, y, dx, dy   (NaN where absent)
    rows: np.ndarray                    # spreadsheet row numbers (1-based) of data rows
    issues: list = field(default_factory=list)   # things the user should fix
    notes: list = field(default_factory=list)    # FYI

    @property
    def n(self) -> int:
        return int(self.data.shape[0])


def clean_cell(text) -> Optional[str]:
    if text is None:
        return None
    s = str(text).strip().replace('−', '-').replace(' ', ' ')
    if not s:
        return None
    if re.match(r'^[+-]?\d+,\d+([eE][+-]?\d+)?$', s):    # European decimal comma
        s = s.replace(',', '.')
    return s


class CellsCommand(QUndoCommand):
    def __init__(self, model, old: dict, new: dict, text: str):
        super().__init__(text)
        self.model, self.old, self.new = model, old, new

    def redo(self):
        self.model._apply(self.new)

    def undo(self):
        self.model._apply(self.old)


class DataTableModel(QAbstractTableModel):
    changed = pyqtSignal()

    def __init__(self, undo_stack: Optional[QUndoStack] = None, parent=None):
        super().__init__(parent)
        self._raw: list = [[None] * NCOLS for _ in range(INITIAL_ROWS)]
        self._cache: dict = {}
        self._dirty = True
        self._formulas: set = set()
        self.undo_stack = undo_stack
        self.roles: dict = {'x': 0, 'y': 1, 'dx': None, 'dy': 2}


    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._raw)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else NCOLS

    def flags(self, index):
        return Qt.ItemIsSelectable | Qt.ItemIsEnabled | Qt.ItemIsEditable

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role != Qt.DisplayRole:
            return None
        if orientation == Qt.Vertical:
            return str(section + 1)
        tags = [ROLE_LABELS[k] for k in ROLE_KEYS if self.roles.get(k) == section]
        return LETTERS[section] + (f'  ({", ".join(tags)})' if tags else '')

    def refresh_headers(self):
        self.headerDataChanged.emit(Qt.Horizontal, 0, NCOLS - 1)

    def set_roles(self, roles: dict):
        self.roles = dict(roles)
        self.refresh_headers()
        self.changed.emit()

    def raw(self, r: int, c: int) -> Optional[str]:
        if 0 <= r < len(self._raw) and 0 <= c < NCOLS:
            return self._raw[r][c]
        return None

    def _ensure_rows(self, n: int):
        n = min(n, MAX_ROWS)
        if n > len(self._raw):
            self.beginInsertRows(QModelIndex(), len(self._raw), n - 1)
            self._raw.extend([[None] * NCOLS for _ in range(n - len(self._raw))])
            self.endInsertRows()

    def last_row(self, cols=None) -> int:
        """1-based number of the last non-empty row (0 if none)."""
        cols = range(NCOLS) if not cols else cols
        for r in range(len(self._raw) - 1, -1, -1):
            row = self._raw[r]
            if any(row[c] is not None for c in cols):
                return r + 1
        return 0


    def _res(self, r: int, c: int, stack=None):
        key = (r, c)
        hit = self._cache.get(key)
        if hit is not None:
            return hit
        raw = self.raw(r, c)
        if raw is None:
            return _EMPTY
        s = raw.strip()
        if s.startswith('='):
            stack = set() if stack is None else stack
            if key in stack:
                res = ('err', 'circular reference')
            else:
                stack.add(key)
                try:
                    res = ('ok', evaluate_expression(s, lambda rr, cc: self._ref(rr, cc, stack)))
                except CellError as exc:
                    res = ('err', str(exc))
                except Exception as exc:
                    res = ('err', type(exc).__name__)
                finally:
                    stack.discard(key)
        else:
            v = parse_number(s)
            res = ('ok', v) if v is not None else ('err', 'not a number')
        self._cache[key] = res
        return res

    def _ref(self, r, c, stack):
        if r >= len(self._raw):
            raise CellError(f'{LETTERS[c]}{r + 1} is empty')
        res = self._res(r, c, stack)
        if res[0] == 'ok':
            return res[1]
        name = f'{LETTERS[c]}{r + 1}'
        if res[0] == 'empty':
            raise CellError(f'{name} is empty')
        raise CellError(f'{name} has an error ({res[1]})')

    def _ensure_cache(self):
        if self._dirty:
            self._cache.clear()
            for key in sorted(self._formulas):      # row order keeps recursion shallow
                self._res(*key)
            self._dirty = False

    def value(self, r, c) -> Optional[float]:
        self._ensure_cache()
        res = self._res(r, c)
        return res[1] if res[0] == 'ok' else None

    def result(self, r, c):
        self._ensure_cache()
        return self._res(r, c)

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        r, c = index.row(), index.column()
        raw = self._raw[r][c]
        if role == Qt.TextAlignmentRole:
            return int(Qt.AlignRight | Qt.AlignVCenter)
        if raw is None:
            return '' if role in (Qt.DisplayRole, Qt.EditRole) else None
        if role == Qt.EditRole:
            return raw
        res = self.result(r, c)
        is_f = raw.strip().startswith('=')
        if role == Qt.DisplayRole:
            if res[0] == 'err':
                return '#ERR'
            return f'{res[1]:.6g}' if is_f else raw
        if role == Qt.BackgroundRole:
            if res[0] == 'err':
                return QColor(255, 214, 214)
            if is_f:
                return QColor(228, 240, 255)
        if role == Qt.ForegroundRole and (res[0] == 'err' or is_f):
            return QColor(0, 0, 0)
        if role == Qt.ToolTipRole:
            if res[0] == 'err':
                return f'{raw}\n#ERR: {res[1]}'
            if is_f:
                return f'{raw}\n= {res[1]:.10g}'
        return None

    def setData(self, index, value, role=Qt.EditRole):
        if role != Qt.EditRole or not index.isValid():
            return False
        new = self.interpret(value)
        self.push_changes({(index.row(), index.column()): new}, 'edit cell')
        return True

    def interpret(self, value) -> Optional[str]:
        """Turn typed text into what we store. Bare expressions become formulas."""
        s = clean_cell(value)
        if s is None or s.startswith('=') or parse_number(s) is not None:
            return s
        try:
            evaluate_expression(s, lambda rr, cc: self._ref(rr, cc, set()))
            return '=' + s
        except CellError:
            return s          # keep the text; it will show #ERR with an explanation

    def push_changes(self, changes: dict, text: str):
        old, new = {}, {}
        for key, val in changes.items():
            cur = self.raw(*key)
            if cur != val:
                old[key], new[key] = cur, val
        if not new:
            return
        self._ensure_rows(max(r for r, _ in new) + 1)
        if self.undo_stack is not None:
            self.undo_stack.push(CellsCommand(self, old, new, text))
        else:
            self._apply(new)

    def _apply(self, changes: dict):
        if not changes:
            return
        self._ensure_rows(max(r for r, _ in changes) + 1)
        for (r, c), val in changes.items():
            if r >= len(self._raw):
                continue
            self._raw[r][c] = val
            if val is not None and val.strip().startswith('='):
                self._formulas.add((r, c))
            else:
                self._formulas.discard((r, c))
        self._dirty = True
        if self._formulas or len(changes) > 5000:
            self.dataChanged.emit(self.index(0, 0), self.index(len(self._raw) - 1, NCOLS - 1))
        else:
            rs = [r for r, _ in changes]
            cs = [c for _, c in changes]
            self.dataChanged.emit(self.index(min(rs), min(cs)), self.index(max(rs), max(cs)))
        self.changed.emit()

    def clear_cells(self, cells):
        self.push_changes({k: None for k in cells}, 'clear cells')

    def clear_all(self, text='clear all data'):
        self.push_changes({(r, c): None for r in range(len(self._raw))
                           for c in range(NCOLS) if self._raw[r][c] is not None}, text)

    def replace_all(self, rows: list, text: str):
        """rows: list of lists of cell text. One undoable step replaces the sheet."""
        changes = {(r, c): None for r in range(len(self._raw))
                   for c in range(NCOLS) if self._raw[r][c] is not None}
        for r, row in enumerate(rows[:MAX_ROWS]):
            for c, v in enumerate(row[:NCOLS]):
                changes[(r, c)] = clean_cell(v)
        self.push_changes(changes, text)

    def paste_block(self, r0: int, c0: int, block: list) -> tuple:
        changes, clipped = {}, False
        for i, row in enumerate(block):
            if r0 + i >= MAX_ROWS:
                clipped = True
                break
            for j, v in enumerate(row):
                if c0 + j >= NCOLS:
                    clipped = True
                    break
                changes[(r0 + i, c0 + j)] = self.interpret(v) if v is not None else None
        self.push_changes(changes, 'paste')
        return len(block), clipped

    def fill_down(self, r0, r1, cols):
        changes = {}
        for c in cols:
            top = self.raw(r0, c)
            for r in range(r0 + 1, r1 + 1):
                try:
                    changes[(r, c)] = shift_formula(top, r - r0) if top and top.strip().startswith('=') else top
                except CellError:
                    changes[(r, c)] = '=#REF'
        self.push_changes(changes, 'fill down')

    def fill_formula(self, col: int, template: str) -> int:
        """Write `template` (written for its first row) down column `col`. Returns rows written."""
        t = template.strip()
        t = t if t.startswith('=') else '=' + t
        refs = list(_REF_RE.finditer(t))
        cols = {LETTERS.index(m.group(2).upper()) for m in refs}
        base = min((int(m.group(4)) for m in refs), default=1)
        last = self.last_row(cols or None)
        if last < base:
            raise CellError('nothing to fill: the cells the formula refers to are empty')
        try:                                    # test the first row before touching anything
            evaluate_expression(t, lambda rr, cc: self._ref(rr, cc, set()))
        except CellError as exc:
            raise CellError(f'formula fails on its first row ({base}): {exc}')
        n_over = sum(1 for r in range(base - 1, last) if self.raw(r, col) is not None)
        if n_over and not get_reply(f'Overwrite {n_over} existing cell(s) in column {LETTERS[col]}?'):
            return 0
        changes = {(r - 1, col): shift_formula(t, r - base) for r in range(base, last + 1)}
        self.push_changes(changes, f'fill column {LETTERS[col]}')
        return len(changes)
        
    def extract(self) -> Extracted:
        self._ensure_cache()
        n = len(self._raw)
        arr = np.full((n, 4), np.nan)
        err_cells = []
        for j, key in enumerate(ROLE_KEYS):
            c = self.roles.get(key)
            if c is None:
                continue
            for r in range(n):
                if self._raw[r][c] is None:
                    continue
                res = self._res(r, c)
                if res[0] == 'ok':
                    arr[r, j] = res[1]
                elif res[0] == 'err':
                    err_cells.append((key, r, c, res[1]))
        ok = np.isfinite(arr[:, 0]) & np.isfinite(arr[:, 1])
        issues, notes = [], []
        if err_cells:
            shown = '; '.join(f'{LETTERS[c]}{r + 1}: {m}' for _, r, c, m in err_cells[:4])
            more = f' (+{len(err_cells) - 4} more)' if len(err_cells) > 4 else ''
            issues.append(f'{len(err_cells)} cell(s) in data columns have errors and are ignored - {shown}{more}')
        half = int(np.sum(np.isfinite(arr[:, 0]) ^ np.isfinite(arr[:, 1])))
        if half:
            notes.append(f'{half} row(s) have only X or only Y and are ignored')
        rows = np.nonzero(ok)[0] + 1
        data = arr[ok].copy()
        for j, nm in ((2, 'ΔX'), (3, 'ΔY')):
            col = data[:, j]
            fin = np.isfinite(col)
            if data.shape[0] and fin.any() and not fin.all():
                idx = np.where(fin, np.arange(col.size), 0)
                np.maximum.accumulate(idx, out=idx)
                filled = col[idx]
                first = int(np.argmax(fin))
                filled[:first] = np.nan
                nfill = int(np.sum(np.isfinite(filled) & ~fin))
                data[:, j] = filled
                if nfill:
                    notes.append(f'{nm} replicated from the row above in {nfill} row(s)')
        return Extracted(data=data, rows=rows, issues=issues, notes=notes)

    def display_text(self, r, c, formulas=False) -> str:
        raw = self.raw(r, c)
        if raw is None:
            return ''
        if formulas or not raw.strip().startswith('='):
            return raw
        res = self.result(r, c)
        return f'{res[1]:.10g}' if res[0] == 'ok' else ''

    def selection_values(self, cells) -> np.ndarray:
        out = []
        for r, c in cells:
            res = self.result(r, c)
            if res[0] == 'ok':
                out.append(res[1])
        return np.array(out, dtype=float)


PRESETS = [
    ('Quick template…', None, 0),
    ('1/√A', '1/sqrt({a})', 1), ('√A', 'sqrt({a})', 1), ('A²', '{a}^2', 1),
    ('1/A', '1/{a}', 1), ('1/A²', '1/{a}^2', 1), ('ln(A)', 'ln({a})', 1),
    ('log10(A)', 'log10({a})', 1), ('exp(A)', 'exp({a})', 1), ('|A|', 'abs({a})', 1),
    ('A + B', '{a}+{b}', 2), ('A − B', '{a}-{b}', 2), ('A × B', '{a}*{b}', 2), ('A ÷ B', '{a}/{b}', 2),
]

def parse_clipboard(text: str) -> list:
    lines = text.replace('\r\n', '\n').replace('\r', '\n').split('\n')
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines:
        return []
    if any('\t' in l for l in lines):
        split = lambda l: l.split('\t')
    elif any(';' in l for l in lines):
        split = lambda l: l.split(';')
    elif any(',' in l for l in lines):
        split = lambda l: l.split(',')
    else:
        split = lambda l: l.split()
    return [[clean_cell(c) for c in split(l)] for l in lines]


class SpreadsheetView(QTableView):
    message = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSelectionMode(QAbstractItemView.ContiguousSelection)
        self.setSelectionBehavior(QAbstractItemView.SelectItems)
        self.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed
                             | QAbstractItemView.AnyKeyPressed)
        self.setAlternatingRowColors(True)
        self.verticalHeader().setDefaultSectionSize(22)
        self.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.horizontalHeader().setDefaultSectionSize(92)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._context_menu)

    # Enter commits and moves down, like Excel
    def closeEditor(self, editor, hint):
        super().closeEditor(editor, hint)
        if hint == QAbstractItemDelegate.SubmitModelCache:
            cur = self.currentIndex()
            if cur.isValid() and cur.row() + 1 < self.model().rowCount():
                self.setCurrentIndex(self.model().index(cur.row() + 1, cur.column()))

    def currentChanged(self, current, previous):
        super().currentChanged(current, previous)
        m = self.model()
        if m is not None and current.isValid() and current.row() >= m.rowCount() - 10:
            m._ensure_rows(m.rowCount() + 200)

    def rect(self):
        idxs = self.selectedIndexes()
        if not idxs:
            cur = self.currentIndex()
            if not cur.isValid():
                return None
            return cur.row(), cur.row(), cur.column(), cur.column()
        rs = [i.row() for i in idxs]
        cs = [i.column() for i in idxs]
        return min(rs), max(rs), min(cs), max(cs)

    def keyPressEvent(self, e):
        if e.matches(QKeySequence.Copy):
            return self.copy_selection()
        if e.matches(QKeySequence.Cut):
            return self.cut_selection()
        if e.matches(QKeySequence.Paste):
            return self.paste_clipboard()
        if e.key() == Qt.Key_Delete and self.state() != QAbstractItemView.EditingState:
            return self.clear_selection()
        if e.modifiers() == Qt.ControlModifier and e.key() == Qt.Key_D:
            return self.fill_down_selection()
        super().keyPressEvent(e)

    def copy_selection(self, formulas=False):
        rc = self.rect()
        if rc is None:
            return
        r0, r1, c0, c1 = rc
        if (r1 - r0 + 1) * (c1 - c0 + 1) > 2_000_000:
            return
        m = self.model()
        text = '\n'.join('\t'.join(m.display_text(r, c, formulas) for c in range(c0, c1 + 1))
                         for r in range(r0, r1 + 1))
        QApplication.clipboard().setText(text)

    def cut_selection(self):
        self.copy_selection(formulas=True)
        self.clear_selection()

    def clear_selection(self):
        idxs = self.selectedIndexes()
        if idxs:
            self.model().clear_cells([(i.row(), i.column()) for i in idxs])

    def fill_down_selection(self):
        rc = self.rect()
        if rc and rc[1] > rc[0]:
            self.model().fill_down(rc[0], rc[1], range(rc[2], rc[3] + 1))

    def paste_clipboard(self):
        try:
            block = parse_clipboard(QApplication.clipboard().text())
            if not block:
                return
            skipped = ''
            first = block[0]
            if (len(block) > 1 and any(first) and
                    all(v is None or (parse_number(v) is None and not v.startswith('=')) for v in first)):
                block = block[1:]
                skipped = ' (header row skipped)'
            rc = self.rect()
            r0, c0 = (rc[0], rc[2]) if rc else (0, 0)
            n, clipped = self.model().paste_block(r0, c0, block)
            self.message.emit(f'Pasted {n} row(s){skipped}' + ('; columns/rows beyond the sheet were dropped' if clipped else ''))
        except Exception as exc:
            show_warn(f'Paste failed: {exc}')

    def _context_menu(self, pos):
        menu = QMenu(self)
        for text, fn in (('Copy', self.copy_selection), ('Copy as formulas', lambda: self.copy_selection(True)),
                         ('Cut', self.cut_selection), ('Paste', self.paste_clipboard),
                         ('Clear', self.clear_selection), ('Fill down', self.fill_down_selection)):
            menu.addAction(text, fn)
        menu.exec_(self.viewport().mapToGlobal(pos))


class FitFunction(ABC):
    name: str = 'abstract'
    latex: str = ''
    param_names: list = []
    param_units: list = []
    param_names_latex: Optional[list] = None
    multistart: bool = False          # try several starting points (oscillating / peaked models)

    @abstractmethod
    def calc(self, x, *params): ...

    def guess_params(self, x, y) -> np.ndarray:
        return np.ones(len(self.param_names))

    def legend(self, popt) -> str:
        return 'fit: ' + ', '.join(f'{n}={v:#.4g}'.rstrip('.') for n, v in zip(self.param_names, popt))

    def detailed_text(self, popt, perr) -> str:
        names = self.param_names_latex or self.param_names
        lines = [f'${n}$ = {error_string(float(p), float(e))} {u}'.rstrip()
                 for n, p, e, u in zip(names, popt, perr, self.param_units)]
        return self.latex + '\n\n' + ',\n'.join(lines)

    def calc_odr(self, beta, x):
        return self.calc(x, *beta)


def _span(x):
    return float(x.max() - x.min()) or 1.0


class FitFunction_constant(FitFunction):
    name = 'constant, y = a'; latex = '$y = a$'
    param_names = ['a']; param_units = ['y']
    def calc(self, x, a): return np.full(np.shape(x), a, dtype=float)
    def guess_params(self, x, y): return np.array([float(np.mean(y))])


class FitFunction_proportional(FitFunction):
    name = 'proportional, y = ax'; latex = '$y = a x$'
    param_names = ['a']; param_units = ['y/x']
    def calc(self, x, a): return a * x
    def guess_params(self, x, y):
        m = np.abs(x) > 1e-12
        if not m.any():
            return np.array([1.0])
        return np.array([float(np.median(y[m] / x[m]))])


class FitFunction_linear(FitFunction):
    name = 'linear, y = ax + b'; latex = '$y = a x + b$'
    param_names = ['a', 'b']; param_units = ['y/x', 'y']
    def calc(self, x, a, b): return a * x + b
    def guess_params(self, x, y):
        try:
            a, b = np.polyfit(x, y, 1)
            if np.isfinite(a) and np.isfinite(b):
                return np.array([a, b])
        except Exception:
            pass
        return np.array([1.0, 0.0])


class FitFunction_quadratic(FitFunction):
    name = 'quadratic, y = ax² + bx + c'; latex = '$y = a x^2 + b x + c$'
    param_names = ['a', 'b', 'c']; param_units = ['y/x²', 'y/x', 'y']
    def calc(self, x, a, b, c): return a * x**2 + b * x + c
    def guess_params(self, x, y):
        try:
            return np.array(np.polyfit(x, y, 2), dtype=float)
        except Exception:
            return np.array([0.0, 1.0, 0.0])


class FitFunction_cubic(FitFunction):
    name = 'cubic, y = ax³ + bx² + cx + d'; latex = '$y = a x^3 + b x^2 + c x + d$'
    param_names = ['a', 'b', 'c', 'd']; param_units = ['y/x³', 'y/x²', 'y/x', 'y']
    def calc(self, x, a, b, c, d): return a * x**3 + b * x**2 + c * x + d
    def guess_params(self, x, y):
        try:
            return np.array(np.polyfit(x, y, 3), dtype=float)
        except Exception:
            return np.array([0.0, 0.0, 1.0, 0.0])


class FitFunction_cubic_odd(FitFunction):
    name = 'cubic odd, y = ax³ + bx'; latex = '$y = a x^3 + b x$'
    param_names = ['a', 'b']; param_units = ['y/x³', 'y/x']
    def calc(self, x, a, b): return a * x**3 + b * x
    def guess_params(self, x, y):
        try:
            A = np.column_stack([x**3, x])
            sol = np.linalg.lstsq(A, y, rcond=None)[0]
            if np.all(np.isfinite(sol)):
                return sol
        except Exception:
            pass
        return np.array([0.0, 1.0])


class FitFunction_power(FitFunction):
    name = 'power, y = a·|x|^p'; latex = '$y = a |x|^p$'
    param_names = ['a', 'p']; param_units = ['y/x^p', '']
    multistart = True
    def calc(self, x, a, p):
        with np.errstate(all='ignore'):
            return a * np.abs(x) ** p
    def guess_params(self, x, y):
        m = (np.abs(x) > 0) & (y > 0)
        if m.sum() < 3:
            return np.array([1.0, 1.0])
        try:
            p, loga = np.polyfit(np.log(np.abs(x[m])), np.log(y[m]), 1)
            if np.isfinite(p) and np.isfinite(loga):
                return np.array([np.exp(loga), p])
        except Exception:
            pass
        return np.array([1.0, 1.0])


class FitFunction_exponential(FitFunction):
    name = 'exponential, y = a·exp(bx)'; latex = '$y = a e^{b x}$'
    param_names = ['a', 'b']; param_units = ['y', '1/x']
    multistart = True
    def calc(self, x, a, b):
        with np.errstate(all='ignore'):
            return a * np.exp(b * x)
    def guess_params(self, x, y):
        m = y > 0
        if m.sum() < 3:
            return np.array([1.0, 0.1])
        try:
            b, loga = np.polyfit(x[m], np.log(y[m]), 1)
            if np.isfinite(b) and np.isfinite(loga):
                return np.array([np.exp(loga), b])
        except Exception:
            pass
        return np.array([1.0, 0.1])


class FitFunction_gaussian(FitFunction):
    name = 'Gaussian, y = a·exp(-(x-b)²/(2c²))'; latex = '$y = a e^{-(x-b)^2/(2c^2)}$'
    param_names = ['a', 'b', 'c']; param_units = ['y', 'x', 'x']
    multistart = True
    def calc(self, x, a, b, c):
        c = c if abs(c) > 1e-300 else 1e-300
        with np.errstate(all='ignore'):
            return a * np.exp(-(x - b) ** 2 / (2 * c ** 2))
    def guess_params(self, x, y):
        i = int(np.argmax(y))
        return np.array([float(y[i]), float(x[i]), _span(x) / 5])


class FitFunction_resonance(FitFunction):
    name = 'resonance (Lorentzian)'
    latex = r'$y = a (\Delta x/2)^2 / [(x-x_0)^2 + (\Delta x/2)^2]$'
    param_names = ['a', 'x_0', 'delta_x']
    param_names_latex = ['a', 'x_0', r'\Delta x']
    param_units = ['y', 'x', 'x']
    multistart = True
    def calc(self, x, a, x0, dx):
        half2 = (dx / 2.0) ** 2
        with np.errstate(all='ignore'):
            return a * half2 / ((x - x0) ** 2 + half2)
    def guess_params(self, x, y):
        i = int(np.argmax(y))
        return np.array([float(y[i]), float(x[i]), _span(x) / 5])


class FitFunction_cosine(FitFunction):
    name = 'cosine, y = a·cos(bx+c)'; latex = r'$y = a\cos(bx+c)$'
    param_names = ['a', 'b', 'c']; param_units = ['y', 'rad/x', 'rad']
    multistart = True
    def calc(self, x, a, b, c): return a * np.cos(b * x + c)
    @staticmethod
    def _freq(x, y):
        yc = y - np.mean(y)
        s = np.sign(yc)
        zc = []
        for i in range(len(x) - 1):
            if s[i] == 0:
                zc.append(x[i])
            elif s[i] * s[i + 1] < 0:
                d = yc[i + 1] - yc[i]
                if d != 0:
                    zc.append(x[i] - yc[i] / d * (x[i + 1] - x[i]))
        if len(zc) >= 2:
            dz = float(np.median(np.diff(zc)))
            if dz > 0:
                return np.pi / dz, zc[0]
        return None, None
    def guess_params(self, x, y):
        a = float((y.max() - y.min()) / 2) or 1.0
        b, z0 = self._freq(x, y)
        if b is not None:
            return np.array([a, b, np.pi / 2 - b * z0])
        b = 2 * np.pi / _span(x)
        return np.array([a, b, -b * float(x[int(np.argmax(y))])])


class FitFunction_abscosine(FitFunction):
    name = '|cosine|, y = a·|cos(bx+c)|'; latex = r'$y = a|\cos(bx+c)|$'
    param_names = ['a', 'b', 'c']; param_units = ['y', 'rad/x', 'rad']
    multistart = True
    def calc(self, x, a, b, c): return a * np.abs(np.cos(b * x + c))
    @staticmethod
    def _freq(x, y):
        a = float(np.max(y))
        mins = [x[i] for i in range(1, len(x) - 1)
                if y[i] < y[i - 1] and y[i] <= y[i + 1] and y[i] < a * 0.5]
        if len(mins) >= 2:
            dm = float(np.median(np.diff(mins)))
            if dm > 0:
                return np.pi / dm, mins[0]
        return None, None
    def guess_params(self, x, y):
        a = float(np.max(y)) or 1.0
        b, m0 = self._freq(x, y)
        if b is not None:
            return np.array([a, b, np.pi / 2 - b * m0])
        b = np.pi / _span(x)
        return np.array([a, b, np.pi / 2 - b * float(x[int(np.argmin(y))])])


FIT_FUNCTIONS = [
    FitFunction_constant(), FitFunction_proportional(), FitFunction_linear(),
    FitFunction_quadratic(), FitFunction_cubic(), FitFunction_cubic_odd(),
    FitFunction_power(), FitFunction_exponential(), FitFunction_gaussian(),
    FitFunction_resonance(), FitFunction_cosine(), FitFunction_abscosine(),
]

class FitError(Exception):
    pass


@dataclass
class FitResult:
    popt: np.ndarray
    perr: np.ndarray
    pcov: Optional[np.ndarray]
    n: int
    dof: int
    method: str
    r2: float
    rms: float
    chi2: float = float('nan')
    chi2_red: float = float('nan')
    pvalue: float = float('nan')
    inferred: bool = False        # errors inferred from scatter (no ΔY supplied)
    s_hat: float = float('nan')
    notes: list = field(default_factory=list)


class FitEngine:
    @staticmethod
    def sigma_eff(func, popt, x, sigma, xsigma):
        """Effective y-uncertainty (adds x-uncertainty through the local slope)."""
        if sigma is None and xsigma is None:
            return None
        s2 = np.zeros_like(x) if sigma is None else np.asarray(sigma, float) ** 2
        if xsigma is not None:
            h = 1e-6 * np.maximum(1.0, np.abs(x))
            with np.errstate(all='ignore'):
                d = (func.calc(x + h, *popt) - func.calc(x - h, *popt)) / (2 * h)
            d = np.where(np.isfinite(d), d, 0.0)
            s2 = s2 + (d * np.asarray(xsigma, float)) ** 2
        s = np.sqrt(s2)
        if not np.all(np.isfinite(s)) or np.any(s <= 0):
            return None
        return s

    @staticmethod
    def _objective(func, p, x, y, sigma, xsigma):
        with np.errstate(all='ignore'):
            yf = func.calc(x, *p)
        if not np.all(np.isfinite(yf)):
            return float('inf')
        s = FitEngine.sigma_eff(func, p, x, sigma, xsigma)
        r = (yf - y) if s is None else (yf - y) / s
        return float(np.sum(r * r))

    @staticmethod
    def _fit_once(func, x, y, sigma, xsigma, start):
        npar = len(func.param_names)
        notes = []
        if xsigma is not None:
            odr = ODR(RealData(x, y, sx=xsigma, sy=sigma), Model(func.calc_odr),
                      beta0=np.asarray(start, float), maxit=300)
            odr.set_job(fit_type=0)
            out = odr.run()
            popt = np.asarray(out.beta, float)
            if not np.all(np.isfinite(popt)):
                raise FitError('ODR produced non-finite parameters')
            if out.info > 3:
                notes.append('ODR stopped before full convergence - check the fit or change the start values')
            cov = out.cov_beta
            if cov is not None and sigma is not None and getattr(out, 'res_var', 0) > 0:
                cov = cov / out.res_var                  # absolute-error scaling
            pcov = None if cov is None else np.asarray(cov, float)
            return popt, pcov, 'orthogonal distance regression (ΔX and ΔY)', notes
        kw = dict(p0=np.asarray(start, float), maxfev=20000)
        if sigma is not None:
            kw.update(sigma=sigma, absolute_sigma=True)
        popt, pcov = curve_fit(func.calc, x, y, **kw)
        return np.asarray(popt, float), np.asarray(pcov, float), 'weighted least squares' if sigma is not None else 'least squares', notes

    @staticmethod
    def perform(func, x, y, sigma=None, xsigma=None, p0=None) -> FitResult:
        x = np.asarray(x, float)
        y = np.asarray(y, float)
        npar = len(func.param_names)
        used = np.isfinite(x) & np.isfinite(y)
        sig = None if sigma is None else np.asarray(sigma, float)
        xs = None if xsigma is None else np.asarray(xsigma, float)
        for nm, arr in (('ΔY', sig), ('ΔX', xs)):
            if arr is not None and np.any(used & ~(np.isfinite(arr) & (arr > 0))):
                raise FitError(f'{nm} must be positive and finite for every point')
        x, y = x[used], y[used]
        sig = None if sig is None else sig[used]
        xs = None if xs is None else xs[used]
        n = x.size
        if n <= npar:
            raise FitError(f'need more than {npar} points for this model (have {n})')

        order = np.argsort(x, kind='stable')
        try:
            guess = np.asarray(func.guess_params(x[order], y[order]), float)
        except Exception:
            guess = np.ones(npar)
        if guess.size != npar or not np.all(np.isfinite(guess)):
            guess = np.ones(npar)
        base = np.asarray(p0, float) if p0 is not None and np.size(p0) == npar and np.all(np.isfinite(p0)) else guess

        rng = np.random.default_rng(12345)
        starts = [base] + [base + rng.standard_normal(npar) * np.maximum(np.abs(base), 1e-3) * 0.5
                           for _ in range(8)]
        best, best_obj, last_err = None, float('inf'), None
        for k, s0 in enumerate(starts):
            if k > 0 and best is not None and not func.multistart:
                break
            try:
                cand = FitEngine._fit_once(func, x, y, sig, xs, s0)
            except Exception as exc:
                last_err = exc
                continue
            obj = FitEngine._objective(func, cand[0], x, y, sig, xs)
            if np.isfinite(obj) and obj < best_obj:
                best, best_obj = cand, obj
        if best is None:
            raise FitError(f'the fit did not converge ({last_err}). Try different start values or another model.')
        popt, pcov, method, notes = best

        if pcov is None or not np.all(np.isfinite(pcov)):
            perr = np.full(npar, np.nan)
            pcov = None
            notes.append('parameter uncertainties could not be determined (model may be over-parameterised or poorly constrained)')
        else:
            perr = np.sqrt(np.clip(np.diag(pcov), 0, None))

        with np.errstate(all='ignore'):
            yfit = func.calc(x, *popt)
        resid = yfit - y
        ss_res = float(np.sum(resid ** 2))
        ss_tot = float(np.sum((y - y.mean()) ** 2))
        r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else float('nan')
        dof = max(1, n - npar)
        res = FitResult(popt=popt, perr=perr, pcov=pcov, n=n, dof=dof, method=method, r2=r2,
                        rms=float(np.sqrt(ss_res / n)), notes=notes)
        s = FitEngine.sigma_eff(func, popt, x, sig, xs)
        if sig is not None and s is not None:
            res.chi2 = float(np.sum((resid / s) ** 2))
            res.chi2_red = res.chi2 / dof
            res.pvalue = float(_stats.chi2.sf(res.chi2, dof))
            if res.chi2_red > 3:
                notes.append(f'χ²/ν = {res.chi2_red:.2f} ≫ 1: the model may not describe the data, or the uncertainties are underestimated')
            elif res.chi2_red < 0.3:
                notes.append(f'χ²/ν = {res.chi2_red:.2f} ≪ 1: uncertainties may be overestimated (or the fit is over-parameterised)')
        else:
            res.inferred = True
            res.s_hat = float(np.sqrt(ss_res / dof))
            notes.append(f'no ΔY given: uncertainties inferred from scatter (s = {res.s_hat:.4g}); parameter errors assume the model is correct')
        if pcov is not None and npar >= 2:
            d = np.sqrt(np.diag(pcov))
            with np.errstate(all='ignore'):
                corr = pcov / np.outer(d, d)
            iu = np.triu_indices(npar, 1)
            if np.any(np.abs(corr[iu]) > 0.99):
                notes.append('two parameters are almost perfectly correlated (|ρ| > 0.99): consider centring x or simplifying the model')
        return res


class FitThread(QThread):
    done = pyqtSignal(int, object)

    def __init__(self, token, args, parent=None):
        super().__init__(parent)
        self.token, self.args = token, args

    def run(self):
        try:
            out = FitEngine.perform(*self.args)
        except FitError as exc:
            out = str(exc)
        except BaseException as exc:
            out = f'{type(exc).__name__}: {exc}'
        self.done.emit(self.token, out)

@dataclass
class PlotData:
    data: np.ndarray
    title: str = ''
    xlabel: str = 'X'
    ylabel: str = 'Y'
    signature: str = ''
    curve: Optional[np.ndarray] = None
    curve_label: Optional[str] = None
    detail: Optional[str] = None
    stats: str = ''
    resid: Optional[np.ndarray] = None        # aligned with data rows
    resid_err: Optional[np.ndarray] = None    # error bars for unscaled residuals
    resid_scaled: bool = True
    xlog: bool = False
    ylog: bool = False


class MyMplWidget(FigureCanvas):
    def __init__(self, main_window, parent=None):
        self.main_window = main_window
        self.fig = Figure(figsize=(9, 6), dpi=90)
        super().__init__(self.fig)
        self.setParent(parent)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.has_plot = False
        self.mpl_connect('motion_notify_event', self._on_move)

    def placeholder(self, msg):
        self.fig.clf()
        ax = self.fig.add_subplot(111)
        ax.text(0.5, 0.5, msg, transform=ax.transAxes, ha='center', va='center', fontsize=14, color='#666')
        ax.axis('off')
        self.has_plot = False
        self.draw_idle()

    def plot(self, d: PlotData):
        try:
            self._plot(d)
            self.has_plot = True
        except Exception:
            traceback.print_exc()
            self.placeholder('Plot error - see console')

    def _plot(self, d: PlotData):
        data = d.data
        if data is None or data.ndim != 2 or data.shape[0] == 0:
            self.placeholder('No data to plot')
            return
        x, y = data[:, 0], data[:, 1]
        has_res = d.curve is not None and d.resid is not None
        self.fig.clf()
        if has_res:
            ax, ax2 = self.fig.subplots(2, 1, sharex=True, gridspec_kw=dict(height_ratios=[4, 1.3], hspace=0.38))
        else:
            ax, ax2 = self.fig.add_subplot(111), None

        def errs(col):
            e = data[:, col]
            if not np.any(np.isfinite(e)):
                return None
            return np.where(np.isfinite(e) & (e > 0), e, 0.0)
        xe, ye = errs(2), errs(3)
        ax.errorbar(x, y, xerr=xe, yerr=ye, fmt='o', ms=5, color='k', ecolor='#555555',
                    capsize=2, elinewidth=1, label='data', zorder=3)
        if d.curve is not None:
            ax.plot(d.curve[:, 0], d.curve[:, 1], '-', color='#d62728', lw=2, label=d.curve_label or 'model', zorder=2)
        if d.xlog:
            ax.set_xscale('log')
        if d.ylog:
            ax.set_yscale('log')
        ax.set_ylabel(d.ylabel or 'Y', fontsize=12)
        ax.set_title(d.title or '', fontsize=13)
        ax.grid(True, alpha=0.3)
        lo, hi = float(np.min(x)), float(np.max(x))
        if not d.xlog:
            m = (hi - lo) * 0.05 or max(1.0, abs(lo) * 0.1)
            ax.set_xlim(lo - m, hi + m)
        try:
            leg = ax.legend(loc='upper right', fontsize=10, framealpha=0.9)
            leg.set_draggable(True)
        except Exception:
            pass
        if d.detail:
            ann = ax.annotate(d.detail, xy=(0.02, 0.98), xycoords='axes fraction', va='top', ha='left', fontsize=10,
                              bbox=dict(boxstyle='round,pad=0.35', fc='white', alpha=0.85, ec='#cccccc'))
            try:
                ann.draggable(True)
            except Exception:
                pass
        if d.stats:
            ax.text(0.02, 0.02, d.stats, transform=ax.transAxes, va='bottom', ha='left', fontsize=10, color='#444')
        if d.signature:
            ax.text(0.98, 0.02, d.signature, transform=ax.transAxes, ha='right', va='bottom', fontsize=10, color='#555')

        if ax2 is not None:
            ax.tick_params(labelbottom=True)       # x axis + label stay under the main plot
            self._residuals(ax2, d)
        ax.set_xlabel(d.xlabel or 'X', fontsize=12)
        self.fig.subplots_adjust(left=0.09, right=0.97, top=0.93, bottom=0.08 if ax2 is not None else 0.11)
        self.draw_idle()

    def _residuals(self, ax, d: PlotData):
        x, r = d.data[:, 0], np.asarray(d.resid, float)
        ok = np.isfinite(r) & np.isfinite(x)
        x, r = x[ok], r[ok]
        ax.axhline(0, color='k', lw=0.8)
        ax.grid(True, alpha=0.3)
        if r.size == 0:
            return
        if d.resid_scaled:
            a = np.abs(r)
            for mask, col, lab in ((a <= 1, '#2ca02c', '|r| ≤ 1'), ((a > 1) & (a < 3), '#ff7f0e', '1 < |r| < 3'),
                                   (a >= 3, '#d62728', '|r| ≥ 3')):
                if mask.any():
                    ax.errorbar(x[mask], r[mask], yerr=1, fmt='o', ms=4, color=col, capsize=2, label=lab)
            for v in (-1, 1):
                ax.axhline(v, color='k', lw=0.6, ls=':')
            lim = max(3.0, 1.1 * (float(a.max()) + 1))
            ax.set_ylim(-lim, lim)
            ax.set_ylabel('(fit − data)/σ', fontsize=10)
            ax.legend(loc='best', fontsize=8, ncol=3, framealpha=0.85)
        else:
            e = None if d.resid_err is None else np.asarray(d.resid_err, float)[ok]
            ax.errorbar(x, r, yerr=e, fmt='ko', ms=4, capsize=2)
            lim = 1.1 * float(np.max(np.abs(r) + (0 if e is None else np.nan_to_num(e))))
            if np.isfinite(lim) and lim > 0:
                ax.set_ylim(-lim, lim)
            ax.set_ylabel('fit − data', fontsize=10)

    def _on_move(self, event):
        if event.xdata is not None and event.ydata is not None:
            self.main_window.statusBar().showMessage(f'x = {event.xdata:.5g},   y = {event.ydata:.5g}', 2000)

class ControlWidget(QWidget):
    status = pyqtSignal(str)

    def __init__(self, mpl_widget, argv, parent=None):
        super().__init__(parent)
        self.mpl = mpl_widget
        self.argv = list(argv)
        self.fitfunction: Optional[FitFunction] = None
        self.undo_stack = QUndoStack(self)
        self._token = 0
        self._threads: set = set()
        self._pending = None
        self._last = None
        self._build_ui()
        self._status_timer = QTimer(self)
        self._status_timer.setSingleShot(True)
        self._status_timer.setInterval(200)
        self._status_timer.timeout.connect(self._refresh_data_status)
        self.model.changed.connect(self._status_timer.start)
        self._refresh_data_status()
        
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(4, 4, 4, 4)
        split = QSplitter(Qt.Vertical)
        root.addWidget(split)

        top = QWidget()
        tl = QVBoxLayout(top)
        tl.setContentsMargins(0, 0, 0, 0)
        self.model = DataTableModel(undo_stack=self.undo_stack)
        self.view = SpreadsheetView()
        self.view.setModel(self.model)
        self.view.message.connect(self.status.emit)
        tl.addWidget(QLabel('<b>Data</b>  &nbsp; paste from Excel, type numbers or formulas (=A2*2). '
                            'Right-click for more.'))
        tl.addWidget(self.view, 1)

        roles = QHBoxLayout()
        self.cmb_role = {}
        for key in ROLE_KEYS:
            roles.addWidget(QLabel(ROLE_LABELS[key]))
            cmb = QComboBox()
            if key in ('dx', 'dy'):
                cmb.addItem('—', None)
            for i, L in enumerate(LETTERS):
                cmb.addItem(L, i)
            cmb.currentIndexChanged.connect(self._roles_changed)
            self.cmb_role[key] = cmb
            roles.addWidget(cmb)
        tl.addLayout(roles)
        self._set_default_roles('-x' in self.argv)

        box = QGroupBox('Calculate a column')
        g = QGridLayout(box)
        g.setContentsMargins(8, 6, 8, 6)
        g.setHorizontalSpacing(6)
        g.addWidget(QLabel('Put result in'), 0, 0)
        self.cmb_fill = QComboBox()
        for L in LETTERS:
            self.cmb_fill.addItem(L)
        self.cmb_fill.setCurrentIndex(3 if '-x' in self.argv else 2)
        g.addWidget(self.cmb_fill, 0, 1)
        self.ed_fill = QLineEdit()
        self.ed_fill.setPlaceholderText('= formula for the first row, e.g. 1/sqrt(A2)')
        self.ed_fill.setStyleSheet('font-family: monospace;')
        self.ed_fill.textChanged.connect(self._fill_preview)
        self.ed_fill.returnPressed.connect(self._do_fill)
        g.addWidget(self.ed_fill, 0, 2, 1, 3)
        btn = QPushButton('Fill column')
        btn.clicked.connect(self._do_fill)
        g.addWidget(btn, 0, 5)
        self.cmb_preset = QComboBox()
        for label, _, _ in PRESETS:
            self.cmb_preset.addItem(label)
        g.addWidget(self.cmb_preset, 1, 0, 1, 2)
        g.addWidget(QLabel('A ='), 1, 2, Qt.AlignRight)
        self.cmb_a = QComboBox()
        self.cmb_b = QComboBox()
        for cb in (self.cmb_a, self.cmb_b):
            for L in LETTERS:
                cb.addItem(L)
        self.cmb_b.setCurrentIndex(1)
        g.addWidget(self.cmb_a, 1, 3)
        g.addWidget(QLabel('B ='), 1, 4, Qt.AlignRight)
        g.addWidget(self.cmb_b, 1, 5)
        for cb in (self.cmb_preset, self.cmb_a, self.cmb_b):
            cb.currentIndexChanged.connect(self._apply_preset)
        g.setColumnStretch(2, 1)
        self.lbl_fill = QLabel('')
        self.lbl_fill.setStyleSheet('font-family: monospace; color: #555;')
        self.lbl_fill.setWordWrap(True)
        g.addWidget(self.lbl_fill, 2, 0, 1, 6)
        tl.addWidget(box)

        self.lbl_data = QLabel('')
        self.lbl_data.setWordWrap(True)
        tl.addWidget(self.lbl_data)
        split.addWidget(top)

        bottom = QWidget()
        bl = QVBoxLayout(bottom)
        bl.setContentsMargins(0, 0, 0, 0)
        form = QFormLayout()
        self.ed_title = QLineEdit('Plot title')
        self.ed_xlabel = QLineEdit('X')
        self.ed_ylabel = QLineEdit('Y')
        self.ed_name = QLineEdit('Name, date')
        for lab, ed in (('Title', self.ed_title), ('X label', self.ed_xlabel),
                        ('Y label', self.ed_ylabel), ('Signature', self.ed_name)):
            form.addRow(lab, ed)
        bl.addLayout(form)

        opts = QHBoxLayout()
        self.cb_xlog = QCheckBox('log X')
        self.cb_ylog = QCheckBox('log Y')
        self.cb_scaled = QCheckBox('scaled residuals')
        self.cb_scaled.setChecked(True)
        self.cb_infer = QCheckBox('infer ΔY if missing')
        self.cb_infer.setChecked('-i' in self.argv)
        self.cb_infer.setToolTip('If no ΔY column is given, estimate the uncertainty from the scatter (same as -i).')
        for cb in (self.cb_xlog, self.cb_ylog, self.cb_scaled):
            cb.toggled.connect(self._rerender)
            opts.addWidget(cb)
        opts.addWidget(self.cb_infer)
        bl.addLayout(opts)

        b1 = QHBoxLayout()
        self.btn_plot = QPushButton('Plot data')
        self.btn_plot.clicked.connect(self.on_plot)
        b1.addWidget(self.btn_plot)
        btn_clear = QPushButton('Clear data')
        btn_clear.setToolTip('Clears all cells (can be undone with Ctrl+Z)')
        btn_clear.clicked.connect(lambda: self.model.clear_all())
        b1.addWidget(btn_clear)
        bl.addLayout(b1)

        fit = QFormLayout()
        self.combo = QComboBox()
        self.combo.addItem('— no fit —')
        for f in FIT_FUNCTIONS:
            self.combo.addItem(f.name)
        self.combo.currentIndexChanged.connect(self._on_model_changed)
        fit.addRow('Model', self.combo)
        self.param_edits, self.param_labels = [], []
        for _ in range(4):
            lbl, ed = QLabel('—'), QLineEdit()
            ed.setPlaceholderText('auto')
            ed.editingFinished.connect(self._validate_params)
            ed.returnPressed.connect(self.on_fit)
            self.param_edits.append(ed)
            self.param_labels.append(lbl)
            fit.addRow(lbl, ed)
        bl.addLayout(fit)

        b2 = QHBoxLayout()
        self.btn_model = QPushButton('Plot model')
        self.btn_model.setToolTip('Draw the model with the start values above (blank = automatic guess)')
        self.btn_model.clicked.connect(self.on_plot_model)
        self.btn_fit = QPushButton('Fit  (Ctrl+Enter)')
        self.btn_fit.clicked.connect(self.on_fit)
        self.btn_cancel = QPushButton('Cancel')
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.clicked.connect(self.cancel_fit)
        for b in (self.btn_model, self.btn_fit, self.btn_cancel):
            b2.addWidget(b)
        bl.addLayout(b2)

        self.report = QPlainTextEdit()
        self.report.setReadOnly(True)
        f = QFont('monospace')
        f.setStyleHint(QFont.Monospace)
        self.report.setFont(f)
        self.report.setPlaceholderText('Fit report appears here.')
        self.report.setMinimumHeight(120)
        bl.addWidget(self.report, 1)
        btn_copy = QPushButton('Copy report')
        btn_copy.clicked.connect(lambda: QApplication.clipboard().setText(self.report.toPlainText()))
        bl.addWidget(btn_copy)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(bottom)
        split.addWidget(scroll)
        split.setSizes([430, 420])

    def _set_default_roles(self, with_dx: bool):
        vals = {'x': 0, 'y': 1, 'dx': 2 if with_dx else None, 'dy': 3 if with_dx else 2}
        for k, cmb in self.cmb_role.items():
            cmb.blockSignals(True)
            cmb.setCurrentIndex(cmb.findData(vals[k]))
            cmb.blockSignals(False)
        self.model.set_roles(vals)

    def _roles_changed(self):
        roles = {k: cmb.currentData() for k, cmb in self.cmb_role.items()}
        self.model.set_roles(roles)
        if roles['x'] == roles['y']:
            self.status.emit('Warning: X and Y use the same column')

    def _apply_preset(self):
        _, tmpl, nargs = PRESETS[self.cmb_preset.currentIndex()]
        self.cmb_b.setEnabled(nargs == 2)
        if tmpl is None:
            return
        ca, cb = self.cmb_a.currentIndex(), self.cmb_b.currentIndex()
        def first(c):
            return next((r + 1 for r in range(len(self.model._raw)) if self.model._raw[r][c] is not None), 1)
        row = max(first(ca), first(cb)) if nargs == 2 else first(ca)
        self.ed_fill.setText('=' + tmpl.format(a=f'{LETTERS[ca]}{row}', b=f'{LETTERS[cb]}{row}'))

    def _fill_preview(self):
        t = self.ed_fill.text().strip()
        if not t:
            self.lbl_fill.setText('')
            return
        t = t if t.startswith('=') else '=' + t
        refs = list(_REF_RE.finditer(t))
        base = min((int(m.group(4)) for m in refs), default=1)
        try:
            v = evaluate_expression(t, lambda r, c: self.model._ref(r, c, set()))
            self.lbl_fill.setText(f'row {base}:  {t}  →  {v:.6g}')
        except CellError as exc:
            self.lbl_fill.setText(f'#ERR: {exc}')

    def _do_fill(self):
        t = self.ed_fill.text().strip()
        if not t:
            self.status.emit('Type a formula first')
            return
        try:
            n = self.model.fill_formula(self.cmb_fill.currentIndex(), t)
            if n:
                self.status.emit(f'Filled {n} row(s) in column {self.cmb_fill.currentText()}')
        except CellError as exc:
            show_warn(f'Could not fill column: {exc}')

    def _refresh_data_status(self):
        try:
            ex = self.model.extract()
        except Exception as exc:
            self.lbl_data.setText(f'Data error: {exc}')
            return
        msg = f'{ex.n} point(s) ready.'
        parts = ex.issues + ex.notes
        has_dy = ex.n and np.any(np.isfinite(ex.data[:, 3]))
        if ex.n and not has_dy and not self.cb_infer.isChecked():
            parts.append('no ΔY values - fitting needs them (or tick "infer ΔY if missing")')
        self.lbl_data.setText(msg + ('  ' + ' • '.join(parts) if parts else ''))
        self.lbl_data.setStyleSheet('color: #b00020;' if ex.issues else 'color: #444;')

    def _on_model_changed(self):
        if self._threads:
            self.cancel_fit()
        i = self.combo.currentIndex()
        self.fitfunction = FIT_FUNCTIONS[i - 1] if i > 0 else None
        for k, (lbl, ed) in enumerate(zip(self.param_labels, self.param_edits)):
            ed.clear()
            ed.setStyleSheet('')
            if self.fitfunction and k < len(self.fitfunction.param_names):
                u = self.fitfunction.param_units[k]
                lbl.setText(f'{self.fitfunction.param_names[k]}' + (f'  [{u}]' if u else ''))
                ed.setEnabled(True)
            else:
                lbl.setText('—')
                ed.setEnabled(False)

    @staticmethod
    def _param_value(text) -> Optional[float]:
        t = (text or '').strip()
        if not t:
            return None
        v = parse_number(t)
        if v is not None:
            return v
        try:
            return evaluate_expression(t, no_cells)        # allows 1e-3, pi/2, 3*0.5
        except CellError:
            return float('nan')

    def _validate_params(self):
        if not self.fitfunction:
            return
        for k in range(len(self.fitfunction.param_names)):
            v = self._param_value(self.param_edits[k].text())
            bad = v is not None and not np.isfinite(v)
            self.param_edits[k].setStyleSheet('background: #ffd6d6; color: #000;' if bad else '')

    def _start_values(self, ex: Extracted) -> Optional[np.ndarray]:
        """User start values; blanks filled by the automatic guess. None if a value is invalid."""
        func = self.fitfunction
        vals = [self._param_value(self.param_edits[k].text()) for k in range(len(func.param_names))]
        if any(v is not None and not np.isfinite(v) for v in vals):
            show_warn('A start value is not a valid number.')
            return None
        order = np.argsort(ex.data[:, 0], kind='stable')
        try:
            guess = np.asarray(func.guess_params(ex.data[order, 0], ex.data[order, 1]), float)
            if guess.size != len(vals) or not np.all(np.isfinite(guess)):
                raise ValueError
        except Exception:
            guess = np.ones(len(vals))
        return np.array([g if v is None else v for v, g in zip(vals, guess)], float)

    def _collect(self):
        self._commit_editor()
        ex = self.model.extract()
        self._refresh_data_status()
        return ex

    def _commit_editor(self):
        if self.view.state() == QAbstractItemView.EditingState:
            self.btn_fit.setFocus()          # focus-out commits the open cell editor

    def _uncertainties(self, ex: Extracted, need_dy: bool):
        """Returns (sigma, xsigma, notes) or raises FitError with a clear message."""
        notes = []
        out = []
        for col, nm in ((3, 'ΔY'), (2, 'ΔX')):
            v = ex.data[:, col]
            fin = np.isfinite(v)
            if not fin.any():
                out.append(None)
                continue
            if np.any(fin & (v <= 0)):
                rows = ex.rows[fin & (v <= 0)][:5]
                raise FitError(f'{nm} must be greater than zero (see row(s) {", ".join(map(str, rows))})')
            if not fin.all():
                if nm == 'ΔY' and self.cb_infer.isChecked():
                    notes.append('ΔY incomplete: ignored, errors inferred from scatter')
                    out.append(None)
                    continue
                raise FitError(f'{nm} is missing for row(s) {", ".join(map(str, ex.rows[~fin][:5]))}. '
                               f'Fill it in{" or tick infer" if nm == "ΔY" else ""}.')
            out.append(v)
        sigma, xsigma = out
        if sigma is None and need_dy and not self.cb_infer.isChecked():
            raise FitError('no ΔY values. Provide a ΔY column (or tick "infer ΔY if missing").')
        return sigma, xsigma, notes

    def _build_plot(self, ex, func=None, popt=None, fit: Optional[FitResult] = None, sigma=None, xsigma=None) -> PlotData:
        d = PlotData(data=ex.data, title=self.ed_title.text(), xlabel=self.ed_xlabel.text() or 'X',
                     ylabel=self.ed_ylabel.text() or 'Y', signature=self.ed_name.text(),
                     resid_scaled=self.cb_scaled.isChecked(),
                     xlog=self.cb_xlog.isChecked(), ylog=self.cb_ylog.isChecked())
        x = ex.data[:, 0]
        if d.xlog and np.any(x <= 0):
            d.xlog = False
            self.status.emit('log X ignored: some X values are ≤ 0')
        if d.ylog and np.any(ex.data[:, 1] <= 0):
            d.ylog = False
            self.status.emit('log Y ignored: some Y values are ≤ 0')
        if func is not None and popt is not None:
            lo, hi = float(np.min(x)), float(np.max(x))
            xs = np.geomspace(lo, hi, 400) if d.xlog and hi > lo else np.linspace(lo, hi, 400)
            with np.errstate(all='ignore'):
                ys = func.calc(xs, *popt)
                yf = func.calc(x, *popt)
            if np.all(np.isfinite(ys)) and np.all(np.isfinite(yf)):
                d.curve = np.column_stack([xs, ys])
                d.curve_label = func.legend(popt)
                resid = yf - ex.data[:, 1]
                s = FitEngine.sigma_eff(func, popt, x, sigma, xsigma)
                if d.resid_scaled:
                    if s is not None:
                        d.resid = resid / s
                    elif fit is not None and fit.inferred and fit.s_hat > 0:
                        d.resid = resid / fit.s_hat
                    else:
                        d.resid_scaled = False
                        d.resid = resid
                else:
                    d.resid, d.resid_err = resid, s
            else:
                self.status.emit('The model produced non-finite values for these parameters')
        if fit is not None:
            d.detail = func.detailed_text(fit.popt, fit.perr)
            d.stats = f'R² = {fit.r2:.5g}' + ('' if fit.inferred else f'   χ²/ν = {fit.chi2_red:.3g}   p = {fit.pvalue:.2g}') + f'   ν = {fit.dof}'
        return d

    def _show(self, d, state):
        self._last = state
        self.mpl.plot(d)

    def _rerender(self):
        if self._last is None:
            return
        try:
            ex, func, popt, fit, sigma, xsigma = self._last
            self.mpl.plot(self._build_plot(ex, func, popt, fit, sigma, xsigma))
        except Exception:
            traceback.print_exc()

    def on_plot(self):
        try:
            ex = self._collect()
            if ex.n == 0:
                self.mpl.placeholder('No data to plot')
                self.status.emit('No numeric X/Y data in the chosen columns')
                return
            sigma = xsigma = None
            self._show(self._build_plot(ex), (ex, None, None, None, None, None))
        except Exception as exc:
            show_warn(f'Could not plot: {exc}')

    def on_plot_model(self):
        try:
            if self.fitfunction is None:
                show_info('Select a model first.')
                return
            ex = self._collect()
            if ex.n == 0:
                self.status.emit('No data to plot')
                return
            p = self._start_values(ex)
            if p is None:
                return
            try:
                sigma, xsigma, _ = self._uncertainties(ex, need_dy=False)
            except FitError:
                sigma = xsigma = None
            self._show(self._build_plot(ex, self.fitfunction, p, None, sigma, xsigma),
                       (ex, self.fitfunction, p, None, sigma, xsigma))
            self.report.setPlainText('Model preview with the start values:\n' +
                                     '\n'.join(f'  {n} = {v:.6g}' for n, v in zip(self.fitfunction.param_names, p)))
        except Exception as exc:
            show_warn(f'Could not plot model: {exc}')

    def on_fit(self):
        if self._threads:
            return
        try:
            func = self.fitfunction
            if func is None:
                show_info('Select a fit model first.')
                return
            ex = self._collect()
            npar = len(func.param_names)
            if ex.n <= npar:
                show_info(f'Not enough points: this model has {npar} parameter(s) and needs more than that many points (have {ex.n}).')
                return
            try:
                sigma, xsigma, notes = self._uncertainties(ex, need_dy=True)
            except FitError as exc:
                show_warn(str(exc))
                return
            p0 = self._start_values(ex)
            if p0 is None:
                return
            have_user = any(self.param_edits[k].text().strip() for k in range(npar))
            self._token += 1
            th = FitThread(self._token, (func, ex.data[:, 0], ex.data[:, 1], sigma, xsigma, p0 if have_user else None))
            th.done.connect(self._on_fit_done)
            th.finished.connect(lambda t=th: self._threads.discard(t))
            self._threads.add(th)
            self._pending = (self._token, ex, func, sigma, xsigma, notes)
            self.btn_fit.setEnabled(False)
            self.btn_cancel.setEnabled(True)
            self.status.emit('Fitting…')
            th.start()
        except Exception as exc:
            traceback.print_exc()
            show_warn(f'Could not start fit: {exc}')

    def _on_fit_done(self, token, out):
        if token != self._token or self._pending is None:
            return                                   # cancelled / stale
        _, ex, func, sigma, xsigma, notes = self._pending
        self._pending = None
        self.btn_fit.setEnabled(True)
        self.btn_cancel.setEnabled(False)
        if isinstance(out, str):
            self.report.setPlainText(f'Fit failed:\n{out}')
            self.status.emit('Fit failed')
            show_warn(f'Fit failed: {out}')
            return
        try:
            res: FitResult = out
            res.notes = notes + res.notes
            for k, v in enumerate(res.popt):
                if k < len(self.param_edits) and np.isfinite(v):
                    self.param_edits[k].setText(f'{v:#.6g}')
                    self.param_edits[k].setStyleSheet('')
            self.report.setPlainText(self._format_report(func, res))
            self._show(self._build_plot(ex, func, res.popt, res, sigma, xsigma), (ex, func, res.popt, res, sigma, xsigma))
            self.status.emit(f'Fit done: R² = {res.r2:.5g}' + ('' if res.inferred else f', χ²/ν = {res.chi2_red:.3g}'))
        except Exception as exc:
            traceback.print_exc()
            show_warn(f'Fit finished but the result could not be displayed: {exc}')

    @staticmethod
    def _format_report(func: FitFunction, r: FitResult) -> str:
        L = [f'Model:   {func.name}', f'Method:  {r.method}', f'Points:  {r.n}   (ν = {r.dof})', '']
        for n, u, p, e in zip(func.param_names, func.param_units, r.popt, r.perr):
            err = f'± {e:.3g}' if np.isfinite(e) else '± n/a'
            L.append(f'  {n:<8} = {p:.6g} {err}  {u}'.rstrip())
        L.append('')
        if r.inferred:
            L.append(f'Residual scatter s = {r.s_hat:.4g}   (χ² not available without ΔY)')
        else:
            L.append(f'χ² = {r.chi2:.4g},  χ²/ν = {r.chi2_red:.3g},  p = {r.pvalue:.3g}')
        L.append(f'R² = {r.r2:.6g},   RMS residual = {r.rms:.4g}')
        if r.notes:
            L += ['', 'Notes:'] + [f'  • {n}' for n in r.notes]
        return '\n'.join(L)

    def cancel_fit(self):
        if self._threads or self._pending:
            self._token += 1                         # result will be ignored
            self._pending = None
            self.status.emit('Fit cancelled')
        self.btn_fit.setEnabled(True)
        self.btn_cancel.setEnabled(False)

    def wait_for_threads(self, ms=5000):
        for t in list(self._threads):
            t.wait(ms)

class MyMainWindow(QMainWindow):
    def __init__(self, argv, x=None, y=None, xerr=None, yerr=None):
        super().__init__()
        argv = list(argv)
        if xerr is not None and '-x' not in argv:
            argv.append('-x')
        self.mpl_widget = MyMplWidget(self)
        self.control = ControlWidget(self.mpl_widget, argv)
        self.control.status.connect(lambda m: self.statusBar().showMessage(m, 6000))
        split = QSplitter(Qt.Horizontal)
        split.addWidget(self.control)
        plot_panel = QWidget()
        pv = QVBoxLayout(plot_panel)
        pv.setContentsMargins(0, 0, 0, 0)
        pv.addWidget(NavigationToolbar(self.mpl_widget, plot_panel))
        pv.addWidget(self.mpl_widget, 1)
        split.addWidget(plot_panel)
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        split.setSizes([470, 760])
        self.setCentralWidget(split)
        self.mpl_widget.placeholder('Enter or paste data, then press "Plot data"')

        self.sel_label = QLabel('')
        self.statusBar().addPermanentWidget(self.sel_label)
        self.control.view.selectionModel().selectionChanged.connect(self._update_selection_stats)
        self._build_menus()
        self.setWindowTitle('Plot & Fit — Labs')
        self.resize(1250, 850)
        if x is not None or y is not None:
            self._load_arrays(x, y, xerr, yerr)
        self.control.undo_stack.setClean()
        
    def _act(self, menu, text, shortcut, slot):
        a = QAction(text, self)
        if shortcut:
            a.setShortcut(QKeySequence(shortcut))
        a.triggered.connect(lambda *_: slot())
        menu.addAction(a)
        return a

    def _build_menus(self):
        c, v, m = self.control, self.control.view, self.menuBar()
        f = m.addMenu('&File')
        self._act(f, '&New', 'Ctrl+N', self.handle_new)
        self._act(f, '&Open CSV…', 'Ctrl+O', self.handle_open)
        self._act(f, '&Save CSV…', 'Ctrl+Shift+S', self.handle_save_csv)
        f.addSeparator()
        self._act(f, 'Save &plot…', 'Ctrl+S', self.handle_save_plot)
        self._act(f, '&Print plot…', 'Ctrl+P', self.handle_print)
        f.addSeparator()
        self._act(f, '&Quit', 'Ctrl+Q', self.close)
        e = m.addMenu('&Edit')
        undo = c.undo_stack.createUndoAction(self, 'Undo')
        undo.setShortcut(QKeySequence('Ctrl+Z'))
        redo = c.undo_stack.createRedoAction(self, 'Redo')
        redo.setShortcuts([QKeySequence('Ctrl+Y'), QKeySequence('Ctrl+Shift+Z')])
        e.addAction(undo)
        e.addAction(redo)
        e.addSeparator()
        self._act(e, 'Copy\tCtrl+C', None, v.copy_selection)
        self._act(e, 'Copy as formulas', None, lambda: v.copy_selection(True))
        self._act(e, 'Cut\tCtrl+X', None, v.cut_selection)
        self._act(e, 'Paste\tCtrl+V', None, v.paste_clipboard)
        self._act(e, 'Clear cells\tDel', None, v.clear_selection)
        self._act(e, 'Fill down\tCtrl+D', None, v.fill_down_selection)
        e.addSeparator()
        self._act(e, 'Clear all data', None, c.model.clear_all)
        r = m.addMenu('F&it')
        self._act(r, '&Plot data', 'Ctrl+Shift+P', c.on_plot)
        self._act(r, 'Plot &model', None, c.on_plot_model)
        self._act(r, '&Run fit', 'Ctrl+Return', c.on_fit)
        self._act(r, '&Cancel fit', 'Ctrl+.', c.cancel_fit)
        h = m.addMenu('&Help')
        self._act(h, '&Formulas && shortcuts', None, self.handle_help)
        
    def _load_arrays(self, x, y, xerr, yerr):
        try:
            cols = []
            with_dx = xerr is not None
            for arr in ([x, y, xerr, yerr] if with_dx else [x, y, yerr]):
                cols.append(None if arr is None else np.asarray(arr, float).ravel())
            n = max((len(a) for a in cols if a is not None), default=0)
            rows = [[('' if a is None or r >= len(a) or not np.isfinite(a[r]) else f'{a[r]:.12g}')
                     for a in cols] for r in range(n)]
            self.control._set_default_roles(with_dx)
            self.control.model.replace_all(rows, 'load data')
        except Exception as exc:
            show_warn(f'Could not load the supplied data: {exc}')

    def handle_new(self):
        if not self._confirm_discard():
            return
        c = self.control
        c.model.clear_all()
        c.undo_stack.clear()
        c.ed_title.setText('Plot title')
        c.ed_xlabel.setText('X')
        c.ed_ylabel.setText('Y')
        c.ed_name.setText('Name, date')
        c.report.clear()
        c._last = None
        self.mpl_widget.placeholder('Enter or paste data, then press "Plot data"')
        c.undo_stack.setClean()

    def handle_open(self):
        if not self._confirm_discard():
            return
        path, _ = QFileDialog.getOpenFileName(self, 'Open data', '', 'Data (*.csv *.txt *.tsv *.dat);;All files (*)')
        if path:
            self.open_path(path)

    def open_path(self, path: str):
        try:
            with open(path, 'r', encoding='utf-8-sig', errors='replace') as fh:
                text = fh.read()
            sample = text[:4096]
            try:
                dialect = csv.Sniffer().sniff(sample, delimiters=',;\t ')
            except csv.Error:
                dialect = csv.excel
            rows = [[clean_cell(c) for c in row] for row in csv.reader(io.StringIO(text), dialect)]
            rows = [r for r in rows if any(c is not None for c in r)]
            if not rows:
                show_warn('The file is empty.')
                return
            first = rows[0]
            if all(c is None or (parse_number(c) is None and not c.startswith('=')) for c in first):
                rows = rows[1:]
            if len(rows) > MAX_ROWS:
                show_warn(f'File has {len(rows)} rows; only the first {MAX_ROWS} are loaded.')
            if max(len(r) for r in rows) > NCOLS:
                show_warn(f'File has more than {NCOLS} columns; extra columns are ignored.')
            self.control.model.replace_all(rows, 'open file')
            self.control.undo_stack.clear()
            self.control.undo_stack.setClean()
            self.statusBar().showMessage(f'Loaded {len(rows)} rows from {os.path.basename(path)}', 5000)
            self.control.on_plot()
        except Exception as exc:
            show_warn(f'Could not open file: {exc}')

    def handle_save_csv(self) -> bool:
        path, _ = QFileDialog.getSaveFileName(self, 'Save data', 'data.csv', 'CSV (*.csv)')
        if not path:
            return False
        if not os.path.splitext(path)[1]:
            path += '.csv'
        try:
            m = self.control.model
            last = m.last_row()
            used = [c for c in range(NCOLS) if any(m.raw(r, c) is not None for r in range(last))] or [0, 1]
            head = {c: next((ROLE_LABELS[k] for k in ROLE_KEYS if m.roles.get(k) == c), LETTERS[c]) for c in used}
            with open(path, 'w', newline='', encoding='utf-8') as fh:
                w = csv.writer(fh)
                w.writerow([head[c] for c in used])
                for r in range(last):
                    w.writerow([m.display_text(r, c) for c in used])
            self.control.undo_stack.setClean()
            self.statusBar().showMessage(f'Saved {last} rows to {path}', 5000)
            return True
        except Exception as exc:
            show_warn(f'Could not save: {exc}')
            return False

    def handle_save_plot(self):
        if not self.mpl_widget.has_plot:
            show_warn('Nothing to save yet - plot something first.')
            return
        path, filt = QFileDialog.getSaveFileName(self, 'Save plot', 'plot.png',
                                                 'PNG (*.png);;PDF (*.pdf);;SVG (*.svg)')
        if not path:
            return
        if not os.path.splitext(path)[1]:
            path += '.' + (re.search(r'\*\.(\w+)', filt) or re.search(r'(png)', 'png')).group(1)
        try:
            self.mpl_widget.fig.savefig(path, dpi=300, bbox_inches='tight')
            self.statusBar().showMessage(f'Saved {path}', 5000)
        except Exception as exc:
            show_warn(f'Could not save plot: {exc}')

    def handle_print(self):
        if not self.mpl_widget.has_plot:
            show_warn('Nothing to print yet - plot something first.')
            return
        try:
            printer = QPrinter(QPrinter.HighResolution)
            if QPrintDialog(printer, self).exec_() != QPrintDialog.Accepted:
                return
            buf = io.BytesIO()
            self.mpl_widget.fig.savefig(buf, format='png', dpi=200, bbox_inches='tight')
            img = QImage.fromData(buf.getvalue())
            painter = QPainter(printer)
            page = printer.pageRect()
            scaled = img.scaled(page.width(), page.height(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
            painter.drawImage(0, 0, scaled)
            painter.end()
        except Exception as exc:
            show_warn(f'Printing failed: {exc}')

    def _update_selection_stats(self, *_):
        try:
            rc = self.control.view.rect()
            idxs = self.control.view.selectedIndexes()
            if rc is None or len(idxs) < 2 or len(idxs) > 50000:
                self.sel_label.setText('')
                return
            vals = self.control.model.selection_values([(i.row(), i.column()) for i in idxs])
            if vals.size == 0:
                self.sel_label.setText('')
                return
            sd = f'{vals.std(ddof=1):.5g}' if vals.size > 1 else 'n/a'
            self.sel_label.setText(f'Count {vals.size}   Sum {vals.sum():.6g}   Mean {vals.mean():.6g}   '
                                   f'Std {sd}   Min {vals.min():.6g}   Max {vals.max():.6g}   ')
        except Exception:
            self.sel_label.setText('')

    def handle_help(self):
        show_info(
            'Cells accept numbers or formulas:\n'
            '  =A2*2     =1/sqrt(B3)     =(A2+A3)/2     A2^2     =$A$1*B2\n'
            'Functions: sqrt abs exp ln log10 log2 sin cos tan asin acos atan atan2 sinh cosh tanh\n'
            '           min max round floor ceil pow rad deg   Constants: pi e tau\n'
            '(log = natural log, same as ln)\n\n'
            'Ctrl+V paste   Ctrl+C copy values   Ctrl+X cut   Del clear   Ctrl+D fill down\n'
            'Ctrl+Z undo   Ctrl+Y redo   Enter commits and moves down\n'
            'Ctrl+Enter run fit   Ctrl+. cancel fit\n\n'
            'Pick which column is X, Y, ΔX, ΔY with the role boxes.\n'
            '"Fill column": write a formula for the first row, e.g. =1/sqrt(A2), and fill the column.\n'
            'ΔY / ΔX values left blank are copied from the row above.\n'
            'A ΔX column switches the fit to orthogonal distance regression.')

    def _confirm_discard(self) -> bool:
        c = self.control
        if c.undo_stack.isClean() or c.model.last_row() == 0:
            return True
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Question)
        box.setWindowTitle('Unsaved data')
        box.setText('The table has unsaved changes.')
        save = box.addButton('Save CSV…', QMessageBox.AcceptRole)
        disc = box.addButton('Discard', QMessageBox.DestructiveRole)
        box.addButton(QMessageBox.Cancel)
        box.exec_()
        if box.clickedButton() is save:
            return self.handle_save_csv()
        return box.clickedButton() is disc

    def closeEvent(self, event):
        if not self._confirm_discard():
            event.ignore()
            return
        self.control.cancel_fit()
        self.control.wait_for_threads(5000)
        event.accept()

_WINDOWS = []


def main(x=None, y=None, xerr=None, yerr=None):
    app = QApplication.instance() or QApplication(sys.argv)
    
    if sys.platform == 'win32':
        import ctypes
        myappid = 'uob.labfit.plotfit.1.0'
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(myappid)
    elif sys.platform == 'darwin':
        try:
            from AppKit import NSApplication, NSApplicationActivationPolicyRegular
            NSApplication.sharedApplication().setActivationPolicy_(NSApplicationActivationPolicyRegular)
        except ImportError:
            pass

    try:
        app.setStyle('Fusion')
    except Exception:
        pass
    
    w = MyMainWindow(sys.argv, x=x, y=y, xerr=xerr, yerr=yerr)
    _WINDOWS.append(w)
    
    from PyQt5.QtGui import QIcon
    icon = QIcon(resource_path('app.ico'))
    w.setWindowIcon(icon)
    
    w.show()
    
    if sys.platform == 'win32':
        w.setWindowIcon(icon)
        try:
            ctypes.windll.user32.SetWindowPos(int(w.winId()), -1, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0040)
        except Exception:
            pass

    return app.exec_()

if __name__ == '__main__':
    sys.exit(main())
