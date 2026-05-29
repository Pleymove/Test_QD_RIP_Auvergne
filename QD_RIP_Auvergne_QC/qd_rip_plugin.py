from PyQt5.QtWidgets import QAction
from PyQt5.QtGui import QIcon
from qgis.utils import iface as qgis_iface


class QDRIPPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.dialog = None
        self.action = None

    def initGui(self):
        self.action = QAction('QD RIP Auvergne', self.iface.mainWindow())
        self.action.setToolTip('Contrôle qualité RIP Auvergne')
        self.action.triggered.connect(self.run)
        self.iface.addToolBarIcon(self.action)
        self.iface.addPluginToMenu('QD RIP Auvergne', self.action)

    def unload(self):
        self.iface.removePluginMenu('QD RIP Auvergne', self.action)
        self.iface.removeToolBarIcon(self.action)
        if self.dialog:
            self.dialog.close()
            self.dialog = None

    def run(self):
        from .qd_rip_dialog import QDRIPDialog
        if self.dialog is None:
            self.dialog = QDRIPDialog(self.iface.mainWindow())
            self.dialog.finished.connect(self._on_dialog_closed)
        self.dialog.show()
        self.dialog.raise_()
        self.dialog.activateWindow()

    def _on_dialog_closed(self):
        self.dialog = None
