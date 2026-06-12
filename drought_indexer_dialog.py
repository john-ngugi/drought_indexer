# -*- coding: utf-8 -*-
import os
import sys
import subprocess
import json
import math
import urllib.parse
import datetime

from qgis.PyQt import uic
from qgis.PyQt import QtWidgets
from qgis.PyQt.QtCore import QThread, pyqtSignal, QSettings, Qt
from qgis.core import QgsProject, QgsMapLayerType, QgsWkbTypes, QgsRasterLayer

FORM_CLASS, _ = uic.loadUiType(os.path.join(
    os.path.dirname(__file__), 'drought_indexer_dialog_base.ui'))


# ─────────────────────────────────────────────── package installer ────────────

def _ensure_package(pip_name, import_name=None):
    try:
        __import__(import_name or pip_name)
        return True
    except ImportError:
        try:
            subprocess.check_call(
                [sys.executable, '-m', 'pip', 'install', '--quiet', pip_name],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        except Exception:
            return False


# ──────────────────────────────────── pure-Python analysis helpers ─────────────

STATE_LABELS  = ['Extreme Drought', 'Moderate Drought', 'Mild Drought', 'No Drought']
STATE_COLOURS = ['#8B0000', '#FF4500', '#FFA500', '#2e7d32']


def _nan(v):
    return v is None or (isinstance(v, float) and math.isnan(v))


def _classify_label(v):
    if _nan(v):    return 'No data'
    if v < -0.5:   return STATE_LABELS[0]
    if v < 0:      return STATE_LABELS[1]
    if v < 0.5:    return STATE_LABELS[2]
    return STATE_LABELS[3]


def _classify_state(v):
    if _nan(v):    return None
    if v < -0.5:   return 0
    if v < 0:      return 1
    if v < 0.5:    return 2
    return 3


def _bar_colour(v):
    s = _classify_state(v)
    return STATE_COLOURS[s] if s is not None else '#aaaaaa'


def _compute_trend(years, values):
    """OLS linear regression. Returns (slope, intercept, r²) or (None,None,None)."""
    pairs = [(x, y) for x, y in zip(years, values) if not _nan(y)]
    if len(pairs) < 2:
        return None, None, None
    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]
    xm = sum(xs) / len(xs)
    ym = sum(ys) / len(ys)
    ss_xy = sum((x - xm) * (y - ym) for x, y in zip(xs, ys))
    ss_xx = sum((x - xm) ** 2 for x in xs)
    if ss_xx == 0:
        return 0.0, ym, 0.0
    slope = ss_xy / ss_xx
    ic    = ym - slope * xm
    yp    = [slope * x + ic for x in xs]
    ss_res = sum((y - p) ** 2 for y, p in zip(ys, yp))
    ss_tot = sum((y - ym) ** 2 for y in ys)
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
    return slope, ic, r2


def _compute_spi(precip_vals):
    """Standardise raw seasonal precipitation to SPI."""
    valid = [v for v in precip_vals if not _nan(v)]
    if len(valid) < 2:
        return [None] * len(precip_vals)
    mu  = sum(valid) / len(valid)
    var = sum((v - mu) ** 2 for v in valid) / (len(valid) - 1)
    std = math.sqrt(var) if var > 0 else 0.0
    if std == 0:
        return [0.0 if not _nan(v) else None for v in precip_vals]
    return [(v - mu) / std if not _nan(v) else None for v in precip_vals]


def _compute_markov(years, sadi_vals):
    """4-state Markov chain from SADI series."""
    states = [_classify_state(v) for v in sadi_vals]
    pairs  = [(states[i], states[i + 1])
              for i in range(len(states) - 1)
              if states[i] is not None and states[i + 1] is not None]
    n = 4
    counts = [[0] * n for _ in range(n)]
    for f, t in pairs:
        counts[f][t] += 1
    prob_matrix = [
        [c / sum(row) for c in row] if sum(row) > 0 else [0.25] * n
        for row in counts
    ]
    last = next((s for s in reversed(states) if s is not None), None)
    return {
        'transition_matrix': prob_matrix,
        'current_state':     last,
        'next_state_probs':  prob_matrix[last] if last is not None else [0.25] * n,
        'states':            states,
    }


def _trend_desc(slope, r2):
    if slope is None:
        return 'insufficient data'
    if abs(slope) < 0.005:
        return f'stable  (slope = {slope:+.4f}/yr)'
    word  = 'worsening' if slope < 0 else 'improving'
    speed = 'strongly' if abs(slope) > 0.03 else 'gradually'
    return f'{speed} {word}  (slope = {slope:+.4f}/yr,  R² = {r2:.2f})'


def _generate_text_summary(rd):
    years = rd['years'];   sadi = rd['sadi'];   ndvi = rd['ndvi']
    spi   = rd['spi'];     season = rd['season_label']
    markov = rd['markov']; next_yr = rd['next_year']
    st = rd['sadi_trend']; nt = rd['ndvi_trend']; pt = rd['spi_trend']

    vs  = [(y, v) for y, v in zip(years, sadi) if not _nan(v)]
    vp  = [(y, v) for y, v in zip(years, spi)  if not _nan(v)]
    dy  = [y for y, v in vs if v < 0]
    pct = 100 * len(dy) // len(vs) if vs else 0

    w_yr, w_v = min(vs, key=lambda x: x[1]) if vs else (None, None)
    b_yr, b_v = max(vs, key=lambda x: x[1]) if vs else (None, None)

    proj = f'{st[0] * next_yr + st[1]:+.3f}' if st[0] is not None else 'N/A'

    cur   = markov['current_state']
    probs = markov['next_state_probs']
    ml    = STATE_LABELS[probs.index(max(probs))]

    L = [
        'AGRICULTURAL DROUGHT ANALYSIS REPORT',
        '=' * 52, '',
        f'Period  : {years[0]}–{years[-1]}   |   Season : {season}',
        f'Source  : Landsat 7 (NDVI / VHI)  +  CHIRPS (SPI)',
        '',
        'DROUGHT CONDITIONS SUMMARY', '-' * 34,
        f'{len(vs)} seasonal composites analysed.',
        f'{len(dy)} of {len(vs)} years ({pct}%) showed drought (SADI < 0).',
    ]
    if w_yr:
        L += [
            f'Most severe  : {w_yr}   SADI = {w_v:+.3f}  ({_classify_label(w_v)})',
            f'Least severe : {b_yr}   SADI = {b_v:+.3f}  ({_classify_label(b_v)})',
        ]
    L += [
        '',
        'LONG-TERM TREND (LINEAR REGRESSION)', '-' * 34,
        f'SADI : {_trend_desc(st[0], st[2])}',
        f'NDVI : {_trend_desc(nt[0], nt[2])}',
        f'SPI  : {_trend_desc(pt[0], pt[2])}',
        f'Projected SADI {next_yr} : {proj}',
        '',
        'MARKOV CHAIN PREDICTION', '-' * 34,
        f'Current drought state ({years[-1]}) : '
        f'{STATE_LABELS[cur] if cur is not None else "Unknown"}',
        'Next-season probabilities :',
    ]
    for label, prob in zip(STATE_LABELS, probs):
        bar = '█' * int(prob * 25)
        L.append(f'  {label:<22}  {100 * prob:5.1f}%   {bar}')
    L.append(f'Most likely next state : {ml}  ({100 * max(probs):.0f}%)')

    if vp:
        dry_yr, dry_v = min(vp, key=lambda x: x[1])
        wet_yr, wet_v = max(vp, key=lambda x: x[1])
        L += [
            '',
            'PRECIPITATION (SPI)', '-' * 34,
            f'Driest season  : {dry_yr}   SPI = {dry_v:+.2f}',
            f'Wettest season : {wet_yr}   SPI = {wet_v:+.2f}',
        ]
    L += [
        '',
        'NOTES', '-' * 34,
        '• Annual SADI is VHI mapped to [−1, +1] at 1 km scale.',
        '• Map-layer SADI uses full DI classification at 30 m (paper method).',
        '• Markov transitions are estimated from the historical SADI record.',
        '• Linear trend projection: use with caution on short time series.',
    ]
    return '\n'.join(L)


# ─────────────────────────────────────── Results panel (non-modal) ────────────

class ResultsPanel(QtWidgets.QDialog):
    """Non-modal results panel with Charts, Prediction and Text Summary tabs."""

    def __init__(self, result_data, parent=None):
        super().__init__(parent)
        self.setWindowFlags(
            self.windowFlags()
            | Qt.WindowMinimizeButtonHint
            | Qt.WindowMaximizeButtonHint
        )
        self.setModal(False)
        self.setWindowTitle('SADI Results')
        self.resize(860, 640)
        self.rd = result_data

        tabs = QtWidgets.QTabWidget()
        tabs.addTab(self._charts_tab(),     'Charts')
        tabs.addTab(self._prediction_tab(), 'Prediction')
        tabs.addTab(self._summary_tab(),    'Text Summary')

        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(tabs)

    # ─────────────────────────────────────────── Charts tab ───────────────────

    def _charts_tab(self):
        widget = QtWidgets.QWidget()
        vbox   = QtWidgets.QVBoxLayout(widget)

        try:
            import matplotlib
            matplotlib.use('Qt5Agg')
            from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg
            from matplotlib.figure import Figure

            rd     = self.rd
            years  = rd['years']
            sadi   = [v if not _nan(v) else float('nan') for v in rd['sadi']]
            ndvi   = [v if not _nan(v) else float('nan') for v in rd['ndvi']]
            spi    = [v if not _nan(v) else float('nan') for v in rd['spi']]

            # Trend lines
            st = rd['sadi_trend'];  nt = rd['ndvi_trend'];  pt = rd['spi_trend']
            trend_x = [years[0], years[-1]]

            fig = Figure(figsize=(9, 8), tight_layout=True)
            self._fig_charts = fig

            # ── NDVI subplot ──
            ax1 = fig.add_subplot(3, 1, 1)
            ax1.plot(years, ndvi, 'o-', color='#388e3c', linewidth=2,
                     markersize=5, label='NDVI')
            if nt[0] is not None:
                ax1.plot(trend_x, [nt[0] * x + nt[1] for x in trend_x],
                         '--', color='#1b5e20', linewidth=1.5, label='Trend')
            ax1.axhline(0, color='gray', linewidth=0.6, linestyle=':')
            ax1.set_ylabel('NDVI')
            ax1.set_title('Annual Mean NDVI')
            ax1.legend(fontsize=8);  ax1.grid(True, alpha=0.2)
            ax1.set_ylim(-0.1, 1.0)

            # ── SADI subplot ──
            ax2 = fig.add_subplot(3, 1, 2)
            colours = [_bar_colour(v) for v in rd['sadi']]
            ax2.bar(years, sadi, color=colours, edgecolor='none', width=0.7)
            if st[0] is not None:
                ax2.plot(trend_x, [st[0] * x + st[1] for x in trend_x],
                         '--', color='#212121', linewidth=1.5, label='Trend')
            ax2.axhline(0,    color='black',  linewidth=0.8)
            ax2.axhline(-0.5, color='#FF4500', linewidth=0.5, linestyle=':')
            ax2.axhline(0.5,  color='#2e7d32', linewidth=0.5, linestyle=':')
            ax2.set_ylabel('SADI')
            ax2.set_title('Annual Mean SADI  (VHI proxy, 1 km)  '
                          '— red = drought,  green = healthy')
            ax2.set_ylim(-1.1, 1.1);  ax2.grid(True, alpha=0.2, axis='y')
            if st[0] is not None:
                ax2.legend(fontsize=8)

            # ── SPI subplot ──
            ax3 = fig.add_subplot(3, 1, 3)
            spi_c = ['#1565c0' if not _nan(v) and v >= 0 else '#6d4c41'
                     for v in rd['spi']]
            ax3.bar(years, spi, color=spi_c, edgecolor='none', width=0.7)
            if pt[0] is not None:
                ax3.plot(trend_x, [pt[0] * x + pt[1] for x in trend_x],
                         '--', color='#212121', linewidth=1.5)
            ax3.axhline(0, color='black', linewidth=0.8)
            ax3.set_ylabel('SPI')
            ax3.set_title('Standardised Precipitation Index (CHIRPS)  '
                          '— blue = wet,  brown = dry')
            ax3.grid(True, alpha=0.2, axis='y')

            for ax in (ax1, ax2, ax3):
                ax.set_xticks(years)
                ax.set_xticklabels([str(y) for y in years], rotation=45,
                                   fontsize=8)

            canvas = FigureCanvasQTAgg(fig)
            vbox.addWidget(canvas)

        except ImportError:
            vbox.addWidget(QtWidgets.QLabel(
                'matplotlib not installed.\nRun:  pip install matplotlib'))
        except Exception as exc:
            vbox.addWidget(QtWidgets.QLabel(f'Chart error: {exc}'))

        btn = QtWidgets.QPushButton('Save Charts as PNG')
        btn.clicked.connect(lambda: self._save_fig(self._fig_charts, 'charts'))
        vbox.addWidget(btn)
        return widget

    # ─────────────────────────────────────── Prediction tab ───────────────────

    def _prediction_tab(self):
        widget = QtWidgets.QWidget()
        vbox   = QtWidgets.QVBoxLayout(widget)

        try:
            import matplotlib
            matplotlib.use('Qt5Agg')
            from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg
            from matplotlib.figure import Figure

            rd     = self.rd
            years  = rd['years']
            sadi   = rd['sadi']
            markov = rd['markov']
            st     = rd['sadi_trend']
            next_yr = rd['next_year']

            fig = Figure(figsize=(9, 5), tight_layout=True)
            self._fig_pred = fig

            # ── Markov next-state probability bars ──
            ax1 = fig.add_subplot(1, 2, 1)
            probs  = markov['next_state_probs']
            ypos   = range(len(STATE_LABELS))
            ax1.barh(list(ypos), probs, color=STATE_COLOURS, edgecolor='none')
            ax1.set_yticks(list(ypos))
            ax1.set_yticklabels(STATE_LABELS, fontsize=9)
            ax1.set_xlabel('Probability')
            ax1.set_xlim(0, 1)
            ax1.set_title(
                f'Markov Prediction\nNext season from '
                f'{STATE_LABELS[markov["current_state"]] if markov["current_state"] is not None else "Unknown"}'
            )
            ax1.grid(True, alpha=0.2, axis='x')
            for i, p in enumerate(probs):
                ax1.text(p + 0.01, i, f'{100 * p:.0f}%', va='center', fontsize=8)

            # ── SADI trend + projection ──
            ax2 = fig.add_subplot(1, 2, 2)
            valid_pairs = [(y, v) for y, v in zip(years, sadi) if not _nan(v)]
            if valid_pairs:
                vx, vy = zip(*valid_pairs)
                ax2.scatter(vx, vy, c=[_bar_colour(v) for v in vy],
                            s=50, zorder=3, label='Historical SADI')
            if st[0] is not None:
                ext_x = list(range(years[0], next_yr + 1))
                ext_y = [st[0] * x + st[1] for x in ext_x]
                hist_x = [x for x in ext_x if x <= years[-1]]
                proj_x = [x for x in ext_x if x >= years[-1]]
                ax2.plot([x for x in ext_x if x <= years[-1]],
                         [st[0]*x + st[1] for x in hist_x],
                         '-', color='#424242', linewidth=1.5, label='Trend')
                ax2.plot([x for x in ext_x if x >= years[-1]],
                         [st[0]*x + st[1] for x in proj_x],
                         '--', color='#f44336', linewidth=1.5, label=f'Projection → {next_yr}')
                proj_val = st[0] * next_yr + st[1]
                ax2.annotate(
                    f'{proj_val:+.2f}',
                    xy=(next_yr, proj_val),
                    xytext=(next_yr - 0.5, proj_val + 0.1),
                    fontsize=9, color='#f44336',
                )
            ax2.axhline(0,    color='black', linewidth=0.8)
            ax2.axhline(-0.5, color='#FF4500', linewidth=0.5, linestyle=':')
            ax2.axhline(0.5,  color='#2e7d32', linewidth=0.5, linestyle=':')
            ax2.set_ylim(-1.2, 1.2)
            ax2.set_title('SADI Trend & Linear Projection')
            ax2.set_ylabel('SADI');  ax2.set_xlabel('Year')
            ax2.legend(fontsize=8);  ax2.grid(True, alpha=0.2)

            canvas = FigureCanvasQTAgg(fig)
            vbox.addWidget(canvas)

        except ImportError:
            vbox.addWidget(QtWidgets.QLabel(
                'matplotlib not installed.\nRun:  pip install matplotlib'))
        except Exception as exc:
            vbox.addWidget(QtWidgets.QLabel(f'Chart error: {exc}'))

        btn = QtWidgets.QPushButton('Save Prediction Charts as PNG')
        btn.clicked.connect(lambda: self._save_fig(self._fig_pred, 'prediction'))
        vbox.addWidget(btn)
        return widget

    # ──────────────────────────────────── Text Summary tab ────────────────────

    def _summary_tab(self):
        widget = QtWidgets.QWidget()
        vbox   = QtWidgets.QVBoxLayout(widget)

        txt = QtWidgets.QTextEdit()
        txt.setReadOnly(True)
        txt.setFont(_monospace_font())
        txt.setPlainText(_generate_text_summary(self.rd))
        vbox.addWidget(txt)

        btn_row = QtWidgets.QHBoxLayout()
        copy_btn = QtWidgets.QPushButton('Copy to Clipboard')
        copy_btn.clicked.connect(
            lambda: QtWidgets.QApplication.clipboard().setText(txt.toPlainText()))
        btn_row.addWidget(copy_btn)
        btn_row.addStretch()
        vbox.addLayout(btn_row)
        return widget

    # ─────────────────────────────────────────── PNG save ─────────────────────

    def _save_fig(self, fig, name):
        if not hasattr(self, '_fig_' + ('charts' if name == 'charts' else 'pred')):
            QtWidgets.QMessageBox.warning(self, 'No chart', 'Chart not available.')
            return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, 'Save Chart', f'sadi_{name}.png', 'PNG (*.png);;JPEG (*.jpg)')
        if path:
            fig.savefig(path, dpi=150, bbox_inches='tight')
            QtWidgets.QMessageBox.information(self, 'Saved', f'Saved:\n{path}')


def _monospace_font():
    from qgis.PyQt.QtGui import QFont
    f = QFont('Courier New', 9)
    f.setStyleHint(QFont.TypeWriter)
    return f


# ─────────────────────────────────────────────────── GEE worker thread ────────

class GEEWorker(QThread):
    progress = pyqtSignal(int)
    status   = pyqtSignal(str)
    finished = pyqtSignal(list)   # [SADI_image, ndvi_image, rgb_image, chart_data]
    error    = pyqtSignal(str)

    def __init__(self, geometry, start_year, end_year, startmonth, endmonth):
        super().__init__()
        self.geometry   = geometry
        self.start_year = start_year
        self.end_year   = end_year
        self.startmonth = startmonth
        self.endmonth   = endmonth

    def run(self):
        try:
            import ee
            g = self.geometry

            # ── Phase 1: full-period SADI composite (map layer) ───────────────
            self.progress.emit(5)
            self.status.emit('Loading Landsat 7 collection…')

            def _mask(img):
                qa = img.select('QA_PIXEL')
                return img.updateMask(
                    qa.bitwiseAnd(1 << 3).eq(0).And(qa.bitwiseAnd(1 << 5).eq(0)))

            col = (ee.ImageCollection('LANDSAT/LE07/C02/T1')
                   .map(_mask)
                   .filter(ee.Filter.calendarRange(
                       self.start_year, self.end_year, 'year'))
                   .filter(ee.Filter.calendarRange(
                       self.startmonth, self.endmonth, 'month'))
                   .filterBounds(g))

            col_size = col.size().getInfo()
            if col_size == 0:
                raise ValueError(
                    f'No Landsat 7 images found for '
                    f'{self.start_year}–{self.end_year}, '
                    f'months {self.startmonth}–{self.endmonth}.\n'
                    'Try a wider date range, a different season, or check the AOI.')

            self.status.emit(f'{col_size} image(s) found — computing composite…')
            image     = col.mean().clip(g)
            col_image = image

            self.progress.emit(15)
            self.status.emit('Computing brightness temperature…')

            toa = image.expression('0.055375 * B6 + 1.18243',
                                   {'B6': image.select('B6_VCID_1')})
            bt  = toa.expression(
                '(1260.56 / (log((607.76 / L) + 1))) - 273.15', {'L': toa}
            ).clip(g)

            self.progress.emit(25)
            self.status.emit('Computing NDVI…')

            ndvi = image.normalizedDifference(['B4', 'B3']).rename('NDVI').clip(g)
            # Combined min+max in one server round-trip (halves reduceRegion calls)
            ndvi_mm  = ndvi.reduceRegion(ee.Reducer.minMax(), g, 30, maxPixels=1e9)
            ndvi_min = ee.Number(ndvi_mm.get('NDVI_min'))
            ndvi_max = ee.Number(ndvi_mm.get('NDVI_max'))

            fv  = (ndvi.subtract(ndvi_min)
                       .divide(ndvi_max.subtract(ndvi_min)).pow(2))
            VCI = ndvi.subtract(ndvi_min).divide(ndvi_max.subtract(ndvi_min))
            EM  = fv.multiply(0.004).add(0.986).rename('EMM')

            self.progress.emit(35)
            self.status.emit('Computing Land Surface Temperature…')

            LST = bt.expression(
                'BT / (1 + (10.60 * BT / 14388) * log(eps))',
                {'BT': bt, 'eps': EM.select('EMM')},
            ).rename('LST').clip(g)

            lst_mm  = LST.reduceRegion(ee.Reducer.minMax(), g, 30, maxPixels=1e9)
            lst_min = ee.Number(lst_mm.get('LST_min'))
            lst_max = ee.Number(lst_mm.get('LST_max'))

            TCI = ee.Image(lst_max).subtract(LST).divide(lst_max.subtract(lst_min))
            VHI = VCI.multiply(0.5).add(TCI.multiply(0.5))

            self.progress.emit(48)
            self.status.emit('Computing SADI map layer…')

            DI = (VHI.lt(0.1).And(VHI.gte(-1))
                  .add(VHI.gte(0.1).And(VHI.lt(0.2)).multiply(2))
                  .add(VHI.gte(0.2).And(VHI.lt(0.3)).multiply(3))
                  .add(VHI.gte(0.3).And(VHI.lt(0.4)).multiply(4))
                  .add(VHI.gte(0.4).multiply(5))).rename('DI')
            di_mm  = DI.reduceRegion(ee.Reducer.minMax(), g, 30, maxPixels=1e9)
            di_min = ee.Number(di_mm.get('DI_min'))
            di_max = ee.Number(di_mm.get('DI_max'))
            SADI = DI.expression(
                '2 * ((DI - mn) / (mx - mn)) - 1',
                {'DI': DI, 'mn': di_min, 'mx': di_max})

            # ── Phase 2: per-year time-series for charts ──────────────────────
            self.progress.emit(62)
            self.status.emit('Computing annual time-series (NDVI + VHI + precipitation)…')

            years  = list(range(self.start_year, self.end_year + 1))
            sm, em = self.startmonth, self.endmonth

            def _add_vhi(img):
                nd  = img.normalizedDifference(['B4', 'B3']).rename('NDVI')
                vci = nd.subtract(ndvi_min).divide(ndvi_max.subtract(ndvi_min))
                t   = img.expression('0.055375 * B6 + 1.18243',
                                     {'B6': img.select('B6_VCID_1')})
                bt_ = t.expression(
                    '(1260.56/(log((607.76/L)+1)))-273.15', {'L': t})
                fv_ = vci.pow(2)
                em_ = fv_.multiply(0.004).add(0.986)
                l_  = bt_.expression(
                    'BT/(1+(10.60*BT/14388)*log(eps))',
                    {'BT': bt_, 'eps': em_}).rename('LST')
                tci_ = ee.Image(lst_max).subtract(l_).divide(lst_max.subtract(lst_min))
                vhi_ = vci.multiply(0.5).add(tci_.multiply(0.5)).rename('VHI')
                return img.addBands(nd).addBands(vhi_)

            # Step 1: batch size check — one round-trip, all years at once.
            # This tells us which years actually have Landsat 7 images so we
            # never call reduceRegion on a 0-band mean image (which returns an
            # empty dict and causes "Dictionary does not contain key" on .get()).
            self.status.emit('Checking per-year Landsat availability…')
            ls_sizes = ee.List([
                ee.ImageCollection('LANDSAT/LE07/C02/T1')
                .map(_mask)
                .filter(ee.Filter.calendarRange(yr, yr, 'year'))
                .filter(ee.Filter.calendarRange(sm, em, 'month'))
                .filterBounds(g)
                .size()
                for yr in years
            ]).getInfo()

            active_years = [yr for yr, sz in zip(years, ls_sizes) if sz > 0]

            # Step 2: build and fetch stats only for years confirmed non-empty.
            def _stats_for_year(yr):
                ls = (ee.ImageCollection('LANDSAT/LE07/C02/T1')
                      .map(_mask)
                      .filter(ee.Filter.calendarRange(yr, yr, 'year'))
                      .filter(ee.Filter.calendarRange(sm, em, 'month'))
                      .filterBounds(g)
                      .map(_add_vhi))
                mean_img = ls.select(['NDVI', 'VHI']).mean().clip(g)
                stats    = mean_img.reduceRegion(
                    ee.Reducer.mean(), g, 1000, maxPixels=1e9)
                chirps = (ee.ImageCollection('UCSB-CHG/CHIRPS/DAILY')
                          .filter(ee.Filter.calendarRange(yr, yr, 'year'))
                          .filter(ee.Filter.calendarRange(sm, em, 'month'))
                          .filterBounds(g))
                # CHIRPS is reliable post-1981; use -1 as a safe sentinel in
                # the rare case the collection is empty (precipitation ≥ 0).
                precip = (chirps.sum().clip(g)
                          .reduceRegion(ee.Reducer.mean(), g, 5500, maxPixels=1e9)
                          .get('precipitation', -1))
                return ee.Dictionary({
                    'ndvi':   stats.get('NDVI'),
                    'vhi':    stats.get('VHI'),
                    'precip': precip,
                })

            if active_years:
                self.status.emit(
                    f'Fetching time-series for {len(active_years)}'
                    f' of {len(years)} year(s)…')
                active_stats = ee.List(
                    [_stats_for_year(yr) for yr in active_years]
                ).getInfo()
            else:
                active_stats = []

            # Reconstruct full-length arrays; years with no imagery → None
            by_year     = dict(zip(active_years, active_stats))
            ndvi_vals   = [by_year[yr].get('ndvi')   if yr in by_year else None
                           for yr in years]
            vhi_vals    = [by_year[yr].get('vhi')    if yr in by_year else None
                           for yr in years]
            precip_vals = [
                (v if v != -1 else None)
                for v in (by_year[yr].get('precip') if yr in by_year else None
                          for yr in years)
            ]
            # VHI [0,1] → SADI proxy [-1,+1]
            sadi_vals   = [v * 2 - 1 if v is not None else None
                           for v in vhi_vals]

            chart_data = {
                'years':        years,
                'ndvi':         ndvi_vals,
                'sadi':         sadi_vals,
                'precip':       precip_vals,
            }

            self.progress.emit(88)
            self.status.emit('Finalising…')
            self.finished.emit([SADI, ndvi, col_image, chart_data])

        except Exception as exc:
            self.error.emit(str(exc))


# ─────────────────────────────────────────────────────────── Main dialog ──────

class droughtIndexerDialog(QtWidgets.QDialog, FORM_CLASS):
    def __init__(self, iface=None, parent=None):
        super().__init__(parent)
        self.iface          = iface
        self._worker        = None
        self._ee_ok         = False
        self._results_panel = None

        self.setupUi(self)

        # "Show Results" button — injected below progress bar
        self.btnShowResults = QtWidgets.QPushButton('Show Results Panel')
        self.btnShowResults.setEnabled(False)
        self.btnShowResults.setVisible(False)
        self.btnShowResults.clicked.connect(self._reopen_results)
        main_layout = self.layout()
        # Insert just before the last item (the OK/Cancel button row)
        main_layout.insertWidget(main_layout.count() - 1, self.btnShowResults)

        self._populate_years()
        self._populate_layers()
        self._restore_project_id()

        self.okBtn.accepted.connect(self._run)
        self.okBtn.rejected.connect(self.close)
        self.btnAuthenticate.clicked.connect(self._authenticate)
        self.btnRefreshLayers.clicked.connect(self._populate_layers)
        self.season.currentIndexChanged.connect(self._sync_end_year)
        self.leProjectId.setToolTip(
            'Your GCP Project ID is saved to your QGIS profile and '
            'persists between sessions. Edit and re-authenticate to change it.')

    # ─────────────────────────────────────────────── setup ────────────────────

    # Season index → last calendar month of that season
    _SEASON_END_MONTH = [3, 6, 9, 12]

    def _populate_years(self):
        today = datetime.date.today()
        years = [str(y) for y in range(2000, today.year + 1)]
        self.startDate.addItems(years)
        self.endDate.addItems(years)
        self._sync_end_year()

    def _sync_end_year(self):
        """Set endDate to the last year whose selected season has fully passed."""
        today  = datetime.date.today()
        end_mo = self._SEASON_END_MONTH[self.season.currentIndex()]
        last   = today.year if today.month > end_mo else today.year - 1
        idx    = self.endDate.findText(str(last))
        if idx >= 0:
            self.endDate.setCurrentIndex(idx)

    def _populate_layers(self):
        self.cbTocLayers.clear()
        for layer in QgsProject.instance().mapLayers().values():
            if (layer.type() == QgsMapLayerType.VectorLayer
                    and layer.geometryType() == QgsWkbTypes.PolygonGeometry):
                self.cbTocLayers.addItem(layer.name(), layer.id())

    def _restore_project_id(self):
        pid = QSettings().value('drought_indexer/gcp_project', '')
        self.leProjectId.setText(pid)
        if pid:
            self._check_ee_ready(pid)
        else:
            self._auth_status(False, 'Enter your GCP Project ID then click Authenticate')

    def _check_ee_ready(self, pid):
        try:
            import ee
            ee.Initialize(project=pid)
            self._ee_ok = True
            self._auth_status(True,
                f'Authenticated  ✓  ({pid})  — saved to your QGIS profile')
        except Exception:
            self._ee_ok = False
            self._auth_status(False,
                f'Saved project: {pid}  — credentials not found, click Authenticate')

    def _auth_status(self, ok, msg):
        self.lblAuthStatus.setText(msg)
        self.lblAuthStatus.setStyleSheet(
            f'color:{"#1b5e20" if ok else "#b71c1c"};font-weight:bold;')

    # ──────────────────────────────────────── authentication ──────────────────

    def _authenticate(self):
        pid = self.leProjectId.text().strip()
        if not pid:
            QtWidgets.QMessageBox.warning(
                self, 'GCP Project Required',
                'Enter your Google Cloud Project ID.\n\n'
                'The project must have the Earth Engine API enabled and be '
                'registered for commercial or noncommercial use.')
            return
        if not _ensure_package('earthengine-api', 'ee'):
            QtWidgets.QMessageBox.critical(
                self, 'Install Failed',
                'Could not install earthengine-api.\n'
                'Run in a terminal:\n\n  pip install earthengine-api')
            return
        try:
            import ee
            self._auth_status(False, 'Opening browser — complete the OAuth flow…')
            QtWidgets.QApplication.processEvents()
            ee.Authenticate()
            ee.Initialize(project=pid)
            self._ee_ok = True
            QSettings().setValue('drought_indexer/gcp_project', pid)
            self._auth_status(True,
                f'Authenticated  ✓  ({pid})  — saved to your QGIS profile')
        except Exception as exc:
            self._ee_ok = False
            self._auth_status(False, 'Authentication failed')
            QtWidgets.QMessageBox.critical(self, 'Authentication Error', str(exc))

    # ──────────────────────────────────────────── AOI ─────────────────────────

    def _get_aoi(self):
        import ee
        tab = self.tabAOI.currentIndex()
        if tab == 0:
            return self._aoi_from_layer()
        elif tab == 1:
            return self._aoi_from_extent()
        else:
            return self._aoi_from_file(self.LegeojsonPath.filePath())

    def _aoi_from_layer(self):
        import ee
        lid = self.cbTocLayers.currentData()
        if not lid:
            raise ValueError(
                'No polygon layer in the list. Load one and click Refresh.')
        layer = QgsProject.instance().mapLayer(lid)
        return self._vector_to_ee_geom(layer.source())

    def _aoi_from_file(self, path):
        import ee
        if not path:
            raise ValueError('No file selected.')
        return self._vector_to_ee_geom(path)

    def _vector_to_ee_geom(self, source_path):
        import ee
        import geopandas as gpd
        df = gpd.read_file(source_path)
        if df.empty:
            raise ValueError(f'No features in: {source_path}')
        if df.crs is None:
            raise ValueError('Layer has no CRS. Set it in QGIS layer properties.')
        if df.crs.to_epsg() != 4326:
            df = df.to_crs('EPSG:4326')
        dissolved = df.geometry.unary_union
        if dissolved is None or dissolved.is_empty:
            raise ValueError('Geometry is empty after dissolving.')
        g = dissolved.__geo_interface__
        if g['type'] == 'Polygon':
            return ee.Geometry.Polygon(g['coordinates'])
        elif g['type'] == 'MultiPolygon':
            return ee.Geometry.MultiPolygon(g['coordinates'])
        else:
            raise ValueError(f'Unsupported geometry type: {g["type"]}')

    def _aoi_from_extent(self):
        import ee
        n, s, w, e = (self.sbNorth.value(), self.sbSouth.value(),
                      self.sbWest.value(),  self.sbEast.value())
        if n == s == w == e == 0:
            raise ValueError('All extent values are 0. Enter valid coordinates.')
        if n <= s:
            raise ValueError('North must be greater than South.')
        if e <= w:
            raise ValueError('East must be greater than West.')
        return ee.Geometry.Rectangle([w, s, e, n])

    # ────────────────────────────────────── map display ───────────────────────

    def _add_ee_layer(self, image, vis_params, name):
        try:
            map_id   = image.getMapId(vis_params)
            tile_url = map_id['tile_fetcher'].url_format
            safe_url = tile_url.replace('&', '%26')
            uri = f'type=xyz&url={safe_url}&zmax=18&zmin=0'
            rl  = QgsRasterLayer(uri, name, 'wms')
            if rl.isValid():
                QgsProject.instance().addMapLayer(rl)
            else:
                self.lblStatus.setText(f'Tile layer "{name}" invalid — check auth.')
        except Exception as exc:
            self.lblStatus.setText(f'Could not add "{name}": {exc}')

    # ────────────────────────────────────────── run ───────────────────────────

    def _run(self):
        if not self._ee_ok:
            QtWidgets.QMessageBox.warning(
                self, 'Not Authenticated',
                'Authenticate with Google Earth Engine first.')
            return
        if not _ensure_package('geopandas'):
            QtWidgets.QMessageBox.critical(
                self, 'Missing Package',
                'geopandas required.\nRun:  pip install geopandas')
            return
        try:
            geometry = self._get_aoi()
        except ValueError as exc:
            QtWidgets.QMessageBox.warning(self, 'AOI Error', str(exc))
            return
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, 'AOI Error', str(exc))
            return

        start_year = int(self.startDate.currentText())
        end_year   = int(self.endDate.currentText())
        if end_year < start_year:
            QtWidgets.QMessageBox.warning(
                self, 'Date Error', 'End year must be ≥ start year.')
            return

        season   = self.season.currentIndex() + 1
        sm, em   = {1: (1, 3), 2: (4, 6), 3: (7, 9), 4: (10, 12)}[season]
        self._season_label = self.season.currentText()
        self._start_year   = start_year
        self._end_year     = end_year

        self.progressBar.setValue(0)
        self.lblStatus.setText('Submitting to Google Earth Engine…')
        self.okBtn.setEnabled(False)
        self.btnShowResults.setEnabled(False)

        self._worker = GEEWorker(geometry, start_year, end_year, sm, em)
        self._worker.progress.connect(self.progressBar.setValue)
        self._worker.status.connect(self.lblStatus.setText)
        self._worker.finished.connect(self._on_finished)
        self._worker.error.connect(self._on_error)
        self._worker.start()

    def _on_finished(self, result):
        sadi, ndvi_img, rgb, chart_data = result

        self.progressBar.setValue(92)
        self.lblStatus.setText('Adding layers to QGIS map…')
        QtWidgets.QApplication.processEvents()

        self._add_ee_layer(
            sadi,
            {'min': -1, 'max': 1,
             'palette': ['8B0000', 'FF4500', 'FFA500', 'FFFF00', '006400']},
            'SADI — Drought Index',
        )
        self._add_ee_layer(
            rgb.select('B3', 'B2', 'B1'), {'min': 3, 'max': 200},
            'Landsat 7 — RGB',
        )
        self._add_ee_layer(
            ndvi_img,
            {'min': -1, 'max': 1, 'palette': ['FF0000', 'FFFF00', '00AA00']},
            'NDVI',
        )

        # ── Client-side analysis ──
        years       = chart_data['years']
        spi_vals    = _compute_spi(chart_data['precip'])
        chart_data['spi']          = spi_vals
        chart_data['season_label'] = self._season_label
        chart_data['next_year']    = self._end_year + 1
        chart_data['markov']       = _compute_markov(years, chart_data['sadi'])
        chart_data['sadi_trend']   = _compute_trend(years, chart_data['sadi'])
        chart_data['ndvi_trend']   = _compute_trend(years, chart_data['ndvi'])
        chart_data['spi_trend']    = _compute_trend(years, spi_vals)

        self.progressBar.setValue(100)
        self.lblStatus.setText('Done — layers added to map.')
        self.okBtn.setEnabled(True)
        self.btnShowResults.setEnabled(True)
        self.btnShowResults.setVisible(True)

        # Open results panel (non-modal)
        self._results_panel = ResultsPanel(chart_data, parent=self)
        self._results_panel.show()

    def _reopen_results(self):
        if self._results_panel:
            self._results_panel.show()
            self._results_panel.raise_()

    def _on_error(self, msg):
        self.progressBar.setValue(0)
        self.lblStatus.setText('Error — see message.')
        self.okBtn.setEnabled(True)
        QtWidgets.QMessageBox.critical(self, 'Processing Error', msg)
