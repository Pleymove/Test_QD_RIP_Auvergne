"""
QD RIP Auvergne – Contrôle qualité
Onglets :
  1. Chevauchements C0 / couches existantes
  2. Doublons dans l'infra (parcours superposés)
  3. Parcours les plus longs
  4. BAL isolées (aucun voisin BAL dans un rayon donné)
"""

import csv
import os
import tempfile
import datetime
from collections import Counter

from qgis.PyQt.QtWidgets import (
    QDialog, QTabWidget, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QDoubleSpinBox, QSpinBox, QLineEdit,
    QTableWidget, QTableWidgetItem, QHeaderView, QCheckBox,
    QGroupBox, QFormLayout, QProgressDialog, QMessageBox,
    QApplication, QAbstractItemView, QFileDialog, QFrame,
    QSplitter, QPlainTextEdit, QDialogButtonBox,
)
from qgis.PyQt.QtCore import Qt, QUrl
from qgis.PyQt.QtGui import QColor, QBrush, QFont, QDesktopServices

from qgis.core import (
    QgsProject, QgsSpatialIndex, QgsFeatureRequest, QgsRectangle,
)
from qgis.gui import QgsMapLayerComboBox
from qgis.utils import iface

from .pm_perimeter import DEFAULT_PM_CODES

# Resolution du filtre de couche, compatible QGIS 3.16 -> 4.x
try:
    from qgis.core import Qgis
    _F_LINE = Qgis.LayerFilter.LineLayer
    _F_POINT = Qgis.LayerFilter.PointLayer
except (ImportError, AttributeError):
    try:
        from qgis.core import QgsMapLayerProxyModel
    except ImportError:
        from qgis.gui import QgsMapLayerProxyModel
    _F_LINE = QgsMapLayerProxyModel.LineLayer
    _F_POINT = QgsMapLayerProxyModel.PointLayer


# ─────────────────────────────────────────────────────────────────────────────
# Custom table item: numeric-aware sort
# ─────────────────────────────────────────────────────────────────────────────
class _NumItem(QTableWidgetItem):
    def __lt__(self, other):
        def _num(s):
            try:
                return float(s.replace('%', '').replace(' m', '').replace(',', '.').strip())
            except ValueError:
                return float('-inf')
        return _num(self.text()) < _num(other.text())


def _ni(text):
    """Create a numeric-sortable table item."""
    return _NumItem(str(text))


def _si(text):
    """Create a plain string table item."""
    return QTableWidgetItem(str(text))


# ─────────────────────────────────────────────────────────────────────────────
# Main dialog
# ─────────────────────────────────────────────────────────────────────────────
class QDRIPDialog(QDialog):

    # Name fragments used to auto-select layers on startup
    _LAYER_HINTS = {
        'infra':        ['infra_c03e1bf7', 'infra'],
        'ft_arciti':    ['ft_arciti_53374007', 'ft_arciti'],
        'bt':           ['bt_def0d723', 'bt'],
        'athd_artere':  ['athd_artere_ab4dbaf5', 'athd_artere'],
        't_cheminement':['t_cheminement_aa3c43e0', 't_cheminement'],
        'bal':          ['bal_442ddc78', 'bal'],
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pm_codes = list(DEFAULT_PM_CODES)
        self._pm_set = set(self._pm_codes)
        self.setWindowTitle('QD RIP Auvergne — Contrôle Qualité')
        self.setMinimumSize(980, 700)
        self.resize(1200, 800)
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowMaximizeButtonHint)
        self._build_ui()
        self._auto_select_layers()

    # ─── layer helpers ───────────────────────────────────────────────────────

    def _find_layer(self, *hints):
        for lyr in QgsProject.instance().mapLayers().values():
            name = lyr.name().lower()
            for h in hints:
                if h.lower() in name:
                    return lyr
        return None

    def _auto_select_layers(self):
        hints = self._LAYER_HINTS
        infra = self._find_layer(*hints['infra'])
        for cb in (self.cb_infra_chev, self.cb_infra_doub,
                   self.cb_infra_parc, self.cb_infra_bal):
            if infra:
                cb.setLayer(infra)

        ft = self._find_layer(*hints['ft_arciti'])
        if ft:
            self.cb_ft.setLayer(ft)

        bt = self._find_layer(*hints['bt'])
        if bt:
            self.cb_bt.setLayer(bt)

        athd = self._find_layer(*hints['athd_artere'])
        if athd:
            self.cb_athd.setLayer(athd)

        chemi = self._find_layer(*hints['t_cheminement'])
        if chemi:
            self.cb_chemi.setLayer(chemi)

        bal = self._find_layer(*hints['bal'])
        if bal:
            self.cb_bal.setLayer(bal)

    # ─── top-level UI ────────────────────────────────────────────────────────

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)

        # ── Périmètre PM (s'applique à toutes les analyses) ──
        pm_bar = QHBoxLayout()
        self.chk_pm = QCheckBox('Restreindre au périmètre PM')
        self.chk_pm.setChecked(True)
        self.chk_pm.setToolTip(
            'Si coché, seules les entités dont le champ "sro" figure\n'
            'dans la liste de PM sont analysées (toutes les analyses).'
        )
        pm_bar.addWidget(self.chk_pm)
        self.lbl_pm = QLabel()
        pm_bar.addWidget(self.lbl_pm)
        btn_pm = QPushButton('Modifier la liste…')
        btn_pm.clicked.connect(self._edit_pm_list)
        pm_bar.addWidget(btn_pm)
        pm_bar.addStretch()
        root.addLayout(pm_bar)
        self._refresh_pm_label()

        self.tabs = QTabWidget()
        self.tabs.addTab(self._tab_chevauchement(), '⚠  Chevauchements C0 / Existant')
        self.tabs.addTab(self._tab_doublons(),       '⛔  Doublons Infra')
        self.tabs.addTab(self._tab_parcours(),       '📏  Parcours les plus longs')
        self.tabs.addTab(self._tab_bal(),            '📍  BAL Isolées')
        root.addWidget(self.tabs)

        bar = QHBoxLayout()
        self.lbl_status = QLabel('Prêt.')
        bar.addWidget(self.lbl_status, 1)
        btn_report = QPushButton('📊  Générer Rapport')
        btn_report.setToolTip(
            'Générer un rapport HTML de synthèse avec les résultats\n'
            'des analyses lancées (chevauchements, doublons, parcours, BAL).'
        )
        btn_report.setStyleSheet('font-weight:bold; padding:5px 10px;')
        btn_report.clicked.connect(self._generate_report)
        bar.addWidget(btn_report)
        btn_close = QPushButton('Fermer')
        btn_close.clicked.connect(self.close)
        bar.addWidget(btn_close)
        root.addLayout(bar)

    # ─── périmètre PM ─────────────────────────────────────────────────────────

    def _refresh_pm_label(self):
        self.lbl_pm.setText(f'({len(self._pm_set)} PM)')

    def _edit_pm_list(self):
        dlg = QDialog(self)
        dlg.setWindowTitle('Liste des PM (périmètre)')
        dlg.resize(420, 560)
        v = QVBoxLayout(dlg)
        v.addWidget(QLabel('Un code PM par ligne (comparé au champ "sro") :'))
        txt = QPlainTextEdit('\n'.join(self._pm_codes))
        v.addWidget(txt)
        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        v.addWidget(bb)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            codes = [ln.strip() for ln in txt.toPlainText().splitlines() if ln.strip()]
            self._pm_codes = codes
            self._pm_set = set(codes)
            self._refresh_pm_label()

    def _in_pm(self, feat, fnames):
        """True si l'entité est dans le périmètre PM (ou si le filtre est inactif)."""
        if not self.chk_pm.isChecked() or not self._pm_set:
            return True
        if 'sro' not in fnames:
            return True
        val = feat['sro']
        return val is not None and str(val).strip() in self._pm_set

    # ─── shared helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _style_table(tbl):
        tbl.setSortingEnabled(True)
        tbl.setAlternatingRowColors(True)
        tbl.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        tbl.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        tbl.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        tbl.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        tbl.horizontalHeader().setStretchLastSection(True)
        tbl.verticalHeader().setDefaultSectionSize(22)
        tbl.setWordWrap(False)

    @staticmethod
    def _filter_table(tbl, text):
        text = text.lower()
        for row in range(tbl.rowCount()):
            visible = not text or any(
                text in (tbl.item(row, c).text().lower() if tbl.item(row, c) else '')
                for c in range(tbl.columnCount())
            )
            tbl.setRowHidden(row, not visible)

    @staticmethod
    def _action_bar(tbl, tab_key, extra_widgets=None):
        """Return a QWidget containing Zoom / Sélectionner / CSV buttons."""
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 2, 0, 2)

        btn_zoom = QPushButton('🔍  Zoom')
        btn_zoom.setToolTip('Double-clic sur une ligne pour zoomer directement')
        btn_sel  = QPushButton('✓  Sélectionner dans QGIS')
        btn_csv  = QPushButton('💾  Exporter CSV')

        h.addWidget(btn_zoom)
        h.addWidget(btn_sel)
        if extra_widgets:
            for ew in extra_widgets:
                h.addWidget(ew)
        h.addStretch()
        h.addWidget(btn_csv)

        return w, btn_zoom, btn_sel, btn_csv

    # ─────────────────────────────────────────────────────────────────────────
    # TAB 1 – Chevauchements
    # ─────────────────────────────────────────────────────────────────────────
    def _tab_chevauchement(self):
        root = QWidget()
        h = QHBoxLayout(root)

        # ── Config panel ────────────────────────────────────────────────────
        cfg = QGroupBox('Configuration')
        cfg.setFixedWidth(310)
        vbox = QVBoxLayout(cfg)

        frm = QFormLayout()
        self.cb_infra_chev = QgsMapLayerComboBox()
        self.cb_infra_chev.setFilters(_F_LINE)
        frm.addRow('Couche Infra :', self.cb_infra_chev)

        self.le_c0_filter = QLineEdit('"statut" = \'C\' AND "mode_pose" = 0')
        self.le_c0_filter.setToolTip(
            'Expression QGIS pour isoler les entités C0.\n'
            'Exemple : "statut" = \'C\' AND "mode_pose" = 0'
        )
        frm.addRow('Filtre C0 :', self.le_c0_filter)
        vbox.addLayout(frm)

        # Existing layers
        grp_exist = QGroupBox('Couches existantes')
        gl = QVBoxLayout(grp_exist)

        def _exist_row(label, default_filter='', checked=True):
            r1 = QHBoxLayout()
            chk = QCheckBox(label)
            chk.setChecked(checked)
            cb = QgsMapLayerComboBox()
            cb.setFilters(_F_LINE)
            r1.addWidget(chk)
            r1.addWidget(cb, 1)
            gl.addLayout(r1)
            r2 = QHBoxLayout()
            r2.addSpacing(20)
            r2.addWidget(QLabel('Filtre :'))
            le = QLineEdit(default_filter)
            le.setPlaceholderText('(toutes les entités)')
            le.setToolTip(
                'Filtre QGIS pour ne garder que les entités existantes.\n'
                'Ex: "statut" = \'E\'  ou  "dispopp_ar" != 0'
            )
            r2.addWidget(le, 1)
            gl.addLayout(r2)
            return chk, cb, le

        self.chk_ft,   self.cb_ft,   self.le_ft_filter   = _exist_row('ft_arciti', '"cm_avct" != \'E4\'')
        self.chk_bt,   self.cb_bt,   self.le_bt_filter   = _exist_row('bt', '"type_de_lien" != \'Câble\'')
        self.chk_athd, self.cb_athd, self.le_athd_filter = _exist_row('athd_artere', '"dispopp_ar" != 0')
        self.chk_chemi, self.cb_chemi, self.le_chemi_filter = _exist_row(
            't_cheminement',
            '"cm_typ_imp" = \'C7\' AND ("cm_typelog" LIKE \'TR\' OR "cm_typelog" LIKE \'TD\')')
        vbox.addWidget(grp_exist)

        # Parameters
        grp_param = QGroupBox('Paramètres')
        pf = QFormLayout(grp_param)

        self.sp_tol_chev = QDoubleSpinBox()
        self.sp_tol_chev.setRange(0.1, 100.0)
        self.sp_tol_chev.setValue(1.0)
        self.sp_tol_chev.setSuffix(' m')
        self.sp_tol_chev.setToolTip(
            'Tolérance latérale : une C0 est en conflit si elle passe\n'
            'à moins de cette distance d\'une ligne existante.'
        )
        pf.addRow('Tolérance :', self.sp_tol_chev)

        self.sp_min_chev = QDoubleSpinBox()
        self.sp_min_chev.setRange(0.5, 1000.0)
        self.sp_min_chev.setValue(5.0)
        self.sp_min_chev.setSuffix(' m')
        self.sp_min_chev.setToolTip('Longueur minimale de chevauchement pour signaler le conflit.')
        pf.addRow('Long. min. chevauch. :', self.sp_min_chev)
        vbox.addWidget(grp_param)

        vbox.addStretch()
        btn_run = QPushButton('▶  Lancer l\'analyse')
        btn_run.setStyleSheet('font-weight:bold; padding:6px;')
        btn_run.clicked.connect(self._run_chevauchement)
        vbox.addWidget(btn_run)
        h.addWidget(cfg)

        # ── Results panel ────────────────────────────────────────────────────
        res = QWidget()
        rv = QVBoxLayout(res)
        rv.setContentsMargins(0, 0, 0, 0)

        sh = QHBoxLayout()
        sh.addWidget(QLabel('Recherche :'))
        self.le_srch_chev = QLineEdit()
        self.le_srch_chev.setPlaceholderText('Filtrer les résultats…')
        self.le_srch_chev.textChanged.connect(lambda t: self._filter_table(self.tbl_chev, t))
        sh.addWidget(self.le_srch_chev, 1)
        self.lbl_cnt_chev = QLabel('—')
        sh.addWidget(self.lbl_cnt_chev)
        rv.addLayout(sh)

        self.tbl_chev = QTableWidget(0, 9)
        self.tbl_chev.setHorizontalHeaderLabels([
            'ID Infra', 'id_pa', 'NRO', 'SRO', 'Long. (m)',
            'Couche conf.', 'ID conf.', 'Chevauch. (m)', '% C0',
        ])
        self._style_table(self.tbl_chev)
        self.tbl_chev.doubleClicked.connect(
            lambda idx: self._zoom_row(self.tbl_chev, idx.row())
        )
        rv.addWidget(self.tbl_chev)

        ab, bz, bs, bc = self._action_bar(self.tbl_chev, 'chev')
        btn_clr = QPushButton('✗  Effacer sél.')
        btn_clr.clicked.connect(self._clear_selection)
        ab.layout().insertWidget(2, btn_clr)
        bz.clicked.connect(lambda: self._zoom_selected(self.tbl_chev))
        bs.clicked.connect(lambda: self._select_qgis(self.tbl_chev))
        bc.clicked.connect(lambda: self._export_csv(self.tbl_chev, 'chevauchements'))
        rv.addWidget(ab)

        h.addWidget(res, 1)
        return root

    # ─────────────────────────────────────────────────────────────────────────
    # TAB 2 – Doublons
    # ─────────────────────────────────────────────────────────────────────────
    def _tab_doublons(self):
        root = QWidget()
        h = QHBoxLayout(root)

        cfg = QGroupBox('Configuration')
        cfg.setFixedWidth(290)
        vbox = QVBoxLayout(cfg)

        frm = QFormLayout()
        self.cb_infra_doub = QgsMapLayerComboBox()
        self.cb_infra_doub.setFilters(_F_LINE)
        frm.addRow('Couche Infra :', self.cb_infra_doub)

        self.le_flt_doub = QLineEdit('"statut" = \'C\' AND "mode_pose" = 0')
        frm.addRow('Filtre :', self.le_flt_doub)

        self.sp_tol_doub = QDoubleSpinBox()
        self.sp_tol_doub.setRange(0.1, 100.0)
        self.sp_tol_doub.setValue(1.0)
        self.sp_tol_doub.setSuffix(' m')
        frm.addRow('Tolérance :', self.sp_tol_doub)

        self.sp_min_doub = QDoubleSpinBox()
        self.sp_min_doub.setRange(0.5, 1000.0)
        self.sp_min_doub.setValue(10.0)
        self.sp_min_doub.setSuffix(' m')
        frm.addRow('Long. min. doublon :', self.sp_min_doub)

        vbox.addLayout(frm)

        info = QLabel(
            '<small><i>Détecte les paires d\'entités qui se superposent '
            'sur plus de la longueur minimale définie.</i></small>'
        )
        info.setWordWrap(True)
        vbox.addWidget(info)
        vbox.addStretch()

        btn_run = QPushButton('▶  Lancer l\'analyse')
        btn_run.setStyleSheet('font-weight:bold; padding:6px;')
        btn_run.clicked.connect(self._run_doublons)
        vbox.addWidget(btn_run)
        h.addWidget(cfg)

        res = QWidget()
        rv = QVBoxLayout(res)
        rv.setContentsMargins(0, 0, 0, 0)

        sh = QHBoxLayout()
        sh.addWidget(QLabel('Recherche :'))
        self.le_srch_doub = QLineEdit()
        self.le_srch_doub.setPlaceholderText('Filtrer…')
        self.le_srch_doub.textChanged.connect(lambda t: self._filter_table(self.tbl_doub, t))
        sh.addWidget(self.le_srch_doub, 1)
        self.lbl_cnt_doub = QLabel('—')
        sh.addWidget(self.lbl_cnt_doub)
        rv.addLayout(sh)

        self.tbl_doub = QTableWidget(0, 9)
        self.tbl_doub.setHorizontalHeaderLabels([
            'ID feat. 1', 'id_pa 1', 'NRO 1', 'Long. 1 (m)',
            'ID feat. 2', 'id_pa 2', 'NRO 2', 'Long. 2 (m)',
            'Chevauch. (m)',
        ])
        self._style_table(self.tbl_doub)
        self.tbl_doub.doubleClicked.connect(
            lambda idx: self._zoom_row_doub(idx.row())
        )
        rv.addWidget(self.tbl_doub)

        ab, bz, bs, bc = self._action_bar(self.tbl_doub, 'doub')
        bz.clicked.connect(lambda: self._zoom_row_doub(
            self.tbl_doub.currentRow()
        ))
        bs.clicked.connect(lambda: self._select_qgis_doub())
        bc.clicked.connect(lambda: self._export_csv(self.tbl_doub, 'doublons'))
        rv.addWidget(ab)

        h.addWidget(res, 1)
        return root

    # ─────────────────────────────────────────────────────────────────────────
    # TAB 3 – Parcours les plus longs
    # ─────────────────────────────────────────────────────────────────────────
    def _tab_parcours(self):
        root = QWidget()
        h = QHBoxLayout(root)

        cfg = QGroupBox('Configuration')
        cfg.setFixedWidth(260)
        vbox = QVBoxLayout(cfg)

        frm = QFormLayout()
        self.cb_infra_parc = QgsMapLayerComboBox()
        self.cb_infra_parc.setFilters(_F_LINE)
        frm.addRow('Couche :', self.cb_infra_parc)

        self.le_flt_parc = QLineEdit()
        self.le_flt_parc.setPlaceholderText('(tout afficher)')
        self.le_flt_parc.setToolTip('Filtre QGIS optionnel (ex: "statut" = \'C\')')
        frm.addRow('Filtre :', self.le_flt_parc)

        self.sp_topn = QSpinBox()
        self.sp_topn.setRange(10, 50000)
        self.sp_topn.setValue(500)
        self.sp_topn.setToolTip('Nombre maximum d\'entités à afficher (triées par longueur desc.)')
        frm.addRow('Top N :', self.sp_topn)

        vbox.addLayout(frm)
        vbox.addStretch()

        btn_run = QPushButton('▶  Charger')
        btn_run.setStyleSheet('font-weight:bold; padding:6px;')
        btn_run.clicked.connect(self._run_parcours)
        vbox.addWidget(btn_run)
        h.addWidget(cfg)

        res = QWidget()
        rv = QVBoxLayout(res)
        rv.setContentsMargins(0, 0, 0, 0)

        sh = QHBoxLayout()
        sh.addWidget(QLabel('Recherche :'))
        self.le_srch_parc = QLineEdit()
        self.le_srch_parc.setPlaceholderText('Filtrer…')
        self.le_srch_parc.textChanged.connect(lambda t: self._filter_table(self.tbl_parc, t))
        sh.addWidget(self.le_srch_parc, 1)
        self.lbl_cnt_parc = QLabel('—')
        sh.addWidget(self.lbl_cnt_parc)
        rv.addLayout(sh)

        self.tbl_parc = QTableWidget(0, 9)
        self.tbl_parc.setHorizontalHeaderLabels([
            'Rang', 'ID', 'id_pa', 'Long. (m)',
            'statut', 'mode_pose', 'nro', 'sro', 'affectation',
        ])
        self._style_table(self.tbl_parc)
        self.tbl_parc.doubleClicked.connect(
            lambda idx: self._zoom_row(self.tbl_parc, idx.row())
        )
        rv.addWidget(self.tbl_parc)

        ab, bz, bs, bc = self._action_bar(self.tbl_parc, 'parc')
        bz.clicked.connect(lambda: self._zoom_selected(self.tbl_parc))
        bs.clicked.connect(lambda: self._select_qgis(self.tbl_parc))
        bc.clicked.connect(lambda: self._export_csv(self.tbl_parc, 'parcours_longs'))
        rv.addWidget(ab)

        h.addWidget(res, 1)
        return root

    # ─────────────────────────────────────────────────────────────────────────
    # TAB 4 – BAL Isolées
    # ─────────────────────────────────────────────────────────────────────────
    def _tab_bal(self):
        root = QWidget()
        h = QHBoxLayout(root)

        cfg = QGroupBox('Configuration')
        cfg.setFixedWidth(290)
        vbox = QVBoxLayout(cfg)

        frm = QFormLayout()

        self.cb_bal = QgsMapLayerComboBox()
        self.cb_bal.setFilters(_F_POINT)
        frm.addRow('Couche BAL :', self.cb_bal)

        self.cb_infra_bal = QgsMapLayerComboBox()
        self.cb_infra_bal.setFilters(_F_LINE)
        frm.addRow('Couche Infra :', self.cb_infra_bal)

        self.le_flt_bal = QLineEdit('"statut" = \'C\' AND "mode_pose" = 0')
        self.le_flt_bal.setToolTip('Filtre sur la couche Infra (C0 par défaut)')
        frm.addRow('Filtre Infra :', self.le_flt_bal)

        self.sp_rayon = QSpinBox()
        self.sp_rayon.setRange(50, 10000)
        self.sp_rayon.setValue(500)
        self.sp_rayon.setSuffix(' m')
        self.sp_rayon.setToolTip(
            'Une BAL est considérée isolée si aucune autre BAL\n'
            'n\'est présente dans ce rayon.'
        )
        frm.addRow('Rayon isolation :', self.sp_rayon)

        vbox.addLayout(frm)

        info = QLabel(
            '<small><i>Les résultats sont triés par distance croissante '
            'à l\'infra C0 la plus proche (les BAL les plus éloignées en premier).</i></small>'
        )
        info.setWordWrap(True)
        vbox.addWidget(info)
        vbox.addStretch()

        btn_run = QPushButton('▶  Lancer l\'analyse')
        btn_run.setStyleSheet('font-weight:bold; padding:6px;')
        btn_run.clicked.connect(self._run_bal)
        vbox.addWidget(btn_run)
        h.addWidget(cfg)

        res = QWidget()
        rv = QVBoxLayout(res)
        rv.setContentsMargins(0, 0, 0, 0)

        sh = QHBoxLayout()
        sh.addWidget(QLabel('Recherche :'))
        self.le_srch_bal = QLineEdit()
        self.le_srch_bal.setPlaceholderText('Filtrer…')
        self.le_srch_bal.textChanged.connect(lambda t: self._filter_table(self.tbl_bal, t))
        sh.addWidget(self.le_srch_bal, 1)
        self.lbl_cnt_bal = QLabel('—')
        sh.addWidget(self.lbl_cnt_bal)
        rv.addLayout(sh)

        self.tbl_bal = QTableWidget(0, 8)
        self.tbl_bal.setHorizontalHeaderLabels([
            'ID BAL', 'Nb voisins BAL',
            'ID Infra proche', 'Dist. infra (m)', 'Long. infra (m)',
            'NRO infra', 'SRO infra', 'id_pa infra',
        ])
        self._style_table(self.tbl_bal)
        self.tbl_bal.doubleClicked.connect(
            lambda idx: self._zoom_row(self.tbl_bal, idx.row())
        )
        rv.addWidget(self.tbl_bal)

        ab, bz, bs, bc = self._action_bar(self.tbl_bal, 'bal')
        bz.clicked.connect(lambda: self._zoom_selected(self.tbl_bal))
        bs.clicked.connect(lambda: self._select_qgis(self.tbl_bal))
        bc.clicked.connect(lambda: self._export_csv(self.tbl_bal, 'bal_isolees'))
        rv.addWidget(ab)

        h.addWidget(res, 1)
        return root

    # ─────────────────────────────────────────────────────────────────────────
    # ANALYSIS – Tab 1: Chevauchements
    # ─────────────────────────────────────────────────────────────────────────
    def _run_chevauchement(self):
        infra_lyr = self.cb_infra_chev.currentLayer()
        if not infra_lyr:
            QMessageBox.warning(self, 'Erreur', 'Sélectionnez la couche Infra.')
            return

        # Collect selected existing layers (chaque couche a son propre filtre)
        existing = []
        for chk, cb, le in [
            (self.chk_ft,   self.cb_ft,   self.le_ft_filter),
            (self.chk_bt,   self.cb_bt,   self.le_bt_filter),
            (self.chk_athd, self.cb_athd, self.le_athd_filter),
            (self.chk_chemi, self.cb_chemi, self.le_chemi_filter),
        ]:
            if chk.isChecked() and cb.currentLayer():
                flt = le.text().strip() or None
                existing.append((cb.currentLayer(), flt))

        if not existing:
            QMessageBox.warning(self, 'Erreur', 'Cochez au moins une couche existante.')
            return

        c0_flt   = self.le_c0_filter.text().strip()
        tol      = self.sp_tol_chev.value()
        min_ov   = self.sp_min_chev.value()

        # Fetch C0 features
        req = QgsFeatureRequest()
        if c0_flt:
            req.setFilterExpression(c0_flt)
        c0_feats = list(infra_lyr.getFeatures(req))
        _infra_fn = infra_lyr.fields().names()
        c0_feats = [f for f in c0_feats if self._in_pm(f, _infra_fn)]

        if not c0_feats:
            QMessageBox.information(self, 'Info',
                f'Aucune entité C0 avec le filtre : {c0_flt or "(vide)"}')
            return

        # Build spatial indices for existing layers
        exist_data = []
        for ex_lyr, ex_flt in existing:
            idx = QgsSpatialIndex()
            fd  = {}
            r2  = QgsFeatureRequest()
            if ex_flt:
                r2.setFilterExpression(ex_flt)
            for ft in ex_lyr.getFeatures(r2):
                idx.insertFeature(ft)
                fd[ft.id()] = ft
            exist_data.append((ex_lyr, idx, fd))

        prog = QProgressDialog(
            'Analyse chevauchements…', 'Annuler', 0, len(c0_feats), self)
        prog.setWindowTitle('Analyse en cours')
        prog.setMinimumDuration(0)
        prog.setWindowModality(Qt.WindowModality.WindowModal)

        results = []
        infra_fnames = infra_lyr.fields().names()

        for i, feat in enumerate(c0_feats):
            prog.setValue(i)
            if prog.wasCanceled():
                break
            if i % 10 == 0:
                prog.setLabelText(f'Chevauchements… {i}/{len(c0_feats)} ({100*i//len(c0_feats)} %)')
                QApplication.processEvents()

            geom = feat.geometry()
            if not geom or geom.isEmpty():
                continue

            bbox = geom.boundingBox()
            bbox.grow(tol + 1)

            for ex_lyr, ex_idx, ex_fd in exist_data:
                for fid2 in ex_idx.intersects(bbox):
                    g2 = ex_fd[fid2].geometry()
                    if not g2 or g2.isEmpty():
                        continue
                    buf = g2.buffer(tol, 8)
                    if not geom.intersects(buf):
                        continue
                    inter = geom.intersection(buf)
                    if not inter or inter.isEmpty():
                        continue
                    ov_len = inter.length()
                    if ov_len < min_ov:
                        continue

                    c0_long = 0.0
                    if 'long' in infra_fnames and feat['long'] is not None:
                        try:
                            c0_long = float(feat['long'])
                        except (TypeError, ValueError):
                            c0_long = geom.length()
                    else:
                        c0_long = geom.length()

                    pct = (ov_len / c0_long * 100) if c0_long > 0 else 0.0

                    results.append(dict(
                        fid       = feat.id(),
                        layer_id  = infra_lyr.id(),
                        idpa      = str(feat['id_pa'])  if 'id_pa'  in infra_fnames and feat['id_pa']  is not None else '',
                        nro       = str(feat['nro'])    if 'nro'    in infra_fnames and feat['nro']    is not None else '',
                        sro       = str(feat['sro'])    if 'sro'    in infra_fnames and feat['sro']    is not None else '',
                        c0_long   = c0_long,
                        ex_name   = ex_lyr.name(),
                        ex_fid    = fid2,
                        ov_len    = ov_len,
                        pct       = pct,
                    ))

        prog.setValue(len(c0_feats))

        self.tbl_chev.setSortingEnabled(False)
        self.tbl_chev.setRowCount(0)

        for r in results:
            row = self.tbl_chev.rowCount()
            self.tbl_chev.insertRow(row)

            cells = [
                _si(r['fid']),
                _si(r['idpa']),
                _si(r['nro']),
                _si(r['sro']),
                _ni(f"{r['c0_long']:.1f}"),
                _si(r['ex_name']),
                _si(r['ex_fid']),
                _ni(f"{r['ov_len']:.1f}"),
                _ni(f"{r['pct']:.1f}%"),
            ]
            for col, item in enumerate(cells):
                self.tbl_chev.setItem(row, col, item)

            # Store IDs in col 0 for zoom/select
            self.tbl_chev.item(row, 0).setData(Qt.ItemDataRole.UserRole + 1, r['fid'])
            self.tbl_chev.item(row, 0).setData(Qt.ItemDataRole.UserRole + 2, r['layer_id'])

        self.tbl_chev.setSortingEnabled(True)
        self.tbl_chev.sortByColumn(7, Qt.SortOrder.DescendingOrder)

        n = len(results)
        self.lbl_cnt_chev.setText(f'{n} conflit(s)')
        self.lbl_status.setText(
            f'Chevauchements : {n} conflit(s) sur {len(c0_feats)} entités C0.')

    # ─────────────────────────────────────────────────────────────────────────
    # ANALYSIS – Tab 2: Doublons
    # ─────────────────────────────────────────────────────────────────────────
    def _run_doublons(self):
        lyr = self.cb_infra_doub.currentLayer()
        if not lyr:
            QMessageBox.warning(self, 'Erreur', 'Sélectionnez la couche Infra.')
            return

        flt    = self.le_flt_doub.text().strip()
        tol    = self.sp_tol_doub.value()
        min_ov = self.sp_min_doub.value()

        req = QgsFeatureRequest()
        if flt:
            req.setFilterExpression(flt)
        feats = list(lyr.getFeatures(req))
        _doub_fn = lyr.fields().names()
        feats = [f for f in feats if self._in_pm(f, _doub_fn)]

        if not feats:
            QMessageBox.information(self, 'Info', 'Aucune entité avec ce filtre.')
            return

        idx = QgsSpatialIndex()
        fd  = {}
        for ft in feats:
            idx.insertFeature(ft)
            fd[ft.id()] = ft

        prog = QProgressDialog(
            'Recherche doublons…', 'Annuler', 0, len(feats), self)
        prog.setWindowTitle('Analyse en cours')
        prog.setMinimumDuration(0)
        prog.setWindowModality(Qt.WindowModality.WindowModal)

        results  = []
        seen     = set()
        fnames   = lyr.fields().names()

        for i, ft1 in enumerate(feats):
            prog.setValue(i)
            if prog.wasCanceled():
                break
            if i % 10 == 0:
                prog.setLabelText(f'Doublons… {i}/{len(feats)} ({100*i//len(feats)} %)')
                QApplication.processEvents()

            g1 = ft1.geometry()
            if not g1 or g1.isEmpty():
                continue

            bbox = g1.boundingBox()
            bbox.grow(tol + 1)

            for fid2 in idx.intersects(bbox):
                if fid2 == ft1.id():
                    continue
                key = (min(ft1.id(), fid2), max(ft1.id(), fid2))
                if key in seen:
                    continue
                seen.add(key)

                ft2 = fd[fid2]
                g2  = ft2.geometry()
                if not g2 or g2.isEmpty():
                    continue

                buf = g1.buffer(tol, 8)
                if not g2.intersects(buf):
                    continue
                inter = g2.intersection(buf)
                if not inter or inter.isEmpty():
                    continue
                ov = inter.length()
                if ov < min_ov:
                    continue

                def _fval(ft, field):
                    return str(ft[field]) if field in fnames and ft[field] is not None else ''

                def _long(ft, g):
                    if 'long' in fnames and ft['long'] is not None:
                        try:
                            return float(ft['long'])
                        except (TypeError, ValueError):
                            pass
                    return g.length()

                results.append(dict(
                    fid1     = ft1.id(),
                    idpa1    = _fval(ft1, 'id_pa'),
                    nro1     = _fval(ft1, 'nro'),
                    long1    = _long(ft1, g1),
                    fid2     = fid2,
                    idpa2    = _fval(ft2, 'id_pa'),
                    nro2     = _fval(ft2, 'nro'),
                    long2    = _long(ft2, g2),
                    ov       = ov,
                    layer_id = lyr.id(),
                ))

        prog.setValue(len(feats))

        self.tbl_doub.setSortingEnabled(False)
        self.tbl_doub.setRowCount(0)

        for r in results:
            row = self.tbl_doub.rowCount()
            self.tbl_doub.insertRow(row)

            cells = [
                _si(r['fid1']),
                _si(r['idpa1']),
                _si(r['nro1']),
                _ni(f"{r['long1']:.1f}"),
                _si(r['fid2']),
                _si(r['idpa2']),
                _si(r['nro2']),
                _ni(f"{r['long2']:.1f}"),
                _ni(f"{r['ov']:.1f}"),
            ]
            for col, item in enumerate(cells):
                self.tbl_doub.setItem(row, col, item)

            self.tbl_doub.item(row, 0).setData(Qt.ItemDataRole.UserRole + 1, r['fid1'])
            self.tbl_doub.item(row, 0).setData(Qt.ItemDataRole.UserRole + 2, r['layer_id'])
            self.tbl_doub.item(row, 4).setData(Qt.ItemDataRole.UserRole + 1, r['fid2'])

        self.tbl_doub.setSortingEnabled(True)
        self.tbl_doub.sortByColumn(8, Qt.SortOrder.DescendingOrder)

        n = len(results)
        self.lbl_cnt_doub.setText(f'{n} doublon(s)')
        self.lbl_status.setText(
            f'Doublons : {n} paire(s) sur {len(feats)} entités.')

    # ─────────────────────────────────────────────────────────────────────────
    # ANALYSIS – Tab 3: Parcours les plus longs
    # ─────────────────────────────────────────────────────────────────────────
    def _run_parcours(self):
        lyr = self.cb_infra_parc.currentLayer()
        if not lyr:
            QMessageBox.warning(self, 'Erreur', 'Sélectionnez une couche.')
            return

        flt   = self.le_flt_parc.text().strip()
        top_n = self.sp_topn.value()

        req = QgsFeatureRequest()
        if flt:
            req.setFilterExpression(flt)

        self.lbl_status.setText('Chargement…')
        QApplication.processEvents()

        fnames = lyr.fields().names()

        def _safe(ft, field):
            return str(ft[field]) if field in fnames and ft[field] is not None else ''

        def _long(ft):
            if 'long' in fnames and ft['long'] is not None:
                try:
                    return float(ft['long'])
                except (TypeError, ValueError):
                    pass
            g = ft.geometry()
            return g.length() if g and not g.isEmpty() else 0.0

        feats = [(f, _long(f)) for f in lyr.getFeatures(req)
                 if self._in_pm(f, fnames)]
        feats.sort(key=lambda x: x[1], reverse=True)
        feats = feats[:top_n]

        self.tbl_parc.setSortingEnabled(False)
        self.tbl_parc.setRowCount(0)

        for rank, (ft, long_v) in enumerate(feats, 1):
            row = self.tbl_parc.rowCount()
            self.tbl_parc.insertRow(row)

            cells = [
                _ni(rank),
                _si(ft.id()),
                _si(_safe(ft, 'id_pa')),
                _ni(f'{long_v:.1f}'),
                _si(_safe(ft, 'statut')),
                _si(_safe(ft, 'mode_pose')),
                _si(_safe(ft, 'nro')),
                _si(_safe(ft, 'sro')),
                _si(_safe(ft, 'affectation')),
            ]
            for col, item in enumerate(cells):
                self.tbl_parc.setItem(row, col, item)

            self.tbl_parc.item(row, 0).setData(Qt.ItemDataRole.UserRole + 1, ft.id())
            self.tbl_parc.item(row, 0).setData(Qt.ItemDataRole.UserRole + 2, lyr.id())

        self.tbl_parc.setSortingEnabled(True)
        n = len(feats)
        self.lbl_cnt_parc.setText(f'{n} parcours')
        self.lbl_status.setText(f'Parcours chargés : {n} (top {top_n}).')

    # ─────────────────────────────────────────────────────────────────────────
    # ANALYSIS – Tab 4: BAL Isolées
    # ─────────────────────────────────────────────────────────────────────────
    def _run_bal(self):
        bal_lyr   = self.cb_bal.currentLayer()
        infra_lyr = self.cb_infra_bal.currentLayer()
        if not bal_lyr or not infra_lyr:
            QMessageBox.warning(self, 'Erreur', 'Sélectionnez les couches BAL et Infra.')
            return

        radius     = float(self.sp_rayon.value())
        infra_flt  = self.le_flt_bal.text().strip()

        self.lbl_status.setText('Construction des index…')
        QApplication.processEvents()

        # BAL index
        bal_idx = QgsSpatialIndex()
        bal_fd  = {}
        for ft in bal_lyr.getFeatures():
            bal_idx.insertFeature(ft)
            bal_fd[ft.id()] = ft

        # Infra index
        infra_idx = QgsSpatialIndex()
        infra_fd  = {}
        req = QgsFeatureRequest()
        if infra_flt:
            req.setFilterExpression(infra_flt)
        for ft in infra_lyr.getFeatures(req):
            infra_idx.insertFeature(ft)
            infra_fd[ft.id()] = ft

        infra_fnames = infra_lyr.fields().names()

        def _infra_safe(ft, field):
            return str(ft[field]) if field in infra_fnames and ft[field] is not None else ''

        def _infra_long(ft):
            if 'long' in infra_fnames and ft['long'] is not None:
                try:
                    return float(ft['long'])
                except (TypeError, ValueError):
                    pass
            g = ft.geometry()
            return g.length() if g and not g.isEmpty() else 0.0

        all_bal = list(bal_fd.values())
        _bal_fn = bal_lyr.fields().names()
        all_bal = [b for b in all_bal if self._in_pm(b, _bal_fn)]

        prog = QProgressDialog(
            'Analyse BAL isolées…', 'Annuler', 0, len(all_bal), self)
        prog.setWindowTitle('Analyse en cours')
        prog.setMinimumDuration(0)
        prog.setWindowModality(Qt.WindowModality.WindowModal)

        results = []

        for i, bal_ft in enumerate(all_bal):
            prog.setValue(i)
            if prog.wasCanceled():
                break
            if i % 50 == 0:
                prog.setLabelText(f'BAL isolées… {i}/{len(all_bal)} ({100*i//len(all_bal)} %)')
                QApplication.processEvents()

            bg = bal_ft.geometry()
            if not bg or bg.isEmpty():
                continue

            # Count BAL neighbors within radius
            bbox = bg.boundingBox()
            bbox.grow(radius)
            nb_voisins = 0
            for nfid in bal_idx.intersects(bbox):
                if nfid == bal_ft.id():
                    continue
                if bg.distance(bal_fd[nfid].geometry()) <= radius:
                    nb_voisins += 1

            if nb_voisins > 0:
                continue

            # Find nearest infra feature (search in 10× radius to be safe)
            search_radius = max(radius * 10, 5000.0)
            ibbox = bg.boundingBox()
            ibbox.grow(search_radius)
            candidates = infra_idx.intersects(ibbox)

            nearest_fid  = None
            nearest_dist = float('inf')
            for ifid in candidates:
                d = bg.distance(infra_fd[ifid].geometry())
                if d < nearest_dist:
                    nearest_dist = d
                    nearest_fid  = ifid

            results.append(dict(
                bal_fid      = bal_ft.id(),
                bal_layer_id = bal_lyr.id(),
                nb_voisins   = nb_voisins,
                infra_fid    = nearest_fid if nearest_fid is not None else -1,
                dist_infra   = nearest_dist if nearest_fid is not None else float('inf'),
                infra_long   = _infra_long(infra_fd[nearest_fid]) if nearest_fid is not None else 0.0,
                infra_nro    = _infra_safe(infra_fd[nearest_fid], 'nro')   if nearest_fid is not None else '',
                infra_sro    = _infra_safe(infra_fd[nearest_fid], 'sro')   if nearest_fid is not None else '',
                infra_idpa   = _infra_safe(infra_fd[nearest_fid], 'id_pa') if nearest_fid is not None else '',
                infra_layer_id = infra_lyr.id(),
            ))

        prog.setValue(len(all_bal))

        # Sort: farthest from infra first (most critical isolated BAL)
        results.sort(key=lambda x: x['dist_infra'], reverse=True)

        self.tbl_bal.setSortingEnabled(False)
        self.tbl_bal.setRowCount(0)

        for r in results:
            row = self.tbl_bal.rowCount()
            self.tbl_bal.insertRow(row)

            dist_s = f"{r['dist_infra']:.1f}" if r['infra_fid'] >= 0 else 'N/A'
            cells = [
                _si(r['bal_fid']),
                _ni(r['nb_voisins']),
                _si(r['infra_fid'] if r['infra_fid'] >= 0 else 'N/A'),
                _ni(dist_s),
                _ni(f"{r['infra_long']:.1f}"),
                _si(r['infra_nro']),
                _si(r['infra_sro']),
                _si(r['infra_idpa']),
            ]
            for col, item in enumerate(cells):
                self.tbl_bal.setItem(row, col, item)

            self.tbl_bal.item(row, 0).setData(Qt.ItemDataRole.UserRole + 1, r['bal_fid'])
            self.tbl_bal.item(row, 0).setData(Qt.ItemDataRole.UserRole + 2, r['bal_layer_id'])

        self.tbl_bal.setSortingEnabled(True)

        n = len(results)
        self.lbl_cnt_bal.setText(f'{n} BAL isolée(s)')
        self.lbl_status.setText(
            f'BAL isolées : {n} sur {len(all_bal)} BAL totales.')

    # ─────────────────────────────────────────────────────────────────────────
    # ACTIONS – Zoom / Select / Flash / Export
    # ─────────────────────────────────────────────────────────────────────────
    def _get_row_ids(self, tbl, row):
        """Return (layer, fid) stored in col 0 of the given row."""
        item = tbl.item(row, 0)
        if not item:
            return None, None
        fid      = item.data(Qt.ItemDataRole.UserRole + 1)
        layer_id = item.data(Qt.ItemDataRole.UserRole + 2)
        if fid is None or layer_id is None:
            return None, None
        return QgsProject.instance().mapLayer(layer_id), fid

    def _zoom_to_feature(self, layer, fid, padding=50.0):
        feats = list(layer.getFeatures(QgsFeatureRequest().setFilterFid(fid)))
        if not feats:
            return
        geom = feats[0].geometry()
        if not geom or geom.isEmpty():
            return
        bbox = geom.boundingBox()
        pad  = max(bbox.width() * 0.15, bbox.height() * 0.15, padding)
        bbox.grow(pad)
        iface.mapCanvas().setExtent(bbox)
        iface.mapCanvas().refresh()
        try:
            iface.mapCanvas().flashFeatureIds(layer, [fid])
        except Exception:
            pass

    def _zoom_row(self, tbl, row):
        lyr, fid = self._get_row_ids(tbl, row)
        if lyr and fid is not None:
            self._zoom_to_feature(lyr, fid)

    def _zoom_selected(self, tbl):
        rows = tbl.selectionModel().selectedRows()
        if not rows:
            QMessageBox.information(self, 'Info', 'Sélectionnez une ligne.')
            return
        self._zoom_row(tbl, rows[0].row())

    def _select_qgis(self, tbl):
        rows = tbl.selectionModel().selectedRows()
        if not rows:
            QMessageBox.information(self, 'Info', 'Sélectionnez une ligne.')
            return
        lyr, fid = self._get_row_ids(tbl, rows[0].row())
        if not lyr or fid is None:
            return
        for l in QgsProject.instance().mapLayers().values():
            if hasattr(l, 'removeSelection'):
                l.removeSelection()
        lyr.selectByIds([fid])
        iface.setActiveLayer(lyr)

    # Doublons-specific: zoom both features
    def _zoom_row_doub(self, row):
        if row < 0:
            return
        item0 = self.tbl_doub.item(row, 0)
        item4 = self.tbl_doub.item(row, 4)
        if not item0:
            return
        fid1     = item0.data(Qt.ItemDataRole.UserRole + 1)
        layer_id = item0.data(Qt.ItemDataRole.UserRole + 2)
        fid2     = item4.data(Qt.ItemDataRole.UserRole + 1) if item4 else None
        lyr      = QgsProject.instance().mapLayer(layer_id) if layer_id else None
        if not lyr:
            return
        fids = [f for f in (fid1, fid2) if f is not None]
        feats = list(lyr.getFeatures(QgsFeatureRequest().setFilterFids(fids)))
        if not feats:
            return
        boxes = [ft.geometry().boundingBox() for ft in feats]
        bbox = QgsRectangle(boxes[0])
        for _b in boxes[1:]:
            bbox.combineExtentWith(_b)
        pad  = max(bbox.width() * 0.15, bbox.height() * 0.15, 50.0)
        bbox.grow(pad)
        iface.mapCanvas().setExtent(bbox)
        iface.mapCanvas().refresh()
        try:
            iface.mapCanvas().flashFeatureIds(lyr, fids)
        except Exception:
            pass

    def _select_qgis_doub(self):
        rows = self.tbl_doub.selectionModel().selectedRows()
        if not rows:
            QMessageBox.information(self, 'Info', 'Sélectionnez une ligne.')
            return
        row  = rows[0].row()
        item0 = self.tbl_doub.item(row, 0)
        item4 = self.tbl_doub.item(row, 4)
        if not item0:
            return
        fid1     = item0.data(Qt.ItemDataRole.UserRole + 1)
        layer_id = item0.data(Qt.ItemDataRole.UserRole + 2)
        fid2     = item4.data(Qt.ItemDataRole.UserRole + 1) if item4 else None
        lyr      = QgsProject.instance().mapLayer(layer_id) if layer_id else None
        if not lyr:
            return
        for l in QgsProject.instance().mapLayers().values():
            if hasattr(l, 'removeSelection'):
                l.removeSelection()
        fids = [f for f in (fid1, fid2) if f is not None]
        lyr.selectByIds(fids)
        iface.setActiveLayer(lyr)

    def _clear_selection(self):
        for lyr in QgsProject.instance().mapLayers().values():
            if hasattr(lyr, 'removeSelection'):
                lyr.removeSelection()

    def _export_csv(self, tbl, name):
        path, _ = QFileDialog.getSaveFileName(
            self, 'Exporter CSV', f'{name}.csv', 'CSV (*.csv)')
        if not path:
            return
        try:
            with open(path, 'w', newline='', encoding='utf-8-sig') as f:
                w = csv.writer(f, delimiter=';')
                w.writerow([
                    (tbl.horizontalHeaderItem(c).text()
                     if tbl.horizontalHeaderItem(c) else f'Col{c}')
                    for c in range(tbl.columnCount())
                ])
                for row in range(tbl.rowCount()):
                    if tbl.isRowHidden(row):
                        continue
                    w.writerow([
                        (tbl.item(row, c).text() if tbl.item(row, c) else '')
                        for c in range(tbl.columnCount())
                    ])
            QMessageBox.information(self, 'Export', f'Fichier exporté :\n{path}')
        except Exception as e:
            QMessageBox.critical(self, 'Erreur export', str(e))

    # ─────────────────────────────────────────────────────────────────────────
    # REPORT GENERATION
    # ─────────────────────────────────────────────────────────────────────────

    def _collect_table_data(self, tbl):
        """Return visible rows as list of dicts keyed by column headers."""
        if tbl.rowCount() == 0:
            return []
        headers = [
            (tbl.horizontalHeaderItem(c).text() if tbl.horizontalHeaderItem(c) else f'Col{c}')
            for c in range(tbl.columnCount())
        ]
        rows = []
        for row in range(tbl.rowCount()):
            if tbl.isRowHidden(row):
                continue
            rows.append({
                headers[c]: (tbl.item(row, c).text() if tbl.item(row, c) else '')
                for c in range(tbl.columnCount())
            })
        return rows

    def _make_charts(self, chev, doub, parc, bal):
        """Generate base64-encoded PNG charts via matplotlib. Returns dict of name→data URI."""
        charts = {}
        try:
            import matplotlib
            matplotlib.use('Agg')
            import matplotlib.pyplot as plt
            from io import BytesIO
            import base64

            COLORS = ['#1a6faf', '#3498db', '#5dade2', '#85c1e9', '#aed6f1']
            BG = '#f8f9fa'

            def _fig_to_uri(fig):
                buf = BytesIO()
                fig.savefig(buf, format='png', dpi=150, bbox_inches='tight',
                            facecolor=BG, edgecolor='none')
                buf.seek(0)
                enc = base64.b64encode(buf.read()).decode()
                plt.close(fig)
                return f'data:image/png;base64,{enc}'

            # ── Chart 1: Chevauchements by conflicting layer ──────────────────
            if chev:
                cnt = Counter(r.get('Couche conf.', '—') for r in chev)
                labels = list(cnt.keys())
                vals   = list(cnt.values())
                fig, ax = plt.subplots(figsize=(7, max(2, len(labels) * 0.5 + 1)))
                fig.patch.set_facecolor(BG)
                ax.set_facecolor(BG)
                bars = ax.barh(labels, vals, color=COLORS[:len(labels)], height=0.55)
                ax.bar_label(bars, padding=4, fontsize=9)
                ax.set_xlabel('Nombre de conflits', fontsize=9)
                ax.set_title('Conflits par couche existante', fontsize=11, fontweight='bold', pad=10)
                ax.spines['top'].set_visible(False)
                ax.spines['right'].set_visible(False)
                ax.tick_params(labelsize=9)
                plt.tight_layout()
                charts['chev_layers'] = _fig_to_uri(fig)

            # ── Chart 2: Overlap length distribution ─────────────────────────
            if chev:
                lengths = []
                for r in chev:
                    try:
                        lengths.append(float(r.get('Chevauch. (m)', '0').replace(',', '.')))
                    except ValueError:
                        pass
                if lengths:
                    fig, ax = plt.subplots(figsize=(7, 3.5))
                    fig.patch.set_facecolor(BG)
                    ax.set_facecolor(BG)
                    _, _, patches = ax.hist(lengths, bins=20, color=COLORS[0],
                                             edgecolor='white', linewidth=0.5)
                    for p in patches:
                        h = p.get_height()
                        if h > 0:
                            ax.text(p.get_x() + p.get_width() / 2., h,
                                    str(int(h)), ha='center', va='bottom', fontsize=7)
                    ax.set_xlabel('Longueur de chevauchement (m)', fontsize=9)
                    ax.set_ylabel('Fréquence', fontsize=9)
                    ax.set_title('Distribution des longueurs de chevauchement', fontsize=11,
                                 fontweight='bold', pad=10)
                    ax.spines['top'].set_visible(False)
                    ax.spines['right'].set_visible(False)
                    ax.tick_params(labelsize=9)
                    plt.tight_layout()
                    charts['chev_hist'] = _fig_to_uri(fig)

            # ── Chart 3: Top 10 longest parcours ─────────────────────────────
            if parc:
                top10 = parc[:10]
                labels = [r.get('id_pa', r.get('ID', f'#{i}')) for i, r in enumerate(top10)]
                vals   = []
                for r in top10:
                    try:
                        vals.append(float(r.get('Long. (m)', '0').replace(',', '.')))
                    except ValueError:
                        vals.append(0.0)
                fig, ax = plt.subplots(figsize=(7, max(2.5, len(labels) * 0.55 + 1)))
                fig.patch.set_facecolor(BG)
                ax.set_facecolor(BG)
                bars = ax.barh(labels[::-1], vals[::-1], color=COLORS[1], height=0.55)
                ax.bar_label(bars, fmt='%.1f m', padding=4, fontsize=8)
                ax.set_xlabel('Longueur (m)', fontsize=9)
                ax.set_title('Top 10 des parcours les plus longs', fontsize=11,
                             fontweight='bold', pad=10)
                ax.spines['top'].set_visible(False)
                ax.spines['right'].set_visible(False)
                ax.tick_params(labelsize=9)
                plt.tight_layout()
                charts['parc_top10'] = _fig_to_uri(fig)

            # ── Chart 4: BAL isolation distances ─────────────────────────────
            if bal:
                dists = []
                for r in bal:
                    try:
                        v = r.get('Dist. infra (m)', 'N/A')
                        if v != 'N/A':
                            dists.append(float(v.replace(',', '.')))
                    except ValueError:
                        pass
                if dists:
                    fig, ax = plt.subplots(figsize=(7, 3.5))
                    fig.patch.set_facecolor(BG)
                    ax.set_facecolor(BG)
                    _, _, patches = ax.hist(dists, bins=15, color='#e67e22',
                                             edgecolor='white', linewidth=0.5)
                    for p in patches:
                        h = p.get_height()
                        if h > 0:
                            ax.text(p.get_x() + p.get_width() / 2., h,
                                    str(int(h)), ha='center', va='bottom', fontsize=7)
                    ax.set_xlabel("Distance à l'infra C0 la plus proche (m)", fontsize=9)
                    ax.set_ylabel('Nombre de BAL', fontsize=9)
                    ax.set_title('Distribution des distances BAL isolées → Infra C0', fontsize=11,
                                 fontweight='bold', pad=10)
                    ax.spines['top'].set_visible(False)
                    ax.spines['right'].set_visible(False)
                    ax.tick_params(labelsize=9)
                    plt.tight_layout()
                    charts['bal_dist'] = _fig_to_uri(fig)

        except Exception:
            pass
        return charts

    def _build_report_html(self, chev, doub, parc, bal, charts):
        """Assemble the full HTML report string."""
        now = datetime.datetime.now().strftime('%d/%m/%Y à %H:%M')

        # Key figures
        n_chev = len(chev)
        n_doub = len(doub)
        n_parc = len(parc)
        n_bal  = len(bal)

        total_ov_chev = 0.0
        for r in chev:
            try:
                total_ov_chev += float(r.get('Chevauch. (m)', '0').replace(',', '.'))
            except ValueError:
                pass

        total_ov_doub = 0.0
        for r in doub:
            try:
                total_ov_doub += float(r.get('Chevauch. (m)', '0').replace(',', '.'))
            except ValueError:
                pass

        total_len_parc = 0.0
        for r in parc:
            try:
                total_len_parc += float(r.get('Long. (m)', '0').replace(',', '.'))
            except ValueError:
                pass

        pm_info = (f'{len(self._pm_set)} PM' if self.chk_pm.isChecked() and self._pm_set
                   else 'Aucun filtre PM')

        def _kpi(value, label, color='#1a6faf', sub=''):
            return f'''
            <div class="kpi">
                <div class="kpi-value" style="color:{color}">{value}</div>
                <div class="kpi-label">{label}</div>
                {f'<div class="kpi-sub">{sub}</div>' if sub else ''}
            </div>'''

        def _chart(key, caption=''):
            if key not in charts:
                return ''
            return f'''
            <figure class="chart">
                <img src="{charts[key]}" alt="{caption}">
                {f'<figcaption>{caption}</figcaption>' if caption else ''}
            </figure>'''

        def _table_html(data, max_rows=50):
            if not data:
                return '<p class="empty">Aucune donnée disponible.</p>'
            headers = list(data[0].keys())
            rows_html = ''
            for r in data[:max_rows]:
                cells = ''.join(f'<td>{r.get(h, "")}</td>' for h in headers)
                rows_html += f'<tr>{cells}</tr>'
            if len(data) > max_rows:
                rows_html += (
                    f'<tr><td colspan="{len(headers)}" class="more">'
                    f'… et {len(data) - max_rows} ligne(s) supplémentaire(s) — '
                    f'voir export CSV pour la liste complète.</td></tr>'
                )
            head = ''.join(f'<th>{h}</th>' for h in headers)
            return f'<table><thead><tr>{head}</tr></thead><tbody>{rows_html}</tbody></table>'

        chev_section = ''
        if chev or n_chev == 0:
            layer_cnt = Counter(r.get('Couche conf.', '—') for r in chev)
            layer_pills = ' '.join(
                f'<span class="pill">{k}: <strong>{v}</strong></span>'
                for k, v in layer_cnt.most_common()
            )
            chev_section = f'''
            <section>
                <h2>⚠ Chevauchements C0 / Existant</h2>
                <div class="kpi-row">
                    {_kpi(n_chev, 'Conflits détectés',
                          '#c0392b' if n_chev > 0 else '#27ae60')}
                    {_kpi(f'{total_ov_chev:,.1f} m', 'Cumul chevauch.')}
                </div>
                {'<div class="pills">' + layer_pills + '</div>' if layer_pills else ''}
                {_chart('chev_layers', 'Répartition des conflits par couche existante')}
                {_chart('chev_hist', 'Distribution des longueurs de chevauchement')}
            </section>'''

        doub_section = ''
        if doub or n_doub == 0:
            doub_section = f'''
            <section>
                <h2>⛔ Doublons Infra</h2>
                <div class="kpi-row">
                    {_kpi(n_doub, 'Paires en doublon',
                          '#c0392b' if n_doub > 0 else '#27ae60')}
                    {_kpi(f'{total_ov_doub:,.1f} m', 'Cumul superposition')}
                </div>
            </section>'''

        parc_section = ''
        if parc or n_parc == 0:
            parc_section = f'''
            <section>
                <h2>📏 Parcours les plus longs</h2>
                <div class="kpi-row">
                    {_kpi(n_parc, 'Parcours listés')}
                    {_kpi(f'{total_len_parc:,.0f} m', 'Longueur totale')}
                </div>
                {_chart('parc_top10', 'Top 10 des parcours les plus longs')}
            </section>'''

        bal_section = ''
        if bal or n_bal == 0:
            avg_dist = ''
            dists = []
            for r in bal:
                try:
                    v = r.get('Dist. infra (m)', 'N/A')
                    if v != 'N/A':
                        dists.append(float(v.replace(',', '.')))
                except ValueError:
                    pass
            if dists:
                avg_dist = f'{sum(dists)/len(dists):,.1f} m'
            bal_section = f'''
            <section>
                <h2>📍 BAL Isolées</h2>
                <div class="kpi-row">
                    {_kpi(n_bal, 'BAL isolées',
                          '#c0392b' if n_bal > 0 else '#27ae60')}
                    {_kpi(avg_dist or "—", "Distance moy. à l'infra")}
                </div>
                {_chart('bal_dist', 'Distribution des distances BAL → Infra C0')}
            </section>'''

        no_data_warn = ''
        if not (chev or doub or parc or bal):
            no_data_warn = '''
            <div class="warn">
                Aucune analyse n'a encore été lancée. Veuillez exécuter les analyses
                depuis les différents onglets puis régénérer le rapport.
            </div>'''

        html = f'''<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Rapport QD RIP Auvergne — {now}</title>
<style>
  :root {{
    --primary: #1a6faf;
    --primary-light: #3498db;
    --accent: #e67e22;
    --danger: #c0392b;
    --success: #27ae60;
    --bg: #f8f9fa;
    --card-bg: #ffffff;
    --border: #dee2e6;
    --text: #212529;
    --muted: #6c757d;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: 'Segoe UI', Arial, sans-serif;
    background: var(--bg);
    color: var(--text);
    font-size: 14px;
    line-height: 1.5;
  }}
  header {{
    background: linear-gradient(135deg, var(--primary) 0%, var(--primary-light) 100%);
    color: white;
    padding: 32px 40px;
    print-color-adjust: exact;
    -webkit-print-color-adjust: exact;
  }}
  header h1 {{
    font-size: 26px;
    font-weight: 700;
    margin-bottom: 6px;
  }}
  header .subtitle {{
    font-size: 14px;
    opacity: 0.85;
  }}
  header .meta {{
    margin-top: 12px;
    font-size: 12px;
    opacity: 0.75;
    display: flex;
    gap: 24px;
  }}
  main {{ max-width: 1100px; margin: 32px auto; padding: 0 24px; }}
  .warn {{
    background: #fff3cd;
    border-left: 4px solid #ffc107;
    padding: 14px 18px;
    border-radius: 4px;
    margin-bottom: 24px;
  }}
  section {{
    background: var(--card-bg);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 24px;
    margin-bottom: 32px;
    box-shadow: 0 2px 4px rgba(0,0,0,.06);
  }}
  section h2 {{
    font-size: 18px;
    font-weight: 700;
    color: var(--primary);
    border-bottom: 2px solid var(--border);
    padding-bottom: 10px;
    margin-bottom: 18px;
  }}
  section h3 {{
    font-size: 14px;
    font-weight: 600;
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: .05em;
    margin: 20px 0 10px;
  }}
  .kpi-row {{
    display: flex;
    gap: 16px;
    flex-wrap: wrap;
    margin-bottom: 20px;
  }}
  .kpi {{
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 16px 24px;
    min-width: 140px;
    text-align: center;
  }}
  .kpi-value {{
    font-size: 28px;
    font-weight: 800;
    line-height: 1.1;
  }}
  .kpi-label {{
    font-size: 12px;
    color: var(--muted);
    margin-top: 4px;
    text-transform: uppercase;
    letter-spacing: .04em;
  }}
  .kpi-sub {{ font-size: 11px; color: var(--muted); margin-top: 2px; }}
  .pills {{ display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 16px; }}
  .pill {{
    background: #e8f4f8;
    border: 1px solid #b8dff0;
    border-radius: 20px;
    padding: 4px 12px;
    font-size: 12px;
    color: var(--primary);
  }}
  figure.chart {{
    margin: 16px 0;
    text-align: center;
  }}
  figure.chart img {{
    max-width: 100%;
    border-radius: 6px;
    border: 1px solid var(--border);
  }}
  figcaption {{
    font-size: 12px;
    color: var(--muted);
    margin-top: 6px;
    font-style: italic;
  }}
  table {{
    width: 100%;
    border-collapse: collapse;
    font-size: 12px;
  }}
  thead th {{
    background: var(--primary);
    color: white;
    padding: 8px 10px;
    text-align: left;
    font-weight: 600;
    white-space: nowrap;
  }}
  tbody tr:nth-child(even) {{ background: #f0f4f8; }}
  tbody tr:hover {{ background: #dbeafe; }}
  tbody td {{
    padding: 6px 10px;
    border-bottom: 1px solid var(--border);
    white-space: nowrap;
  }}
  .more {{ color: var(--muted); font-style: italic; text-align: center; }}
  p.empty {{ color: var(--muted); font-style: italic; }}
  footer {{
    text-align: center;
    padding: 24px;
    color: var(--muted);
    font-size: 12px;
    border-top: 1px solid var(--border);
    margin-top: 24px;
  }}
  @media print {{
    body {{ font-size: 11px; }}
    header {{ padding: 20px; }}
    section {{ break-inside: avoid; box-shadow: none; }}
    main {{ margin: 16px; padding: 0; max-width: none; }}
  }}
</style>
</head>
<body>
<header>
  <h1>Rapport de Contrôle Qualité — RIP Auvergne</h1>
  <div class="subtitle">Analyse de la conformité des données fibre optique C0</div>
  <div class="meta">
    <span>📅 Généré le {now}</span>
    <span>🗺 Périmètre : {pm_info}</span>
    <span>🔧 Plugin QD RIP Auvergne v1.0.6</span>
  </div>
</header>
<main>
{no_data_warn}
<section>
  <h2>Synthèse générale</h2>
  <div class="kpi-row">
    {_kpi(n_chev, 'Chevauchements',
          '#c0392b' if n_chev > 0 else '#27ae60',
          f'{total_ov_chev:,.1f} m cumulés')}
    {_kpi(n_doub, 'Doublons',
          '#c0392b' if n_doub > 0 else '#27ae60',
          f'{total_ov_doub:,.1f} m cumulés')}
    {_kpi(n_parc, 'Parcours listés', '#1a6faf',
          f'{total_len_parc:,.0f} m total')}
    {_kpi(n_bal, 'BAL isolées',
          '#c0392b' if n_bal > 0 else '#27ae60')}
  </div>
</section>
{chev_section}
{doub_section}
{parc_section}
{bal_section}
</main>
<footer>
  Rapport généré par le plugin <strong>QD RIP Auvergne</strong> — Pleymove
  &nbsp;|&nbsp; {now}
  &nbsp;|&nbsp; Pour imprimer en PDF : Fichier → Imprimer → Enregistrer en PDF
</footer>
</body>
</html>'''
        return html

    def _generate_report(self):
        """Collect analysis results, build HTML report and open in browser."""
        chev = self._collect_table_data(self.tbl_chev)
        doub = self._collect_table_data(self.tbl_doub)
        parc = self._collect_table_data(self.tbl_parc)
        bal  = self._collect_table_data(self.tbl_bal)

        if not (chev or doub or parc or bal):
            ans = QMessageBox.question(
                self, 'Rapport — aucune donnée',
                'Aucune analyse n\'a encore été lancée.\n\n'
                'Générer un rapport vide à titre d\'illustration ?',
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if ans != QMessageBox.StandardButton.Yes:
                return

        self.lbl_status.setText('Génération du rapport…')
        QApplication.processEvents()

        charts = self._make_charts(chev, doub, parc, bal)
        html   = self._build_report_html(chev, doub, parc, bal, charts)

        # Ask where to save
        path, _ = QFileDialog.getSaveFileName(
            self, 'Enregistrer le rapport HTML',
            f'rapport_qd_rip_{datetime.datetime.now().strftime("%Y%m%d_%H%M")}.html',
            'Rapport HTML (*.html)',
        )
        if not path:
            self.lbl_status.setText('Rapport annulé.')
            return

        try:
            with open(path, 'w', encoding='utf-8') as f:
                f.write(html)
        except Exception as e:
            QMessageBox.critical(self, 'Erreur', f'Impossible d\'écrire le fichier :\n{e}')
            self.lbl_status.setText('Erreur export rapport.')
            return

        self.lbl_status.setText(f'Rapport enregistré : {os.path.basename(path)}')
        QDesktopServices.openUrl(QUrl.fromLocalFile(path))
