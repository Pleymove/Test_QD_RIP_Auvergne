"""
QD RIP Auvergne – Contrôle qualité
Onglets :
  1. Chevauchements C0 / couches existantes
  2. Doublons dans l'infra (parcours superposés)
  3. Parcours les plus longs
  4. BAL éloignées infra (distance BAL→infra par PM, rayon isolation optionnel)
  5. PA sans infra (ZAPA sans infra dans le groupement livrables)
"""

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
    QSplitter, QPlainTextEdit, QDialogButtonBox, QTextBrowser, QComboBox,
    QStackedWidget,
)
from qgis.PyQt.QtCore import Qt, QUrl
from qgis.PyQt.QtGui import QColor, QBrush, QFont, QDesktopServices

from qgis.core import (
    QgsProject, QgsSpatialIndex, QgsFeatureRequest, QgsRectangle,
    QgsVectorFileWriter, QgsFeature, QgsCoordinateTransform,
)
from qgis.gui import QgsMapLayerComboBox
from qgis.utils import iface

from .pm_perimeter import DEFAULT_PM_CODES

# Resolution du filtre de couche, compatible QGIS 3.16 -> 4.x
try:
    from qgis.core import Qgis
    _F_LINE  = Qgis.LayerFilter.LineLayer
    _F_POINT = Qgis.LayerFilter.PointLayer
    _F_POLY  = Qgis.LayerFilter.PolygonLayer
except (ImportError, AttributeError):
    try:
        from qgis.core import QgsMapLayerProxyModel
    except ImportError:
        from qgis.gui import QgsMapLayerProxyModel
    _F_LINE  = QgsMapLayerProxyModel.LineLayer
    _F_POINT = QgsMapLayerProxyModel.PointLayer
    _F_POLY  = QgsMapLayerProxyModel.PolygonLayer


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
        'infra':           ['infra_c03e1bf7', 'infra'],
        'ft_arciti':       ['ft_arciti_53374007', 'ft_arciti'],
        'bt':              ['bt_def0d723', 'bt'],
        'athd_artere':     ['athd_artere_ab4dbaf5', 'athd_artere'],
        't_cheminement':   ['t_cheminement_aa3c43e0', 't_cheminement'],
        'bal':             ['bal_442ddc78', 'bal'],
        'za_sro':          ['za_sro'],
        'livrable_zapa':   ['livrable_zapa'],
        'livrable_infra':  ['livrable_infra'],
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pm_codes = list(DEFAULT_PM_CODES)
        self._pm_set = set(self._pm_codes)
        self._pa_ignored_empty_zapa = 0
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

    # ── Smart detection helpers for PA sans infra tab ──────────────────────

    def _layer_source_contains_table(self, lyr, table_fragment):
        """Return True if the layer's PostGIS source string contains table_fragment."""
        try:
            src = lyr.source().lower()
            return table_fragment.lower() in src
        except Exception:
            return False

    def _has_fields(self, lyr, *field_names):
        """Return True if lyr exposes all requested field names."""
        try:
            names = lyr.fields().names()
            return all(f in names for f in field_names)
        except Exception:
            return False

    def _find_pa_zapa_layer(self):
        """Locate the ZAPA polygon layer for the PA sans infra tab.

        Priority:
          1. PostGIS source contains 'table="rad_aw_2026"."zapa"'
          2. Exact name == 'zapa'  (polygon, has id_metier + sro)
          3. Name contains 'livrable_zapa'
        """
        from qgis.core import QgsWkbTypes
        candidates_p2 = []
        candidates_p3 = []

        for lyr in QgsProject.instance().mapLayers().values():
            if not hasattr(lyr, 'geometryType'):
                continue
            try:
                if lyr.geometryType() != QgsWkbTypes.GeometryType.PolygonGeometry:
                    continue
            except Exception:
                continue

            # Priority 1 — PostGIS source
            if self._layer_source_contains_table(lyr, 'table="rad_aw_2026"."zapa"'):
                return lyr

            name = lyr.name().strip().lower()

            # Priority 2 — exact name + field validation
            if name == 'zapa' and self._has_fields(lyr, 'id_metier', 'sro'):
                candidates_p2.append(lyr)

            # Priority 3 — livrable_zapa substring
            if 'livrable_zapa' in name:
                candidates_p3.append(lyr)

        if candidates_p2:
            return candidates_p2[0]
        if candidates_p3:
            return candidates_p3[0]
        return None

    def _find_pa_infra_layer(self):
        """Locate the infra line layer for the PA sans infra tab.

        Priority:
          1. PostGIS source contains 'table="rad_aw_2026"."infra"'
          2. Exact name == 'infra'  (line, has id_pa)
          3. Name contains 'livrable_infra'
        """
        from qgis.core import QgsWkbTypes
        candidates_p2 = []
        candidates_p3 = []

        for lyr in QgsProject.instance().mapLayers().values():
            if not hasattr(lyr, 'geometryType'):
                continue
            try:
                if lyr.geometryType() != QgsWkbTypes.GeometryType.LineGeometry:
                    continue
            except Exception:
                continue

            # Priority 1 — PostGIS source
            if self._layer_source_contains_table(lyr, 'table="rad_aw_2026"."infra"'):
                return lyr

            name = lyr.name().strip().lower()

            # Priority 2 — exact name + field validation
            if name == 'infra' and self._has_fields(lyr, 'id_pa'):
                candidates_p2.append(lyr)

            # Priority 3 — livrable_infra substring
            if 'livrable_infra' in name:
                candidates_p3.append(lyr)

        if candidates_p2:
            return candidates_p2[0]
        if candidates_p3:
            return candidates_p3[0]
        return None

    def _find_pa_bal_layer(self):
        """Locate the BAL point layer for the PA sans infra tab.

        Priority:
          1. PostGIS source contains 'table="rad_aw_2026"."bal"'
          2. Exact name == 'bal'  (point, has fields zapa + sro)
          3. Name contains 'bal'  (point, has fields zapa + sro)
        """
        from qgis.core import QgsWkbTypes
        candidates_p2 = []
        candidates_p3 = []

        for lyr in QgsProject.instance().mapLayers().values():
            if not hasattr(lyr, 'geometryType'):
                continue
            try:
                if lyr.geometryType() != QgsWkbTypes.GeometryType.PointGeometry:
                    continue
            except Exception:
                continue

            # Priority 1 — PostGIS source
            if self._layer_source_contains_table(lyr, 'table="rad_aw_2026"."bal"'):
                return lyr

            # Fields zapa + sro required for priorities 2 and 3
            if not self._has_fields(lyr, 'zapa', 'sro'):
                continue

            name = lyr.name().strip().lower()

            # Priority 2 — exact name
            if name == 'bal':
                candidates_p2.append(lyr)

            # Priority 3 — name contains 'bal'
            elif 'bal' in name:
                candidates_p3.append(lyr)

        if candidates_p2:
            return candidates_p2[0]
        if candidates_p3:
            return candidates_p3[0]
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

        zasro = self._find_layer(*hints['za_sro'])
        if zasro:
            self.cb_zasro.setLayer(zasro)

        pa_zapa = self._find_pa_zapa_layer()
        if pa_zapa:
            self.cb_zapa.setLayer(pa_zapa)

        pa_infra = self._find_pa_infra_layer()
        if pa_infra:
            self.cb_infra_pa.setLayer(pa_infra)

        pa_bal = self._find_pa_bal_layer()
        if pa_bal:
            self.cb_bal_pa.setLayer(pa_bal)

        epa = self._find_extraction_pa_layer()
        if epa:
            self.cb_epa.setLayer(epa)

        bal_ext = self._find_extraction_bal_layer()
        if bal_ext:
            self.cb_bal_extract.setLayer(bal_ext)

    def _find_extraction_pa_layer(self):
        """Locate the EPA/PA point layer for the Extractions tab.

        Priority:
          1. PostGIS source contains 'table="rad_aw_2026"."pa"' (not zapa)
          2. Exact name in ('pa', 'georeso_pa', 'livrable_pa')
          3. Name contains 'pa' but NOT 'zapa'  (point geometry)
        """
        from qgis.core import QgsWkbTypes
        EXACT_NAMES = ('pa', 'georeso_pa', 'livrable_pa')
        candidates_p2 = []
        candidates_p3 = []

        for lyr in QgsProject.instance().mapLayers().values():
            if not hasattr(lyr, 'geometryType'):
                continue
            try:
                if lyr.geometryType() != QgsWkbTypes.GeometryType.PointGeometry:
                    continue
            except Exception:
                continue

            src = ''
            try:
                src = lyr.source().lower()
            except Exception:
                pass

            # Priority 1 — PostGIS source: pa table, explicitly not zapa
            if 'table="rad_aw_2026"."pa"' in src and '"zapa"' not in src:
                return lyr

            name = lyr.name().strip().lower()
            if 'zapa' in name:
                continue

            # Priority 2 — exact name
            if name in EXACT_NAMES:
                candidates_p2.append(lyr)
            # Priority 3 — name contains 'pa' (zapa already excluded)
            elif 'pa' in name:
                candidates_p3.append(lyr)

        if candidates_p2:
            return candidates_p2[0]
        if candidates_p3:
            return candidates_p3[0]
        return None

    def _find_extraction_bal_layer(self):
        """Locate the BAL point layer for the Extractions tab.

        Priority:
          1. PostGIS source contains 'table="rad_aw_2026"."bal"'
          2. Exact name in ('bal', 'georeso_bal', 'livrable_bal')
          3. Name contains 'bal'  (point geometry)
        """
        from qgis.core import QgsWkbTypes
        EXACT_NAMES = ('bal', 'georeso_bal', 'livrable_bal')
        candidates_p2 = []
        candidates_p3 = []

        for lyr in QgsProject.instance().mapLayers().values():
            if not hasattr(lyr, 'geometryType'):
                continue
            try:
                if lyr.geometryType() != QgsWkbTypes.GeometryType.PointGeometry:
                    continue
            except Exception:
                continue

            src = ''
            try:
                src = lyr.source().lower()
            except Exception:
                pass

            # Priority 1 — PostGIS source
            if 'table="rad_aw_2026"."bal"' in src:
                return lyr

            name = lyr.name().strip().lower()

            # Priority 2 — exact name
            if name in EXACT_NAMES:
                candidates_p2.append(lyr)
            # Priority 3 — name contains 'bal'
            elif 'bal' in name:
                candidates_p3.append(lyr)

        if candidates_p2:
            return candidates_p2[0]
        if candidates_p3:
            return candidates_p3[0]
        return None

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
        self.tabs.addTab(self._tab_chevauchement(),    '⚠  Chevauchements C0 / Existant')
        self.tabs.addTab(self._tab_doublons(),         '⛔  Doublons Infra')
        self.tabs.addTab(self._tab_parcours(),         '📏  Parcours les plus longs')
        self.tabs.addTab(self._tab_bal(),              '📍  BAL éloignées infra')
        self.tabs.addTab(self._tab_pa_sans_infra(),    '🚫  PA sans infra')
        self.tabs.addTab(self._tab_extractions(),      '📤  Extractions')
        self.tabs.addTab(self._tab_rapport(),          '📊  Tableau de bord')
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
        """Return a QWidget containing Zoom / Sélectionner / XLSX buttons."""
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 2, 0, 2)

        btn_zoom = QPushButton('🔍  Zoom')
        btn_zoom.setToolTip('Double-clic sur une ligne pour zoomer directement')
        btn_sel  = QPushButton('✓  Sélectionner dans QGIS')
        btn_csv  = QPushButton('💾  Exporter Excel')

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
        bc.clicked.connect(lambda: self._export_xlsx(self.tbl_chev, 'chevauchements'))
        btn_shp_chev = QPushButton('🗺  Exporter SHP')
        btn_shp_chev.setToolTip('Exporter les entités visibles en Shapefile')
        btn_shp_chev.clicked.connect(lambda: self._export_shp(self.tbl_chev, 'chevauchements'))
        ab.layout().addWidget(btn_shp_chev)
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
        bc.clicked.connect(lambda: self._export_xlsx(self.tbl_doub, 'doublons'))
        btn_shp_doub = QPushButton('🗺  Exporter SHP')
        btn_shp_doub.setToolTip('Exporter les entités visibles en Shapefile')
        btn_shp_doub.clicked.connect(lambda: self._export_shp(self.tbl_doub, 'doublons', include_col4_fid=True))
        ab.layout().addWidget(btn_shp_doub)
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
        bc.clicked.connect(lambda: self._export_xlsx(self.tbl_parc, 'parcours_longs'))
        btn_shp_parc = QPushButton('🗺  Exporter SHP')
        btn_shp_parc.setToolTip('Exporter les entités visibles en Shapefile')
        btn_shp_parc.clicked.connect(lambda: self._export_shp(self.tbl_parc, 'parcours_longs'))
        ab.layout().addWidget(btn_shp_parc)
        rv.addWidget(ab)

        h.addWidget(res, 1)
        return root

    # ─────────────────────────────────────────────────────────────────────────
    # TAB 4 – BAL éloignées infra
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

        self.le_flt_bal = QLineEdit()
        self.le_flt_bal.setPlaceholderText('(toute l\'infra)')
        self.le_flt_bal.setToolTip(
            'Filtre optionnel sur la couche Infra.\n'
            'Laisser vide pour trouver la vraie infra la plus proche\n'
            'quelle que soit sa nature (C0, E0, E7…).'
        )
        frm.addRow('Filtre Infra :', self.le_flt_bal)

        self.cb_zasro = QgsMapLayerComboBox()
        self.cb_zasro.setFilters(_F_POLY)
        self.cb_zasro.setAllowEmptyLayer(True)
        self.cb_zasro.setToolTip(
            'Couche polygonale za_sro (zones PM).\n'
            'Utilisée en repli quand le champ "sro" de la BAL est vide :\n'
            'on cherche le polygone PM contenant la BAL, puis l\'infra\n'
            'dont le champ "sro" correspond à ce polygone.'
        )
        frm.addRow('Périmètre PM (za_sro) :', self.cb_zasro)

        self.sp_rayon = QSpinBox()
        self.sp_rayon.setRange(0, 999999999)
        self.sp_rayon.setValue(500)
        self.sp_rayon.setSuffix(' m')
        self.sp_rayon.setToolTip(
            'Une BAL est considérée isolée si aucune autre BAL\n'
            'n\'est présente dans ce rayon.'
        )
        frm.addRow('Rayon isolation :', self.sp_rayon)

        vbox.addLayout(frm)

        # ── Rayon d'isolation activé / désactivé ─────────────────────────────
        self.chk_use_rayon = QCheckBox('Utiliser le rayon d\'isolation')
        self.chk_use_rayon.setChecked(True)
        self.chk_use_rayon.setToolTip(
            'Si coché, seules les BAL sans voisin dans le rayon sont analysées.\n'
            'Si décoché, toutes les BAL du périmètre PM sont analysées et la\n'
            'distance à l\'infra la plus proche est calculée pour chacune.'
        )
        self.chk_use_rayon.toggled.connect(self.sp_rayon.setEnabled)
        vbox.addWidget(self.chk_use_rayon)

        # ── Filtre distance infra ─────────────────────────────────────────────
        self.chk_flt_dist = QCheckBox('Filtrer par distance à l\'infra la plus proche')
        self.chk_flt_dist.setChecked(True)
        self.chk_flt_dist.setToolTip(
            'Si coché, seules les BAL dont la distance à l\'infra la plus\n'
            'proche est >= au seuil ci-dessous sont affichées.\n'
            'Les BAL sans infra trouvée dans leur PM sont toujours affichées.'
        )
        vbox.addWidget(self.chk_flt_dist)

        frm2 = QFormLayout()
        self.sp_dist_min = QSpinBox()
        self.sp_dist_min.setRange(0, 999999999)
        self.sp_dist_min.setValue(1000)
        self.sp_dist_min.setSuffix(' m')
        self.sp_dist_min.setToolTip(
            'Afficher uniquement les BAL dont la distance à l\'infra\n'
            'la plus proche dans le même PM est >= à cette valeur.'
        )
        frm2.addRow('Distance infra min. :', self.sp_dist_min)
        self.chk_flt_dist.toggled.connect(self.sp_dist_min.setEnabled)
        vbox.addLayout(frm2)

        info = QLabel(
            '<small><i>Pour chaque BAL analysée, l\'infra la plus proche est cherchée '
            'dans la même PM (champ sro). Si le sro est absent, repli sur le polygone '
            'za_sro. Le type réel (C0, E0, E7…) est affiché.<br>'
            'Les BAL sans infra dans leur PM sont toujours remontées.</i></small>'
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
        self.le_srch_bal.textChanged.connect(lambda _: self._filter_bal_table())
        sh.addWidget(self.le_srch_bal, 1)
        sh.addWidget(QLabel('Type infra :'))
        self.cmb_bal_type = QComboBox()
        self.cmb_bal_type.setMinimumWidth(90)
        self.cmb_bal_type.addItem('(tous)')
        self.cmb_bal_type.setToolTip(
            'Filtrer par type d\'infra (statut + mode_pose).\n'
            'Ex : C0 = aérien à créer, C1 = enterré à créer, E0 = aérien existant.'
        )
        self.cmb_bal_type.currentTextChanged.connect(lambda _: self._filter_bal_table())
        sh.addWidget(self.cmb_bal_type)
        self.lbl_cnt_bal = QLabel('—')
        sh.addWidget(self.lbl_cnt_bal)
        rv.addLayout(sh)

        self.tbl_bal = QTableWidget(0, 8)
        self.tbl_bal.setHorizontalHeaderLabels([
            'ID BAL', 'SRO BAL',
            'ID Infra proche', 'Dist. infra (m)', 'Long. infra (m)',
            'SRO infra', 'id_pa infra',
            'Type infra',
        ])
        self._style_table(self.tbl_bal)
        self.tbl_bal.doubleClicked.connect(
            lambda idx: self._zoom_row(self.tbl_bal, idx.row())
        )
        rv.addWidget(self.tbl_bal)

        ab, bz, bs, bc = self._action_bar(self.tbl_bal, 'bal')
        bz.clicked.connect(lambda: self._zoom_selected(self.tbl_bal))
        bs.clicked.connect(lambda: self._select_qgis(self.tbl_bal))
        bc.clicked.connect(lambda: self._export_xlsx(self.tbl_bal, 'bal_eloignees'))
        btn_shp_bal = QPushButton('🗺  Exporter SHP')
        btn_shp_bal.setToolTip('Exporter les entités visibles en Shapefile')
        btn_shp_bal.clicked.connect(lambda: self._export_shp(self.tbl_bal, 'bal_eloignees'))
        ab.layout().addWidget(btn_shp_bal)
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
        self._refresh_rapport_tab()

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
        self._refresh_rapport_tab()

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
        self._refresh_rapport_tab()

    # ─────────────────────────────────────────────────────────────────────────
    # ANALYSIS – Tab 4: BAL éloignées infra
    # ─────────────────────────────────────────────────────────────────────────
    def _run_bal(self):
        bal_lyr   = self.cb_bal.currentLayer()
        infra_lyr = self.cb_infra_bal.currentLayer()
        if not bal_lyr or not infra_lyr:
            QMessageBox.warning(self, 'Erreur', 'Sélectionnez les couches BAL et Infra.')
            return

        use_rayon   = self.chk_use_rayon.isChecked()
        use_flt_dist = self.chk_flt_dist.isChecked()
        radius      = float(self.sp_rayon.value())
        dist_min    = float(self.sp_dist_min.value()) if use_flt_dist else 0.0
        infra_flt   = self.le_flt_bal.text().strip()

        self.lbl_status.setText('Construction des index…')
        QApplication.processEvents()

        # BAL index
        bal_idx = QgsSpatialIndex()
        bal_fd  = {}
        for ft in bal_lyr.getFeatures():
            bal_idx.insertFeature(ft)
            bal_fd[ft.id()] = ft

        # Infra index — on charge TOUTES les natures pour trouver la vraie plus proche
        infra_idx     = QgsSpatialIndex()
        infra_fd      = {}
        infra_sro_map = {}   # fid → sro string
        req = QgsFeatureRequest()
        if infra_flt:
            req.setFilterExpression(infra_flt)
        infra_fnames_pre = infra_lyr.fields().names()
        for ft in infra_lyr.getFeatures(req):
            infra_idx.insertFeature(ft)
            infra_fd[ft.id()] = ft
            sro_v = ft['sro'] if 'sro' in infra_fnames_pre and ft['sro'] is not None else ''
            infra_sro_map[ft.id()] = str(sro_v).strip()

        infra_fnames = infra_lyr.fields().names()

        # za_sro index — repli quand le champ sro de la BAL est vide
        zasro_lyr = self.cb_zasro.currentLayer()
        zasro_idx     = None
        zasro_fd      = {}
        zasro_sro_map = {}   # fid → sro string
        if zasro_lyr:
            zasro_idx = QgsSpatialIndex()
            zasro_fn  = zasro_lyr.fields().names()
            for ft in zasro_lyr.getFeatures():
                zasro_idx.insertFeature(ft)
                zasro_fd[ft.id()] = ft
                sro_v = ft['sro'] if 'sro' in zasro_fn and ft['sro'] is not None else ''
                zasro_sro_map[ft.id()] = str(sro_v).strip()

        def _infra_safe(ft, field):
            return str(ft[field]) if field in infra_fnames and ft[field] is not None else ''

        def _infra_type(ft):
            """Retourne le type combiné ex. C0, C1, E0, E1..."""
            s = _infra_safe(ft, 'statut')
            m = _infra_safe(ft, 'mode_pose')
            return (s + m).strip() or '—'

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

        lbl_analyse = ('BAL isolées…' if use_rayon else 'BAL éloignées…')
        prog = QProgressDialog(
            lbl_analyse, 'Annuler', 0, len(all_bal), self)
        prog.setWindowTitle('Analyse en cours')
        prog.setMinimumDuration(0)
        prog.setWindowModality(Qt.WindowModality.WindowModal)

        results = []

        for i, bal_ft in enumerate(all_bal):
            prog.setValue(i)
            if prog.wasCanceled():
                break
            if i % 50 == 0:
                pct = 100 * i // len(all_bal) if all_bal else 0
                prog.setLabelText(f'{lbl_analyse} {i}/{len(all_bal)} ({pct} %)')
                QApplication.processEvents()

            bg = bal_ft.geometry()
            if not bg or bg.isEmpty():
                continue

            # ── Mode A : rayon d'isolation activé ────────────────────────────
            if use_rayon:
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

            # ── Déterminer la PM effective de cette BAL ──────────────────────
            # Étape 1 : champ sro de la BAL
            bal_sro = ''
            if 'sro' in _bal_fn and bal_ft['sro'] is not None:
                bal_sro = str(bal_ft['sro']).strip()

            # Étape 2 : repli polygone za_sro si sro vide ou absent
            if not bal_sro and zasro_idx is not None:
                pt_bbox = bg.boundingBox()
                for zasro_fid in zasro_idx.intersects(pt_bbox):
                    if zasro_fd[zasro_fid].geometry().contains(bg):
                        bal_sro = zasro_sro_map.get(zasro_fid, '')
                        break

            # ── Chercher l'infra la plus proche dans la même PM ──────────────
            search_radius = max(radius * 10, 5000.0) if use_rayon else 50000.0
            ibbox = bg.boundingBox()
            ibbox.grow(search_radius)
            candidates = infra_idx.intersects(ibbox)

            nearest_fid  = None
            nearest_dist = float('inf')
            for ifid in candidates:
                if bal_sro and infra_sro_map.get(ifid, '') != bal_sro:
                    continue
                d = bg.distance(infra_fd[ifid].geometry())
                if d < nearest_dist:
                    nearest_dist = d
                    nearest_fid  = ifid

            nf = infra_fd[nearest_fid] if nearest_fid is not None else None

            # ── Filtre distance infra ─────────────────────────────────────────
            # BAL sans infra dans leur PM : toujours remontées
            # BAL avec infra trouvée : filtrer si use_flt_dist
            if use_flt_dist and nf is not None and nearest_dist < dist_min:
                continue

            bal_gid = (str(bal_ft['gid'])
                       if 'gid' in _bal_fn and bal_ft['gid'] is not None
                       else str(bal_ft.id()))
            results.append(dict(
                bal_fid        = bal_ft.id(),
                bal_gid        = bal_gid,
                bal_layer_id   = bal_lyr.id(),
                bal_sro        = bal_sro,
                infra_fid      = nearest_fid if nf is not None else -1,
                dist_infra     = nearest_dist if nf is not None else float('inf'),
                infra_long     = _infra_long(nf) if nf is not None else 0.0,
                infra_nro      = _infra_safe(nf, 'nro')   if nf is not None else '',
                infra_sro      = _infra_safe(nf, 'sro')   if nf is not None else '',
                infra_idpa     = _infra_safe(nf, 'id_pa') if nf is not None else '',
                infra_type     = _infra_type(nf)          if nf is not None else '—',
                infra_layer_id = infra_lyr.id(),
            ))

        prog.setValue(len(all_bal))

        # Sort: farthest from infra first (most critical BAL)
        results.sort(key=lambda x: x['dist_infra'], reverse=True)

        self.tbl_bal.setSortingEnabled(False)
        self.tbl_bal.setRowCount(0)

        for r in results:
            row = self.tbl_bal.rowCount()
            self.tbl_bal.insertRow(row)

            dist_s = f"{r['dist_infra']:.1f}" if r['infra_fid'] >= 0 else 'N/A'
            cells = [
                _si(r['bal_gid']),
                _si(r['bal_sro']),
                _si(r['infra_fid'] if r['infra_fid'] >= 0 else 'N/A'),
                _ni(dist_s),
                _ni(f"{r['infra_long']:.1f}"),
                _si(r['infra_sro']),
                _si(r['infra_idpa']),
                _si(r['infra_type']),
            ]
            for col, item in enumerate(cells):
                self.tbl_bal.setItem(row, col, item)

            self.tbl_bal.item(row, 0).setData(Qt.ItemDataRole.UserRole + 1, r['bal_fid'])
            self.tbl_bal.item(row, 0).setData(Qt.ItemDataRole.UserRole + 2, r['bal_layer_id'])

        self.tbl_bal.setSortingEnabled(True)

        # Populate type filter combo with unique infra types found
        types = sorted({r['infra_type'] for r in results if r['infra_type'] not in ('', '—')})
        self.cmb_bal_type.blockSignals(True)
        self.cmb_bal_type.clear()
        self.cmb_bal_type.addItem('(tous)')
        for t in types:
            self.cmb_bal_type.addItem(t)
        self.cmb_bal_type.blockSignals(False)

        n = len(results)
        n_total = len(all_bal)

        if use_rayon:
            cnt_txt = f'{n} BAL éloignée(s) — rayon {int(radius)} m'
        else:
            cnt_txt = f'{n} BAL affichées sur {n_total} analysées'
        if use_flt_dist:
            cnt_txt += f' — dist. ≥ {int(dist_min)} m'
        else:
            cnt_txt += ' — sans filtre dist.'

        self.lbl_cnt_bal.setText(cnt_txt)
        self.lbl_status.setText(
            f'BAL éloignées : {n} affichées'
            + (f' — rayon {int(radius)} m' if use_rayon else f' sur {n_total} analysées')
            + (f', dist. ≥ {int(dist_min)} m' if use_flt_dist else '')
            + '.')
        self._refresh_rapport_tab()

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

    def _filter_bal_table(self):
        """Filter BAL table by text search AND infra type combo (col 7)."""
        text     = self.le_srch_bal.text().lower()
        sel_type = self.cmb_bal_type.currentText()
        use_type = sel_type not in ('', '(tous)')
        for row in range(self.tbl_bal.rowCount()):
            if text:
                text_ok = any(
                    text in (self.tbl_bal.item(row, c).text().lower()
                             if self.tbl_bal.item(row, c) else '')
                    for c in range(self.tbl_bal.columnCount())
                )
            else:
                text_ok = True
            if use_type:
                cell = self.tbl_bal.item(row, 7)   # Type infra column
                type_ok = sel_type == (cell.text() if cell else '')
            else:
                type_ok = True
            self.tbl_bal.setRowHidden(row, not (text_ok and type_ok))

    def _clear_selection(self):
        for lyr in QgsProject.instance().mapLayers().values():
            if hasattr(lyr, 'removeSelection'):
                lyr.removeSelection()

    def _export_xlsx(self, tbl, name):
        """Export the visible table rows to a real Excel file (.xlsx) as a table."""
        path, _ = QFileDialog.getSaveFileName(
            self, 'Exporter Excel', f'{name}.xlsx', 'Classeur Excel (*.xlsx)')
        if not path:
            return
        if not path.lower().endswith('.xlsx'):
            path += '.xlsx'

        headers = [
            (tbl.horizontalHeaderItem(c).text()
             if tbl.horizontalHeaderItem(c) else f'Col{c}')
            for c in range(tbl.columnCount())
        ]
        rows = []
        for row in range(tbl.rowCount()):
            if tbl.isRowHidden(row):
                continue
            rows.append([
                (tbl.item(row, c).text() if tbl.item(row, c) else '')
                for c in range(tbl.columnCount())
            ])

        try:
            self._write_xlsx(path, headers, rows, name)
            QMessageBox.information(
                self, 'Export',
                f'{len(rows)} ligne(s) exportée(s) :\n{path}')
        except Exception as e:
            QMessageBox.critical(self, 'Erreur export', str(e))

    # ── Écriture XLSX native (OOXML, sans dépendance externe) ────────────────
    @staticmethod
    def _xlsx_col_letter(n):
        """1 -> A, 27 -> AA."""
        s = ''
        while n > 0:
            n, r = divmod(n - 1, 26)
            s = chr(65 + r) + s
        return s

    @staticmethod
    def _xlsx_esc(t):
        return (t.replace('&', '&amp;').replace('<', '&lt;')
                 .replace('>', '&gt;').replace('"', '&quot;'))

    def _write_xlsx(self, path, headers, rows, name):
        """Write a minimal but valid .xlsx with the data formatted as an Excel Table."""
        import re
        import zipfile

        esc = self._xlsx_esc
        col_letter = self._xlsx_col_letter
        ncols = len(headers)
        nrows = len(rows)
        last_col = col_letter(ncols)
        # Le tableau couvre l'en-tête (ligne 1) + les données.
        last_row = nrows + 1
        ref = f'A1:{last_col}{last_row}'

        num_re = re.compile(r'^-?\d+(?:[.,]\d+)?$')

        def is_num(txt):
            return bool(txt) and bool(num_re.match(txt.strip()))

        # En-têtes uniques (contrainte d'un tableau Excel)
        uniq, seen = [], {}
        for hpos, htxt in enumerate(headers):
            h = htxt if htxt else f'Col{hpos + 1}'
            if h in seen:
                seen[h] += 1
                h = f'{h} ({seen[h]})'
            else:
                seen[h] = 1
            uniq.append(h)

        def cell_xml(ref_, txt):
            if is_num(txt):
                return f'<c r="{ref_}"><v>{txt.strip().replace(",", ".")}</v></c>'
            return (f'<c r="{ref_}" t="inlineStr">'
                    f'<is><t xml:space="preserve">{esc(txt)}</t></is></c>')

        # Lignes de la feuille
        sheet_rows = []
        header_cells = ''.join(
            cell_xml(f'{col_letter(c + 1)}1', uniq[c]) for c in range(ncols))
        sheet_rows.append(f'<row r="1">{header_cells}</row>')
        for ri, rdata in enumerate(rows, start=2):
            cells = ''.join(
                cell_xml(f'{col_letter(c + 1)}{ri}',
                         rdata[c] if c < len(rdata) else '')
                for c in range(ncols))
            sheet_rows.append(f'<row r="{ri}">{cells}</row>')
        sheet_data = ''.join(sheet_rows)

        # Colonnes du tableau
        table_cols = ''.join(
            f'<tableColumn id="{c + 1}" name="{esc(uniq[c])}"/>'
            for c in range(ncols))

        # Noms sûrs pour la feuille et le tableau
        sheet_name = re.sub(r'[\[\]:\*\?/\\]', '_', name)[:31] or 'Export'
        table_disp = re.sub(r'[^A-Za-z0-9_]', '_', name) or 'Export'
        if not re.match(r'^[A-Za-z_]', table_disp):
            table_disp = 'T_' + table_disp

        content_types = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
            '<Override PartName="/xl/tables/table1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.table+xml"/>'
            '</Types>'
        )
        rels = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            '</Relationships>'
        )
        workbook = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            f'<sheets><sheet name="{esc(sheet_name)}" sheetId="1" r:id="rId1"/></sheets>'
            '</workbook>'
        )
        wb_rels = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
            '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
            '</Relationships>'
        )
        styles = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts>'
            '<fills count="2"><fill><patternFill patternType="none"/></fill>'
            '<fill><patternFill patternType="gray125"/></fill></fills>'
            '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
            '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
            '<cellXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/></cellXfs>'
            '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
            '</styleSheet>'
        )
        worksheet = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            f'<dimension ref="{ref}"/>'
            f'<sheetData>{sheet_data}</sheetData>'
            '<tableParts count="1"><tablePart r:id="rId1"/></tableParts>'
            '</worksheet>'
        )
        sheet_rels = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/table" Target="../tables/table1.xml"/>'
            '</Relationships>'
        )
        table = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<table xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            f'id="1" name="{esc(table_disp)}" displayName="{esc(table_disp)}" '
            f'ref="{ref}" totalsRowShown="0">'
            f'<autoFilter ref="{ref}"/>'
            f'<tableColumns count="{ncols}">{table_cols}</tableColumns>'
            '<tableStyleInfo name="TableStyleMedium2" showFirstColumn="0" '
            'showLastColumn="0" showRowStripes="1" showColumnStripes="0"/>'
            '</table>'
        )

        with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as z:
            z.writestr('[Content_Types].xml', content_types)
            z.writestr('_rels/.rels', rels)
            z.writestr('xl/workbook.xml', workbook)
            z.writestr('xl/_rels/workbook.xml.rels', wb_rels)
            z.writestr('xl/styles.xml', styles)
            z.writestr('xl/worksheets/sheet1.xml', worksheet)
            z.writestr('xl/worksheets/_rels/sheet1.xml.rels', sheet_rels)
            z.writestr('xl/tables/table1.xml', table)

    def _export_shp(self, tbl, name, include_col4_fid=False):
        """Export visible table rows as Shapefile(s), one file per source layer."""
        layer_fids   = {}
        fid_display  = {}   # (layer_id, fid) -> ID affiché dans le tableau
        for row in range(tbl.rowCount()):
            if tbl.isRowHidden(row):
                continue
            item0 = tbl.item(row, 0)
            if not item0:
                continue
            fid      = item0.data(Qt.ItemDataRole.UserRole + 1)
            layer_id = item0.data(Qt.ItemDataRole.UserRole + 2)
            if fid is None or not layer_id:
                continue
            layer_fids.setdefault(layer_id, set()).add(fid)
            fid_display[(layer_id, fid)] = item0.text()
            if include_col4_fid:
                item4 = tbl.item(row, 4)
                if item4:
                    fid2 = item4.data(Qt.ItemDataRole.UserRole + 1)
                    if fid2 is not None:
                        layer_fids[layer_id].add(fid2)
                        fid_display[(layer_id, fid2)] = item4.text()

        if not layer_fids:
            QMessageBox.information(self, 'Export SHP', 'Aucune entité à exporter.')
            return

        path, _ = QFileDialog.getSaveFileName(
            self, 'Exporter SHP', f'{name}.shp', 'Shapefile (*.shp)')
        if not path:
            return
        if not path.lower().endswith('.shp'):
            path += '.shp'
        base = path[:-4]

        exported = []
        errors   = []
        skipped  = []   # (couche, [IDs affichés]) entités sans géométrie, ignorées par le SHP

        for layer_id, fids in layer_fids.items():
            lyr = QgsProject.instance().mapLayer(layer_id)
            if not lyr:
                errors.append(f'Couche introuvable : {layer_id}')
                continue

            out_path = path if len(layer_fids) == 1 else (
                f'{base}_{lyr.name().replace(" ", "_").replace("/", "_")}.shp'
            )

            # Détecter les entités sans géométrie valide : elles sont présentes
            # dans le CSV mais ignorées par le pilote Shapefile.
            no_geom = []
            req = QgsFeatureRequest().setFilterFids(list(fids))
            for ft in lyr.getFeatures(req):
                g = ft.geometry()
                if g is None or g.isEmpty() or g.isNull():
                    no_geom.append(fid_display.get((layer_id, ft.id()), str(ft.id())))
            if no_geom:
                skipped.append((lyr.name(), sorted(no_geom)))

            lyr.selectByIds(list(fids))
            try:
                opts = QgsVectorFileWriter.SaveVectorOptions()
                opts.driverName = 'ESRI Shapefile'
                opts.fileEncoding = 'UTF-8'
                opts.onlySelectedFeatures = True
                result = QgsVectorFileWriter.writeAsVectorFormatV3(
                    lyr, out_path, lyr.transformContext(), opts)
                err_code = result[0]
                err_msg  = result[1] if len(result) > 1 else ''
                try:
                    no_error = (err_code == QgsVectorFileWriter.WriterError.NoError)
                except AttributeError:
                    no_error = (err_code == 0)
                if no_error:
                    exported.append(out_path)
                else:
                    errors.append(f'{lyr.name()} : {err_msg}')
            except Exception as e:
                errors.append(f'{lyr.name()} : {e}')
            finally:
                lyr.removeSelection()

        if exported:
            msg = 'Fichier(s) exporté(s) :\n' + '\n'.join(exported)
            if errors:
                msg += '\n\nErreur(s) :\n' + '\n'.join(errors)
            if skipped:
                self._show_shp_skipped_dialog(msg, skipped)
            else:
                QMessageBox.information(self, 'Export SHP', msg)
        else:
            QMessageBox.critical(self, 'Export SHP', 'Erreur(s) :\n' + '\n'.join(errors))

    def _show_shp_skipped_dialog(self, summary, skipped):
        """Affiche le résultat de l'export + la liste copiable des IDs ignorés."""
        total = sum(len(ids) for _, ids in skipped)

        # Liste détaillée copiable (une section par couche)
        blocks = []
        flat_ids = []
        for nm, ids in skipped:
            flat_ids.extend(ids)
            blocks.append(f'# {nm} ({len(ids)} entité(s))\n' + '\n'.join(ids))
        detail_text = '\n\n'.join(blocks)

        dlg = QDialog(self)
        dlg.setWindowTitle('Export SHP – entités ignorées')
        dlg.resize(440, 420)
        v = QVBoxLayout(dlg)

        v.addWidget(QLabel(summary))

        warn = QLabel(
            f'⚠ <b>{total} entité(s) ignorée(s)</b> : géométrie nulle ou invalide, '
            f'donc absente(s) du Shapefile mais présente(s) dans le CSV.<br>'
            f'IDs ci-dessous (sélectionnables / copiables) :'
        )
        warn.setWordWrap(True)
        v.addWidget(warn)

        txt = QPlainTextEdit()
        txt.setReadOnly(True)
        txt.setPlainText(detail_text)
        txt.setStyleSheet('font-family: monospace;')
        v.addWidget(txt, 1)

        bar = QHBoxLayout()
        btn_copy = QPushButton('📋  Copier les IDs')

        def _copy():
            QApplication.clipboard().setText('\n'.join(flat_ids))
            btn_copy.setText('✓  Copié')
        btn_copy.clicked.connect(_copy)
        bar.addWidget(btn_copy)
        bar.addStretch()
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok)
        bb.accepted.connect(dlg.accept)
        bar.addWidget(bb)
        v.addLayout(bar)

        dlg.exec()

    # ─────────────────────────────────────────────────────────────────────────
    # TAB 5 – PA sans infra
    # ─────────────────────────────────────────────────────────────────────────
    def _tab_pa_sans_infra(self):
        root = QWidget()
        h = QHBoxLayout(root)

        cfg = QGroupBox('Configuration')
        cfg.setFixedWidth(290)
        vbox = QVBoxLayout(cfg)

        frm = QFormLayout()

        self.cb_zapa = QgsMapLayerComboBox()
        self.cb_zapa.setFilters(_F_POLY)
        self.cb_zapa.setToolTip(
            'Couche polygonale zapa (champs requis : id_metier, sro).\n'
            'Détection auto : source PostGIS table="rad_aw_2026"."zapa",\n'
            'puis nom exact "zapa", puis nom contenant "livrable_zapa".'
        )
        frm.addRow('Couche zapa :', self.cb_zapa)

        self.cb_infra_pa = QgsMapLayerComboBox()
        self.cb_infra_pa.setFilters(_F_LINE)
        self.cb_infra_pa.setToolTip(
            'Couche linéaire infra (champ requis : id_pa).\n'
            'Détection auto : source PostGIS table="rad_aw_2026"."infra",\n'
            'puis nom exact "infra", puis nom contenant "livrable_infra".'
        )
        frm.addRow('Couche infra :', self.cb_infra_pa)

        self.cb_bal_pa = QgsMapLayerComboBox()
        self.cb_bal_pa.setFilters(_F_POINT)
        self.cb_bal_pa.setToolTip(
            'Couche serveur rad_aw_2026.bal (nom QGIS habituel : bal).\n'
            'Sert à vérifier qu\'une ZAPA contient au moins une BAL avant\n'
            'de la considérer comme anomalie sans infra.\n'
            'Champs requis : zapa, sro.\n'
            'Détection auto : source PostGIS table="rad_aw_2026"."bal",\n'
            'puis nom exact "bal" avec champs zapa+sro.'
        )
        frm.addRow('Couche BAL :', self.cb_bal_pa)

        self.sp_tol_pa = QSpinBox()
        self.sp_tol_pa.setRange(0, 500)
        self.sp_tol_pa.setValue(1)
        self.sp_tol_pa.setSuffix(' m')
        self.sp_tol_pa.setToolTip(
            'Buffer appliqué à la ZAPA pour le contrôle spatial.\n'
            '0 = intersection stricte, 1 m = léger buffer contre les micro-décalages.'
        )
        frm.addRow('Tolérance spatiale :', self.sp_tol_pa)

        vbox.addLayout(frm)

        self.chk_discord = QCheckBox('Inclure les discordances')
        self.chk_discord.setToolTip(
            'Affiche aussi les ZAPA où le contrôle attributaire (id_pa)\n'
            'et le contrôle spatial (intersection) se contredisent :\n'
            '• Discordance attributaire : pas d\'id_pa mais infra spatiale trouvée\n'
            '• Discordance spatiale : id_pa trouvé mais aucune infra intersectante'
        )
        vbox.addWidget(self.chk_discord)

        info = QLabel(
            '<small><i>Périmètre : liste PM actuelle du plugin.<br>'
            'ZAPA : <b>id_metier</b>, <b>sro</b>.<br>'
            'Infra : <b>id_pa</b>.<br>'
            'BAL : <b>zapa</b>, <b>sro</b>.<br>'
            'Les ZAPA sans BAL sont ignorées.</i></small>'
        )
        info.setWordWrap(True)
        vbox.addWidget(info)
        vbox.addStretch()

        btn_run = QPushButton('▶  Lancer l\'analyse')
        btn_run.setStyleSheet('font-weight:bold; padding:6px;')
        btn_run.clicked.connect(self._run_pa_sans_infra)
        vbox.addWidget(btn_run)
        h.addWidget(cfg)

        res = QWidget()
        rv = QVBoxLayout(res)
        rv.setContentsMargins(0, 0, 0, 0)

        sh = QHBoxLayout()
        sh.addWidget(QLabel('Recherche :'))
        self.le_srch_pa = QLineEdit()
        self.le_srch_pa.setPlaceholderText('Filtrer…')
        self.le_srch_pa.textChanged.connect(
            lambda txt: self._filter_table(self.tbl_pa, txt))
        sh.addWidget(self.le_srch_pa, 1)
        self.lbl_cnt_pa = QLabel('—')
        sh.addWidget(self.lbl_cnt_pa)
        rv.addLayout(sh)

        self.tbl_pa = QTableWidget(0, 9)
        self.tbl_pa.setHorizontalHeaderLabels([
            'SRO / PM', 'ID ZAPA / PA',
            'Nb BAL', 'Source BAL',
            'Nb infra attr.', 'Long. attr. (m)',
            'Nb infra spatiale', 'Long. spatiale (m)',
            'Diagnostic',
        ])
        self._style_table(self.tbl_pa)
        self.tbl_pa.doubleClicked.connect(
            lambda idx: self._zoom_row(self.tbl_pa, idx.row())
        )
        rv.addWidget(self.tbl_pa)

        ab, bz, bs, bc = self._action_bar(self.tbl_pa, 'pa')
        bz.clicked.connect(lambda: self._zoom_selected(self.tbl_pa))
        bs.clicked.connect(lambda: self._select_qgis(self.tbl_pa))
        bc.clicked.connect(lambda: self._export_xlsx(self.tbl_pa, 'pa_sans_infra'))
        btn_shp_pa = QPushButton('🗺  Exporter SHP')
        btn_shp_pa.setToolTip('Exporter les ZAPA visibles en Shapefile')
        btn_shp_pa.clicked.connect(
            lambda: self._export_shp(self.tbl_pa, 'zapa_sans_infra'))
        ab.layout().addWidget(btn_shp_pa)
        rv.addWidget(ab)

        h.addWidget(res, 1)
        return root

    # ─────────────────────────────────────────────────────────────────────────
    # ANALYSIS – Tab 5: PA sans infra
    # ─────────────────────────────────────────────────────────────────────────
    def _run_pa_sans_infra(self):
        zapa_lyr  = self.cb_zapa.currentLayer()
        infra_lyr = self.cb_infra_pa.currentLayer()
        bal_lyr   = self.cb_bal_pa.currentLayer()

        if not zapa_lyr:
            QMessageBox.warning(self, 'Erreur',
                                'Sélectionnez la couche zapa\n'
                                '(polygone avec champs id_metier, sro).')
            return
        if not infra_lyr:
            QMessageBox.warning(self, 'Erreur',
                                'Sélectionnez la couche infra\n'
                                '(linéaire avec champ id_pa).')
            return
        if not bal_lyr:
            QMessageBox.warning(self, 'Erreur',
                                'Sélectionnez la couche BAL\n'
                                '(point avec champs zapa, sro).')
            return

        zapa_fnames  = zapa_lyr.fields().names()
        infra_fnames = infra_lyr.fields().names()
        bal_fnames   = bal_lyr.fields().names()

        missing = []
        if 'id_metier' not in zapa_fnames:
            missing.append(f'Couche zapa "{zapa_lyr.name()}" : champ "id_metier" absent')
        if 'sro' not in zapa_fnames:
            missing.append(f'Couche zapa "{zapa_lyr.name()}" : champ "sro" absent')
        if 'id_pa' not in infra_fnames:
            missing.append(f'Couche infra "{infra_lyr.name()}" : champ "id_pa" absent')
        if 'zapa' not in bal_fnames:
            missing.append(f'Couche BAL "{bal_lyr.name()}" : champ "zapa" absent')
        if 'sro' not in bal_fnames:
            missing.append(f'Couche BAL "{bal_lyr.name()}" : champ "sro" absent')
        if missing:
            QMessageBox.warning(self, 'Champs requis manquants',
                                '\n'.join(missing))
            return

        # Cet onglet filtre TOUJOURS sur la liste PM, indépendamment de chk_pm.
        # Si la liste est vide, l'analyse est bloquée : sans périmètre les
        # résultats seraient hors contexte.
        if not self._pm_set:
            QMessageBox.warning(
                self, 'Périmètre PM vide',
                'La liste PM du plugin est vide.\n'
                'Cet onglet analyse uniquement les ZAPA du périmètre PM courant.\n'
                'Ajoutez des PM via "Modifier la liste…" avant de lancer l\'analyse.')
            return

        tol          = self.sp_tol_pa.value()
        incl_discord = self.chk_discord.isChecked()
        has_infra_sro = 'sro' in infra_fnames

        # CRS : transformer les géométries infra vers le CRS des ZAPA si besoin
        need_transform = (zapa_lyr.crs().authid() != infra_lyr.crs().authid())
        xform = None
        if need_transform:
            xform = QgsCoordinateTransform(
                infra_lyr.crs(), zapa_lyr.crs(),
                QgsProject.instance().transformContext())

        self.lbl_status.setText('Chargement des infras livrables…')
        QApplication.processEvents()

        # ── Longueur d'une entité infra ───────────────────────────────────────
        def _infra_len(ft, geom):
            for fld in ('long', 'longueur'):
                if fld in infra_fnames and ft[fld] is not None:
                    try:
                        return float(ft[fld])
                    except (TypeError, ValueError):
                        pass
            return geom.length()

        # ── Charger toutes les infras du périmètre ────────────────────────────
        infra_idx       = QgsSpatialIndex()
        infra_geoms     = {}   # fid → QgsGeometry (potentiellement transformée)
        infra_feats     = {}   # fid → QgsFeature
        infra_by_idpa   = {}   # str(id_pa) → [(fid, longueur)]
        infra_sro_by_fid = {}  # fid → str(sro)

        for ft in infra_lyr.getFeatures():
            g = ft.geometry()
            if g is None or g.isEmpty():
                continue
            if need_transform:
                from qgis.core import QgsGeometry as _QgsGeometry
                g = _QgsGeometry(g)
                g.transform(xform)

            # Filtre PM sur l'infra si le champ sro est présent (toujours actif)
            if has_infra_sro:
                sro_v = ft['sro']
                if sro_v is None or str(sro_v).strip() not in self._pm_set:
                    continue

            fid = ft.id()
            infra_geoms[fid] = g
            infra_feats[fid] = ft

            if has_infra_sro and ft['sro'] is not None:
                infra_sro_by_fid[fid] = str(ft['sro']).strip()

            id_pa_v = ft['id_pa']
            if id_pa_v is not None:
                key = str(id_pa_v).strip()
                if key:
                    infra_by_idpa.setdefault(key, []).append(
                        (fid, _infra_len(ft, g)))

            tmp = QgsFeature()
            tmp.setId(fid)
            tmp.setGeometry(g)
            infra_idx.insertFeature(tmp)

        # ── Charger les BAL du périmètre PM (comptage attributaire + index spatial) ─
        self.lbl_status.setText('Chargement des BAL…')
        QApplication.processEvents()

        # CRS BAL → CRS ZAPA si nécessaire pour le fallback spatial
        need_bal_transform = (zapa_lyr.crs().authid() != bal_lyr.crs().authid())
        xform_bal = None
        if need_bal_transform:
            xform_bal = QgsCoordinateTransform(
                bal_lyr.crs(), zapa_lyr.crs(),
                QgsProject.instance().transformContext())

        # bal_count_by_zapa : id_metier (str) → nb BAL rattachées attributairement
        bal_count_by_zapa = {}
        bal_idx   = QgsSpatialIndex()
        bal_geoms = {}   # fid → QgsGeometry (dans le CRS ZAPA)

        for ft in bal_lyr.getFeatures():
            g = ft.geometry()
            if g is None or g.isEmpty():
                continue
            # Filtre PM sur le SRO de la BAL
            sro_v = ft['sro']
            if sro_v is None or str(sro_v).strip() not in self._pm_set:
                continue
            # Comptage attributaire : bal.zapa → zapa.id_metier
            zapa_ref = ft['zapa']
            if zapa_ref is not None:
                key = str(zapa_ref).strip()
                if key:
                    bal_count_by_zapa[key] = bal_count_by_zapa.get(key, 0) + 1
            # Index spatial pour fallback
            if need_bal_transform:
                from qgis.core import QgsGeometry as _QgsGeometry
                g = _QgsGeometry(g)
                g.transform(xform_bal)
            fid = ft.id()
            bal_geoms[fid] = g
            tmp = QgsFeature()
            tmp.setId(fid)
            tmp.setGeometry(g)
            bal_idx.insertFeature(tmp)

        # ── Charger les ZAPA du périmètre PM (filtre toujours actif) ─────────
        # On filtre directement sur _pm_set sans passer par _in_pm/_chk_pm.
        def _zapa_in_pm(ft):
            val = ft['sro'] if 'sro' in zapa_fnames else None
            return val is not None and str(val).strip() in self._pm_set

        zapa_feats = [
            ft for ft in zapa_lyr.getFeatures()
            if _zapa_in_pm(ft)
            and ft.geometry() is not None
            and not ft.geometry().isEmpty()
        ]

        if not zapa_feats:
            QMessageBox.information(
                self, 'Info',
                'Aucune ZAPA trouvée pour les PM de la liste courante.\n'
                'Vérifiez que le champ "sro" de la couche ZAPA contient bien '
                'des codes présents dans la liste PM du plugin.')
            return

        prog = QProgressDialog(
            'Analyse PA sans infra…', 'Annuler', 0, len(zapa_feats), self)
        prog.setWindowTitle('Analyse en cours')
        prog.setMinimumDuration(0)
        prog.setWindowModality(Qt.WindowModality.WindowModal)

        results = []
        ignored_empty_zapa = 0

        for i, zapa_ft in enumerate(zapa_feats):
            prog.setValue(i)
            if prog.wasCanceled():
                break
            if i % 50 == 0:
                prog.setLabelText(
                    f'PA sans infra… {i}/{len(zapa_feats)} '
                    f'({100 * i // len(zapa_feats)} %)')
                QApplication.processEvents()

            zapa_g    = zapa_ft.geometry()
            id_metier = (str(zapa_ft['id_metier']).strip()
                         if zapa_ft['id_metier'] is not None else '')
            zapa_sro  = (str(zapa_ft['sro']).strip()
                         if zapa_ft['sro'] is not None else '')

            # ── Garde-fou BAL : ignorer les ZAPA sans BAL ────────────────────
            # Priorité 1 — attributaire : bal.zapa == zapa.id_metier
            nb_bal_attr = bal_count_by_zapa.get(id_metier, 0) if id_metier else 0

            # Priorité 2 — fallback spatial si 0 BAL attributaire
            if nb_bal_attr == 0:
                bbox_zapa = zapa_g.boundingBox()
                nb_bal_spatial = 0
                for bfid in bal_idx.intersects(bbox_zapa):
                    if zapa_g.contains(bal_geoms[bfid]):
                        nb_bal_spatial += 1
                nb_bal = nb_bal_spatial
                bal_source = 'Spatial' if nb_bal > 0 else '—'
            else:
                nb_bal = nb_bal_attr
                bal_source = 'Attributaire'

            if nb_bal == 0:
                # Pas de BAL dans cette ZAPA : absence d'infra est normale, ignorer
                ignored_empty_zapa += 1
                continue

            # ── Contrôle attributaire infra ───────────────────────────────────
            attr_count = 0
            attr_long  = 0.0
            if id_metier:
                for (fid, l) in infra_by_idpa.get(id_metier, []):
                    if has_infra_sro and zapa_sro:
                        isro = infra_sro_by_fid.get(fid, '')
                        if isro and isro != zapa_sro:
                            continue
                    attr_count += 1
                    attr_long  += l

            # ── Contrôle spatial infra ────────────────────────────────────────
            spatial_count = 0
            spatial_long  = 0.0
            search_g = zapa_g.buffer(tol, 5) if tol > 0 else zapa_g
            bbox = search_g.boundingBox()
            for ifid in infra_idx.intersects(bbox):
                if search_g.intersects(infra_geoms[ifid]):
                    spatial_count += 1
                    spatial_long  += _infra_len(infra_feats[ifid],
                                                infra_geoms[ifid])

            # ── Diagnostic ───────────────────────────────────────────────────
            if attr_count == 0 and spatial_count == 0:
                diagnostic = 'SANS INFRA'
            elif attr_count == 0 and spatial_count > 0:
                diagnostic = 'Discordance attributaire'
            elif attr_count > 0 and spatial_count == 0:
                diagnostic = 'Discordance spatiale'
            else:
                continue  # ZAPA correctement rattachée des deux côtés

            if diagnostic != 'SANS INFRA' and not incl_discord:
                continue

            results.append(dict(
                sro           = zapa_sro,
                id_zapa       = id_metier,
                nb_bal        = nb_bal,
                bal_source    = bal_source,
                attr_count    = attr_count,
                attr_long     = attr_long,
                spatial_count = spatial_count,
                spatial_long  = spatial_long,
                diagnostic    = diagnostic,
                fid           = zapa_ft.id(),
                layer_id      = zapa_lyr.id(),
            ))

        prog.setValue(len(zapa_feats))
        self._pa_ignored_empty_zapa = ignored_empty_zapa

        # ── Remplir le tableau ────────────────────────────────────────────────
        self.tbl_pa.setSortingEnabled(False)
        self.tbl_pa.setRowCount(0)

        for r in results:
            row = self.tbl_pa.rowCount()
            self.tbl_pa.insertRow(row)
            cells = [
                _si(r['sro']),
                _si(r['id_zapa']),
                _ni(r['nb_bal']),
                _si(r['bal_source']),
                _ni(r['attr_count']),
                _ni(f"{r['attr_long']:.1f}"),
                _ni(r['spatial_count']),
                _ni(f"{r['spatial_long']:.1f}"),
                _si(r['diagnostic']),
            ]
            for col, item in enumerate(cells):
                self.tbl_pa.setItem(row, col, item)
            self.tbl_pa.item(row, 0).setData(
                Qt.ItemDataRole.UserRole + 1, r['fid'])
            self.tbl_pa.item(row, 0).setData(
                Qt.ItemDataRole.UserRole + 2, r['layer_id'])

        self.tbl_pa.setSortingEnabled(True)

        n_sans  = sum(1 for r in results if r['diagnostic'] == 'SANS INFRA')
        n_disc  = len(results) - n_sans
        n_total = len(zapa_feats) - ignored_empty_zapa
        cnt_txt = f'{n_sans} ZAPA sans infra / {n_total} ZAPA avec BAL analysées'
        if incl_discord and n_disc:
            cnt_txt += f' + {n_disc} discordance(s)'
        if ignored_empty_zapa:
            cnt_txt += f' — {ignored_empty_zapa} ZAPA sans BAL ignorées'
        self.lbl_cnt_pa.setText(cnt_txt)
        self.lbl_status.setText(
            f'PA sans infra : {n_sans} ZAPA sans infra sur {n_total} ZAPA avec BAL analysées.'
            + (f' {ignored_empty_zapa} ZAPA sans BAL ignorées.' if ignored_empty_zapa else ''))
        self._refresh_rapport_tab()

    # ─────────────────────────────────────────────────────────────────────────
    # TAB 6 – Extractions
    # ─────────────────────────────────────────────────────────────────────────
    def _tab_extractions(self):
        root = QWidget()
        main_v = QVBoxLayout(root)
        main_v.setContentsMargins(6, 6, 6, 6)

        # ── Type selector ──────────────────────────────────────────────────
        type_bar = QHBoxLayout()
        type_bar.addWidget(QLabel('<b>Type d\'extraction :</b>'))
        self.cmb_extract_type = QComboBox()
        self.cmb_extract_type.addItem('EPA / PA du périmètre PM')
        self.cmb_extract_type.addItem('BAL du périmètre PM')
        self.cmb_extract_type.setMinimumWidth(240)
        self.cmb_extract_type.setToolTip(
            'Choisir le type de données à extraire pour le périmètre PM courant.')
        type_bar.addWidget(self.cmb_extract_type)
        type_bar.addStretch()
        main_v.addLayout(type_bar)

        # ── Stacked pages ──────────────────────────────────────────────────
        self.stk_extract = QStackedWidget()
        self.cmb_extract_type.currentIndexChanged.connect(
            self.stk_extract.setCurrentIndex)

        self.stk_extract.addWidget(self._extract_panel_epa())   # index 0
        self.stk_extract.addWidget(self._extract_panel_bal())   # index 1

        main_v.addWidget(self.stk_extract, 1)
        return root

    # ── Extraction panel helpers ───────────────────────────────────────────

    @staticmethod
    def _extract_action_bar(tbl, zoom_slot, sel_slot, csv_slot, xlsx_slot, shp_slot,
                            csv_tooltip=''):
        """Build a standard Zoom/Sélectionner/CSV/Excel/SHP action bar."""
        ab_w = QWidget()
        ab_h = QHBoxLayout(ab_w)
        ab_h.setContentsMargins(0, 2, 0, 2)
        btn_zoom = QPushButton('🔍  Zoom')
        btn_zoom.setToolTip('Double-clic sur une ligne pour zoomer directement')
        btn_zoom.clicked.connect(zoom_slot)
        ab_h.addWidget(btn_zoom)
        btn_sel = QPushButton('✓  Sélectionner dans QGIS')
        btn_sel.clicked.connect(sel_slot)
        ab_h.addWidget(btn_sel)
        ab_h.addStretch()
        btn_csv = QPushButton('💾  Exporter CSV')
        if csv_tooltip:
            btn_csv.setToolTip(csv_tooltip)
        btn_csv.clicked.connect(csv_slot)
        ab_h.addWidget(btn_csv)
        btn_xlsx = QPushButton('📊  Exporter Excel')
        btn_xlsx.clicked.connect(xlsx_slot)
        ab_h.addWidget(btn_xlsx)
        btn_shp = QPushButton('🗺  Exporter SHP')
        btn_shp.clicked.connect(shp_slot)
        ab_h.addWidget(btn_shp)
        return ab_w

    def _extract_panel_epa(self):
        """Build the EPA / PA extraction panel (page 0 of stk_extract)."""
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 4, 0, 0)

        cfg = QGroupBox('Configuration')
        cfg.setFixedWidth(290)
        vbox = QVBoxLayout(cfg)
        frm = QFormLayout()

        self.cb_epa = QgsMapLayerComboBox()
        self.cb_epa.setFilters(_F_POINT)
        self.cb_epa.setToolTip(
            'Couche EPA / PA (point).\n'
            'Détection auto : source PostGIS table="rad_aw_2026"."pa",\n'
            'puis nom exact "pa", "georeso_pa", "livrable_pa".\n'
            'Ne sélectionne jamais une couche ZAPA.'
        )
        frm.addRow('Couche EPA / PA :', self.cb_epa)
        vbox.addLayout(frm)

        info = QLabel(
            '<small><i>Extrait les EPA/PA rattachés aux PM du périmètre courant.<br>'
            'Colonnes exportées : <b>id_epa</b>, <b>pmz</b>.<br>'
            'Champ id : id_metier &gt; id_ftth &gt; gid &gt; fid.<br>'
            'Champ PMZ : sro &gt; id_ftth_pf &gt; pmz &gt; pm &gt; nom_pm.</i></small>'
        )
        info.setWordWrap(True)
        vbox.addWidget(info)
        vbox.addStretch()

        btn_run = QPushButton('🔎  Prévisualiser EPA')
        btn_run.setStyleSheet('font-weight:bold; padding:6px;')
        btn_run.clicked.connect(self._run_extract_epa)
        vbox.addWidget(btn_run)
        h.addWidget(cfg)

        res = QWidget()
        rv = QVBoxLayout(res)
        rv.setContentsMargins(0, 0, 0, 0)

        sh = QHBoxLayout()
        sh.addWidget(QLabel('Recherche :'))
        self.le_srch_epa = QLineEdit()
        self.le_srch_epa.setPlaceholderText('Filtrer…')
        self.le_srch_epa.textChanged.connect(
            lambda txt: self._filter_table(self.tbl_epa, txt))
        sh.addWidget(self.le_srch_epa, 1)
        self.lbl_cnt_epa = QLabel('—')
        sh.addWidget(self.lbl_cnt_epa)
        rv.addLayout(sh)

        self.tbl_epa = QTableWidget(0, 4)
        self.tbl_epa.setHorizontalHeaderLabels([
            'id_epa', 'pmz', 'Champ ID', 'Champ PMZ',
        ])
        self._style_table(self.tbl_epa)
        self.tbl_epa.doubleClicked.connect(
            lambda idx: self._zoom_row(self.tbl_epa, idx.row()))
        rv.addWidget(self.tbl_epa)

        rv.addWidget(self._extract_action_bar(
            self.tbl_epa,
            zoom_slot  = lambda: self._zoom_selected(self.tbl_epa),
            sel_slot   = lambda: self._select_qgis(self.tbl_epa),
            csv_slot   = self._export_csv_epa,
            xlsx_slot  = lambda: self._export_xlsx(self.tbl_epa, 'epa_perimetre_pm'),
            shp_slot   = lambda: self._export_shp(self.tbl_epa, 'epa_perimetre_pm'),
            csv_tooltip = 'Exporter id_epa;pmz en CSV (UTF-8, séparateur ;)',
        ))
        h.addWidget(res, 1)
        return w

    def _extract_panel_bal(self):
        """Build the BAL extraction panel (page 1 of stk_extract)."""
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 4, 0, 0)

        cfg = QGroupBox('Configuration')
        cfg.setFixedWidth(290)
        vbox = QVBoxLayout(cfg)
        frm = QFormLayout()

        self.cb_bal_extract = QgsMapLayerComboBox()
        self.cb_bal_extract.setFilters(_F_POINT)
        self.cb_bal_extract.setToolTip(
            'Couche BAL (point).\n'
            'Détection auto : source PostGIS table="rad_aw_2026"."bal",\n'
            'puis nom exact "bal", "georeso_bal", "livrable_bal".'
        )
        frm.addRow('Couche BAL :', self.cb_bal_extract)
        vbox.addLayout(frm)

        info = QLabel(
            '<small><i>Extrait toutes les BAL des PM du périmètre courant.<br>'
            'Colonnes exportées : <b>id_bal</b>, <b>nb_prises</b>, <b>pa</b>, <b>pmz</b>.<br>'
            'Champ id : id_metier &gt; gid &gt; fid.<br>'
            'Champ PMZ : sro &gt; id_ftth_pf &gt; pmz &gt; pm &gt; nom_pm.</i></small>'
        )
        info.setWordWrap(True)
        vbox.addWidget(info)
        vbox.addStretch()

        btn_run = QPushButton('🔎  Prévisualiser BAL')
        btn_run.setStyleSheet('font-weight:bold; padding:6px;')
        btn_run.clicked.connect(self._run_extract_bal)
        vbox.addWidget(btn_run)
        h.addWidget(cfg)

        res = QWidget()
        rv = QVBoxLayout(res)
        rv.setContentsMargins(0, 0, 0, 0)

        sh = QHBoxLayout()
        sh.addWidget(QLabel('Recherche :'))
        self.le_srch_bal_ext = QLineEdit()
        self.le_srch_bal_ext.setPlaceholderText('Filtrer…')
        self.le_srch_bal_ext.textChanged.connect(
            lambda txt: self._filter_table(self.tbl_bal_ext, txt))
        sh.addWidget(self.le_srch_bal_ext, 1)
        self.lbl_cnt_bal_ext = QLabel('—')
        sh.addWidget(self.lbl_cnt_bal_ext)
        rv.addLayout(sh)

        self.tbl_bal_ext = QTableWidget(0, 6)
        self.tbl_bal_ext.setHorizontalHeaderLabels([
            'id_bal', 'nb_prises', 'pa', 'pmz', 'Champ ID', 'Champ PMZ',
        ])
        self._style_table(self.tbl_bal_ext)
        self.tbl_bal_ext.doubleClicked.connect(
            lambda idx: self._zoom_row(self.tbl_bal_ext, idx.row()))
        rv.addWidget(self.tbl_bal_ext)

        rv.addWidget(self._extract_action_bar(
            self.tbl_bal_ext,
            zoom_slot  = lambda: self._zoom_selected(self.tbl_bal_ext),
            sel_slot   = lambda: self._select_qgis(self.tbl_bal_ext),
            csv_slot   = self._export_csv_bal,
            xlsx_slot  = lambda: self._export_xlsx(self.tbl_bal_ext, 'bal_perimetre_pm'),
            shp_slot   = lambda: self._export_shp(self.tbl_bal_ext, 'bal_perimetre_pm'),
            csv_tooltip = 'Exporter id_bal;nb_prises;pa;pmz en CSV (UTF-8, séparateur ;)',
        ))
        h.addWidget(res, 1)
        return w

    # ─────────────────────────────────────────────────────────────────────────
    # ANALYSIS – Tab 6: Extractions EPA
    # ─────────────────────────────────────────────────────────────────────────
    def _run_extract_epa(self):
        pa_lyr = self.cb_epa.currentLayer()
        if not pa_lyr:
            QMessageBox.warning(self, 'Erreur',
                                'Sélectionnez la couche EPA / PA (point).')
            return

        if not self._pm_set:
            QMessageBox.warning(self, 'Périmètre PM vide',
                'Aucun PM dans le périmètre courant.\n'
                'Ajoutez des PM via le bouton "Modifier la liste…".')
            return

        fnames = pa_lyr.fields().names()

        # Detect best id_epa field
        id_field = None
        for f in ('id_metier', 'id_ftth', 'gid', 'fid'):
            if f in fnames:
                id_field = f
                break

        # Detect best pmz field
        pm_field = None
        for f in ('sro', 'id_ftth_pf', 'pmz', 'pm', 'nom_pm'):
            if f in fnames:
                pm_field = f
                break

        if pm_field is None:
            preview = ', '.join(fnames[:10]) + ('…' if len(fnames) > 10 else '')
            QMessageBox.warning(self, 'Champs PMZ manquants',
                f'Impossible de filtrer la couche EPA/PA : aucun champ PMZ/SRO trouvé.\n'
                f'Champs cherchés : sro, id_ftth_pf, pmz, pm, nom_pm.\n'
                f'Champs disponibles : {preview}')
            return

        self.lbl_status.setText('Extraction EPA en cours…')
        QApplication.processEvents()

        results = []
        for ft in pa_lyr.getFeatures():
            pmz_val = ft[pm_field]
            if pmz_val is None:
                continue
            pmz_str = str(pmz_val).strip()
            if pmz_str not in self._pm_set:
                continue
            id_str = (str(ft[id_field]) if id_field and ft[id_field] is not None
                      else str(ft.id()))
            results.append(dict(
                fid      = ft.id(),
                layer_id = pa_lyr.id(),
                id_epa   = id_str,
                pmz      = pmz_str,
                id_field = id_field or '(QGIS id)',
                pm_field = pm_field,
            ))

        self.tbl_epa.setSortingEnabled(False)
        self.tbl_epa.setRowCount(0)
        for r in results:
            row = self.tbl_epa.rowCount()
            self.tbl_epa.insertRow(row)
            cells = [
                _si(r['id_epa']),
                _si(r['pmz']),
                _si(r['id_field']),
                _si(r['pm_field']),
            ]
            for col, item in enumerate(cells):
                self.tbl_epa.setItem(row, col, item)
            self.tbl_epa.item(row, 0).setData(Qt.ItemDataRole.UserRole + 1, r['fid'])
            self.tbl_epa.item(row, 0).setData(Qt.ItemDataRole.UserRole + 2, r['layer_id'])
        self.tbl_epa.setSortingEnabled(True)

        n     = len(results)
        n_pm  = len(self._pm_set)
        self.lbl_cnt_epa.setText(f'{n} EPA / {n_pm} PM')
        self.lbl_status.setText(
            f'EPA périmètre PM : {n} EPA exportables sur {n_pm} PM.')

    def _export_csv_epa(self):
        """Export id_epa;pmz as UTF-8-sig CSV (visible rows only)."""
        if self.tbl_epa.rowCount() == 0:
            QMessageBox.information(self, 'Export CSV',
                'Aucune donnée à exporter.\nLancez d\'abord la prévisualisation EPA.')
            return
        path, _ = QFileDialog.getSaveFileName(
            self, 'Exporter CSV', 'epa_perimetre_pm.csv', 'CSV (*.csv)')
        if not path:
            return
        if not path.lower().endswith('.csv'):
            path += '.csv'
        try:
            import csv as _csv
            rows = []
            for row in range(self.tbl_epa.rowCount()):
                if self.tbl_epa.isRowHidden(row):
                    continue
                id_epa = (self.tbl_epa.item(row, 0).text()
                          if self.tbl_epa.item(row, 0) else '')
                pmz    = (self.tbl_epa.item(row, 1).text()
                          if self.tbl_epa.item(row, 1) else '')
                rows.append([id_epa, pmz])
            with open(path, 'w', newline='', encoding='utf-8-sig') as f:
                writer = _csv.writer(f, delimiter=';')
                writer.writerow(['id_epa', 'pmz'])
                writer.writerows(rows)
            QMessageBox.information(self, 'Export CSV',
                f'{len(rows)} ligne(s) exportée(s) :\n{path}')
        except Exception as e:
            QMessageBox.critical(self, 'Erreur export CSV', str(e))

    def _run_extract_bal(self):
        bal_lyr = self.cb_bal_extract.currentLayer()
        if bal_lyr is None:
            QMessageBox.warning(self, 'Extraction BAL',
                'Aucune couche BAL sélectionnée.')
            return
        if not self._pm_set:
            QMessageBox.warning(self, 'Extraction BAL',
                'Aucun PM chargé. Veuillez d\'abord charger le périmètre PM.')
            return

        fnames = [f.lower() for f in bal_lyr.fields().names()]
        fnames_orig = bal_lyr.fields().names()

        def _pick(candidates):
            for c in candidates:
                if c.lower() in fnames:
                    return fnames_orig[fnames.index(c.lower())]
            return None

        id_field    = _pick(['id_metier', 'gid', 'fid'])
        prises_field = _pick(['prises', 'nb_prises', 'nb_pe'])
        pa_field    = _pick(['zapa', 'id_zapa', 'pa'])
        pm_field    = _pick(['sro', 'id_ftth_pf', 'pmz', 'pm', 'nom_pm'])

        if pm_field is None:
            QMessageBox.critical(self, 'Extraction BAL',
                f'Aucun champ PM trouvé dans "{bal_lyr.name()}".\n'
                'Champs attendus : sro, id_ftth_pf, pmz, pm, nom_pm.')
            return

        layer_id = bal_lyr.id()
        pm_set_lower = {str(p).lower() for p in self._pm_set}

        self.tbl_bal_ext.setRowCount(0)
        row_idx = 0
        n_pm_hit = set()

        for feat in bal_lyr.getFeatures():
            pm_val = feat[pm_field]
            if pm_val is None:
                continue
            if str(pm_val).lower() not in pm_set_lower:
                continue
            n_pm_hit.add(str(pm_val).lower())

            id_val     = str(feat[id_field])     if id_field     else str(feat.id())
            prises_val = str(feat[prises_field]) if prises_field else ''
            pa_val     = str(feat[pa_field])     if pa_field     else ''
            pmz_val    = str(pm_val)
            id_fld_lbl = id_field or 'id QGIS'
            pm_fld_lbl = pm_field

            self.tbl_bal_ext.insertRow(row_idx)
            it0 = QTableWidgetItem(id_val)
            it0.setData(Qt.ItemDataRole.UserRole + 1, feat.id())
            it0.setData(Qt.ItemDataRole.UserRole + 2, layer_id)
            self.tbl_bal_ext.setItem(row_idx, 0, it0)
            self.tbl_bal_ext.setItem(row_idx, 1, QTableWidgetItem(prises_val))
            self.tbl_bal_ext.setItem(row_idx, 2, QTableWidgetItem(pa_val))
            self.tbl_bal_ext.setItem(row_idx, 3, QTableWidgetItem(pmz_val))
            self.tbl_bal_ext.setItem(row_idx, 4, QTableWidgetItem(id_fld_lbl))
            self.tbl_bal_ext.setItem(row_idx, 5, QTableWidgetItem(pm_fld_lbl))
            row_idx += 1

        n = self.tbl_bal_ext.rowCount()
        n_pm = len(n_pm_hit)
        self.lbl_cnt_bal_ext.setText(f'{n} BAL')
        self.lbl_status.setText(
            f'BAL périmètre PM : {n} BAL exportables sur {n_pm} PM.')

    def _export_csv_bal(self):
        if self.tbl_bal_ext.rowCount() == 0:
            QMessageBox.information(self, 'Export CSV',
                'Aucune donnée à exporter.\nLancez d\'abord la prévisualisation BAL.')
            return
        path, _ = QFileDialog.getSaveFileName(
            self, 'Exporter CSV', 'bal_perimetre_pm.csv', 'CSV (*.csv)')
        if not path:
            return
        if not path.lower().endswith('.csv'):
            path += '.csv'
        try:
            import csv as _csv
            rows = []
            for row in range(self.tbl_bal_ext.rowCount()):
                if self.tbl_bal_ext.isRowHidden(row):
                    continue
                def _t(c):
                    it = self.tbl_bal_ext.item(row, c)
                    return it.text() if it else ''
                rows.append([_t(0), _t(1), _t(2), _t(3)])
            with open(path, 'w', newline='', encoding='utf-8-sig') as f:
                writer = _csv.writer(f, delimiter=';')
                writer.writerow(['id_bal', 'nb_prises', 'pa', 'pmz'])
                writer.writerows(rows)
            QMessageBox.information(self, 'Export CSV',
                f'{len(rows)} ligne(s) exportée(s) :\n{path}')
        except Exception as e:
            QMessageBox.critical(self, 'Erreur export CSV', str(e))

    # ─────────────────────────────────────────────────────────────────────────
    # ─────────────────────────────────────────────────────────────────────────
    # TAB 7 – Tableau de bord (in-app live report)
    # ─────────────────────────────────────────────────────────────────────────

    def _tab_rapport(self):
        root = QWidget()
        v = QVBoxLayout(root)
        v.setContentsMargins(8, 8, 8, 8)

        bar = QHBoxLayout()
        self.lbl_rapport_time = QLabel(
            'Lancez une analyse pour actualiser automatiquement ce tableau de bord.'
        )
        self.lbl_rapport_time.setStyleSheet('color: gray; font-style: italic; font-size: 11px;')
        bar.addWidget(self.lbl_rapport_time, 1)
        btn_refresh = QPushButton('🔄  Actualiser')
        btn_refresh.setToolTip('Recalcule le tableau de bord avec les résultats actuels.')
        btn_refresh.clicked.connect(self._refresh_rapport_tab)
        bar.addWidget(btn_refresh)
        v.addLayout(bar)

        self.tb_rapport = QTextBrowser()
        self.tb_rapport.setOpenExternalLinks(False)
        self.tb_rapport.setHtml(
            '<html><body style="font-family:Arial,sans-serif; color:#888888; padding:24px;">'
            '<p style="font-size:13px;">Aucune analyse disponible.<br/>'
            'Lancez les analyses dans les onglets précédents, le tableau de bord '
            's\'actualisera automatiquement.</p></body></html>'
        )
        v.addWidget(self.tb_rapport)

        return root

    def _refresh_rapport_tab(self):
        """Rebuild the in-app dashboard with the latest results from all analysis tabs."""
        if not hasattr(self, 'tb_rapport'):
            return
        chev = self._collect_table_data(self.tbl_chev)
        doub = self._collect_table_data(self.tbl_doub)
        parc = self._collect_table_data(self.tbl_parc)
        bal  = self._collect_table_data(self.tbl_bal)
        pa   = (self._collect_table_data(self.tbl_pa)
                if hasattr(self, 'tbl_pa') else [])
        charts = self._make_charts(chev, doub, parc, bal)
        self.tb_rapport.setHtml(
            self._build_tab_html(chev, doub, parc, bal, charts, pa=pa,
                                 pa_ignored=self._pa_ignored_empty_zapa))
        now = datetime.datetime.now().strftime('%H:%M:%S')
        self.lbl_rapport_time.setText(f'Dernière actualisation : {now}')
        self.lbl_rapport_time.setStyleSheet('font-size: 11px;')

    def _build_tab_html(self, chev, doub, parc, bal, charts, pa=None, pa_ignored=0):
        """Build QTextBrowser-compatible HTML for the in-app dashboard."""
        if pa is None:
            pa = []
        now = datetime.datetime.now().strftime('%d/%m/%Y à %H:%M')

        n_chev = len(chev)
        n_doub = len(doub)
        n_parc = len(parc)
        n_bal  = len(bal)
        n_pa_sans = sum(
            1 for r in pa if r.get('Diagnostic', '') == 'SANS INFRA')

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

        avg_dist_bal = ''
        bal_dists = []
        for r in bal:
            try:
                v = r.get('Dist. infra (m)', 'N/A')
                if v != 'N/A':
                    bal_dists.append(float(v.replace(',', '.')))
            except ValueError:
                pass
        if bal_dists:
            avg_dist_bal = f'{sum(bal_dists)/len(bal_dists):,.1f} m'

        pm_info = (f'{len(self._pm_set)} PM'
                   if self.chk_pm.isChecked() and self._pm_set else 'Aucun filtre PM')

        def _c(n):
            return '#c0392b' if n > 0 else '#27ae60'

        # KPI box (table-cell based, QTextBrowser-safe)
        def _kpi(val, label, color='#1a6faf', sub=''):
            sub_html = (f'<br/><span style="font-size:10px; color:#aaaaaa;">{sub}</span>'
                        if sub else '')
            return (
                f'<td style="padding:5px; width:25%;">'
                f'<table width="100%" cellspacing="0" cellpadding="8">'
                f'<tr><td style="background-color:white; border:1px solid #dee2e6; '
                f'border-radius:6px; text-align:center;">'
                f'<span style="font-size:22px; font-weight:bold; color:{color};">{val}</span><br/>'
                f'<span style="font-size:10px; color:#888888;">{label}</span>'
                f'{sub_html}'
                f'</td></tr></table></td>'
            )

        def _section(icon, title, color, body):
            return (
                f'<table width="100%" cellspacing="0" cellpadding="0" '
                f'style="margin-bottom:12px;">'
                f'<tr><td style="background-color:white; border:1px solid #dee2e6; '
                f'border-radius:8px; padding:14px;">'
                f'<p style="font-size:14px; font-weight:bold; color:{color}; '
                f'border-bottom:1px solid #dee2e6; padding-bottom:6px; margin:0 0 10px 0;">'
                f'{icon} {title}</p>'
                f'{body}'
                f'</td></tr></table>'
            )

        def _img(key):
            if key not in charts:
                return ''
            return (f'<p><img src="{charts[key]}" width="680" '
                    f'style="border:1px solid #dee2e6; border-radius:4px;"/></p>')

        def _pill(text):
            return (f'<span style="background-color:#e8f4f8; border:1px solid #b8dff0; '
                    f'border-radius:10px; padding:2px 8px; font-size:11px; '
                    f'color:#1a6faf; margin-right:4px;">{text}</span>')

        # ── Synthèse ──────────────────────────────────────────────────────────
        synthese = _section('📋', 'Synthèse générale', '#1a6faf',
            f'<table width="100%" cellspacing="0" cellpadding="0"><tr>'
            f'{_kpi(n_chev, "Chevauchements", _c(n_chev), f"{total_ov_chev:,.1f} m")}'
            f'{_kpi(n_doub, "Doublons", _c(n_doub), f"{total_ov_doub:,.1f} m")}'
            f'{_kpi(n_parc, "Parcours listés", "#1a6faf", f"{total_len_parc:,.0f} m total")}'
            f'{_kpi(n_bal, "BAL éloignées", _c(n_bal), avg_dist_bal or "—")}'
            f'{_kpi(n_pa_sans, "PA sans infra", _c(n_pa_sans))}'
            f'</tr></table>'
        )

        # ── Chevauchements ────────────────────────────────────────────────────
        layer_cnt = Counter(r.get('Couche conf.', '—') for r in chev)
        pills_html = ''.join(_pill(f'{k}: <b>{v}</b>') for k, v in layer_cnt.most_common())
        chev_body = (
            f'<table width="100%" cellspacing="0" cellpadding="0"><tr>'
            f'{_kpi(n_chev, "Conflits détectés", _c(n_chev))}'
            f'{_kpi(f"{total_ov_chev:,.1f} m", "Cumul chevauchement")}'
            f'<td></td><td></td>'
            f'</tr></table>'
            + (f'<p style="margin:8px 0;">{pills_html}</p>' if pills_html else '')
            + _img('chev_layers')
            + _img('chev_hist')
        )
        chev_section = _section('⚠', 'Chevauchements C0 / Existant', '#c0392b', chev_body)

        # ── Doublons ──────────────────────────────────────────────────────────
        doub_body = (
            f'<table width="100%" cellspacing="0" cellpadding="0"><tr>'
            f'{_kpi(n_doub, "Paires en doublon", _c(n_doub))}'
            f'{_kpi(f"{total_ov_doub:,.1f} m", "Cumul superposition")}'
            f'<td></td><td></td>'
            f'</tr></table>'
        )
        doub_section = _section('⛔', 'Doublons Infra', '#c0392b', doub_body)

        # ── Parcours ──────────────────────────────────────────────────────────
        parc_body = (
            f'<table width="100%" cellspacing="0" cellpadding="0"><tr>'
            f'{_kpi(n_parc, "Parcours listés", "#1a6faf")}'
            f'{_kpi(f"{total_len_parc:,.0f} m", "Longueur totale")}'
            f'<td></td><td></td>'
            f'</tr></table>'
            + _img('parc_top10')
        )
        parc_section = _section('📏', 'Parcours les plus longs', '#1a6faf', parc_body)

        # ── BAL ───────────────────────────────────────────────────────────────
        bal_body = (
            f'<table width="100%" cellspacing="0" cellpadding="0"><tr>'
            f'{_kpi(n_bal, "BAL éloignées", _c(n_bal))}'
            f'{_kpi(avg_dist_bal or "—", "Distance moy. à infra")}'
            f'<td></td><td></td>'
            f'</tr></table>'
            + _img('bal_dist')
        )
        bal_section = _section('📍', 'BAL éloignées infra', '#e67e22', bal_body)

        # ── PA sans infra ─────────────────────────────────────────────────────
        n_disc_pa = len(pa) - n_pa_sans
        pa_body = (
            f'<table width="100%" cellspacing="0" cellpadding="0"><tr>'
            f'{_kpi(n_pa_sans, "ZAPA sans infra", _c(n_pa_sans))}'
            f'{_kpi(n_disc_pa, "Discordances", _c(n_disc_pa) if n_disc_pa else "#888888")}'
            f'{_kpi(pa_ignored, "ZAPA sans BAL ignorées", "#888888")}'
            f'<td></td>'
            f'</tr></table>'
        )
        pa_section = _section('🚫', 'PA sans infra', '#8e44ad', pa_body)

        return (
            '<!DOCTYPE html><html><head><meta charset="UTF-8"></head>'
            '<body style="background-color:#f8f9fa; font-family:Arial,sans-serif; '
            'color:#212529; margin:0; padding:10px;">'

            # Header
            '<table width="100%" cellspacing="0" cellpadding="0" '
            'style="margin-bottom:12px;">'
            '<tr><td style="background-color:#1a6faf; color:white; '
            'border-radius:6px; padding:14px 16px;">'
            '<span style="font-size:16px; font-weight:bold;">Tableau de bord — QD RIP Auvergne</span><br/>'
            f'<span style="font-size:11px; color:#cce4f7;">'
            f'Actualisé le {now} &nbsp;·&nbsp; Périmètre : {pm_info}</span>'
            '</td></tr></table>'

            + synthese
            + chev_section
            + doub_section
            + parc_section
            + bal_section
            + pa_section

            + '<p style="font-size:10px; color:#aaaaaa; text-align:center; margin-top:8px;">'
            'Plugin QD RIP Auvergne v1.1.7 — Pleymove</p>'
            '</body></html>'
        )

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
                    ax.set_xlabel("Distance à l'infra la plus proche (m)", fontsize=9)
                    ax.set_ylabel('Nombre de BAL', fontsize=9)
                    ax.set_title('Distribution des distances BAL → Infra', fontsize=11,
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
                <h2>📍 BAL éloignées infra</h2>
                <div class="kpi-row">
                    {_kpi(n_bal, 'BAL éloignées',
                          '#c0392b' if n_bal > 0 else '#27ae60')}
                    {_kpi(avg_dist or "—", "Distance moy. à l'infra")}
                </div>
                {_chart('bal_dist', 'Distribution des distances BAL → Infra')}
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
    <span>🔧 Plugin QD RIP Auvergne v1.0.8</span>
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
  Rapport généré par le plugin <strong>QD RIP Auvergne v1.0.8</strong> — Pleymove
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
