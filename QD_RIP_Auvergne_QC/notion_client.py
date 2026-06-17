# -*- coding: utf-8 -*-
"""
Client HTTP minimal pour l'API Notion (lecture des bases de suivi PA / BAL).

Utilise QgsBlockingNetworkRequest (synchrone, fourni par QGIS) afin de
rester cohérent avec le reste du plugin, qui n'a pas de boucle d'événements
asynchrone dédiée pour les appels réseau.

Sécurité : ce module ne contient et ne stocke aucun jeton. Le jeton est
toujours fourni par l'appelant (lu depuis QSettings côté UI) et n'est
jamais journalisé ni écrit sur disque ici.
"""

import json

from qgis.PyQt.QtCore import QUrl, QByteArray, QObject, pyqtSignal
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtNetwork import QNetworkRequest
from qgis.core import QgsBlockingNetworkRequest

# Compatibilité QGIS 3.16 -> 4.x : énumération scopée (Qt6) ou non selon la version
try:
    _NO_ERROR = QgsBlockingNetworkRequest.ErrorCode.NoError
except AttributeError:
    _NO_ERROR = QgsBlockingNetworkRequest.NoError

NOTION_API_VERSION = '2022-06-28'
NOTION_API_BASE = 'https://api.notion.com/v1'
NOTION_PAGE_SIZE = 100
NOTION_TIMEOUT_MS = 10000

# Identifiants des bases Notion (database_id, 32 caractères hexadécimaux).
# Ce ne sont PAS des secrets : ils peuvent être codés en dur. Ils sont
# extraits de l'URL des bases Notion (chaîne avant le "?", tirets retirés).
#   - BDD Suivi PA Auvergne  : https://app.notion.com/p/37a1eb58862a805bb9a8d6ecff31e805
#   - BDD Suivi BAL Auvergne : https://app.notion.com/p/37a1eb58862a80abb5b6c6db9a13ca93
NOTION_DB_PA = '37a1eb58862a805bb9a8d6ecff31e805'
NOTION_DB_BAL = '37a1eb58862a80abb5b6c6db9a13ca93'

# Couleurs de statut Notion -> QColor (point de couleur affiché dans les tableaux)
NOTION_COLOR_MAP = {
    'default': '#9b9a97',
    'gray':    '#9b9a97',
    'brown':   '#64473a',
    'orange':  '#d9730d',
    'yellow':  '#dfab01',
    'green':   '#0f7b6c',
    'blue':    '#0b6e99',
    'purple':  '#6940a5',
    'pink':    '#ad1a72',
    'red':     '#e03e3e',
}


class NotionClientError(Exception):
    """Erreur levée lors d'un appel à l'API Notion (réseau, auth, format)."""


def notion_color_to_qcolor(color_name):
    """Convertit un nom de couleur de statut Notion en QColor (gris par défaut)."""
    hex_color = NOTION_COLOR_MAP.get((color_name or '').lower(), NOTION_COLOR_MAP['default'])
    return QColor(hex_color)


def fetch_database_states(database_id, token, title_field, status_field='État'):
    """
    Interroge une base de données Notion et retourne un dict :
        { <valeur du titre de page> : {"etat": str, "couleur": str, "url": str} }

    - database_id  : identifiant de la base Notion (32 caractères hex)
    - token        : jeton d'intégration Notion (Bearer)
    - title_field  : nom de la propriété "titre" servant de clé de jointure
                     (ex. "id_epa" ou "id_bal")
    - status_field : nom de la propriété "status" Notion (par défaut "État")

    Lève NotionClientError en cas d'échec (réseau, authentification, format
    de réponse inattendu). Ne laisse jamais fuiter une autre exception.
    """
    if not database_id or not token:
        raise NotionClientError('Identifiant de base ou jeton Notion manquant.')

    result = {}
    start_cursor = None

    try:
        while True:
            payload = {'page_size': NOTION_PAGE_SIZE}
            if start_cursor:
                payload['start_cursor'] = start_cursor

            url = f'{NOTION_API_BASE}/databases/{database_id}/query'
            request = QNetworkRequest(QUrl(url))
            request.setHeader(QNetworkRequest.KnownHeaders.ContentTypeHeader,
                               'application/json')
            request.setRawHeader(b'Authorization', f'Bearer {token}'.encode('utf-8'))
            request.setRawHeader(b'Notion-Version', NOTION_API_VERSION.encode('utf-8'))
            if hasattr(request, 'setTransferTimeout'):
                request.setTransferTimeout(NOTION_TIMEOUT_MS)

            body = QByteArray(json.dumps(payload).encode('utf-8'))
            blocking = QgsBlockingNetworkRequest()
            err = blocking.post(request, body)

            if err != _NO_ERROR:
                raise NotionClientError(
                    f'Erreur réseau Notion : {blocking.errorMessage() or err}')

            reply = blocking.reply()
            raw = bytes(reply.content())
            try:
                data = json.loads(raw.decode('utf-8'))
            except ValueError as exc:
                raise NotionClientError(f'Réponse Notion illisible : {exc}')

            if data.get('object') == 'error':
                raise NotionClientError(
                    f"Erreur API Notion : {data.get('message', 'inconnue')}")

            for page in data.get('results', []):
                props = page.get('properties', {})
                title_prop = props.get(title_field) or {}
                title_list = title_prop.get('title') or []
                if not title_list:
                    continue
                page_key = (title_list[0].get('plain_text') or '').strip()
                if not page_key:
                    continue

                status_prop = props.get(status_field) or {}
                status = status_prop.get('status') or {}

                result[page_key] = {
                    'etat': status.get('name') or '',
                    'couleur': status.get('color') or '',
                    'url': page.get('url') or '',
                }

            if not data.get('has_more'):
                break
            start_cursor = data.get('next_cursor')
            if not start_cursor:
                break

    except NotionClientError:
        raise
    except Exception as exc:
        raise NotionClientError(f'Échec de la lecture de la base Notion : {exc}')

    return result


class NotionFetchWorker(QObject):
    """Worker exécuté dans un QThread dédié pour charger les états PA/BAL.

    QgsBlockingNetworkRequest est conçu pour être utilisé depuis un thread
    secondaire (QGIS fournit une instance de QgsNetworkAccessManager par
    thread) : l'appel reste synchrone à l'intérieur de ce worker, mais le
    thread GUI n'est jamais bloqué puisque run() s'exécute hors thread GUI.
    """

    # pa_map, bal_map (dict ou None en cas d'échec), msg_pa, msg_bal
    finished = pyqtSignal(object, object, str, str)

    def __init__(self, token):
        super().__init__()
        self._token = token

    def run(self):
        pa_map = bal_map = None
        msg_pa = msg_bal = ''
        try:
            pa_map = fetch_database_states(NOTION_DB_PA, self._token, 'id_epa')
        except Exception as exc:
            msg_pa = str(exc)
        try:
            bal_map = fetch_database_states(NOTION_DB_BAL, self._token, 'id_bal')
        except Exception as exc:
            msg_bal = str(exc)
        self.finished.emit(pa_map, bal_map, msg_pa, msg_bal)
