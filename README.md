# QD RIP Auvergne – Plugin QGIS

Plugin de contrôle qualité des données RIP Auvergne.

## Installation dans QGIS

1. **Extensions → Gérer et installer des extensions → Paramètres → Ajouter un dépôt**
2. Remplir :
   - **Nom :** `QD RIP Auvergne`
   - **URL :** `https://raw.githubusercontent.com/Pleymove/Test_QD_RIP_Auvergne/main/plugins.xml`
3. Onglet **Toutes** → rechercher `QD RIP Auvergne` → **Installer**
4. Pour les mises à jour : le bouton **Mettre à jour** apparaît automatiquement dans QGIS à chaque nouvelle release publiée sur ce dépôt.

## Installation manuelle (script)

- **Windows :** double-clic sur `install_plugin.bat`
- **Linux / Mac :** `bash install_plugin.sh`

## Fonctionnalités

| Onglet | Description |
|---|---|
| Chevauchements C0 / Existant | Détecte les infra C0 qui passent sur `ft_arciti`, `bt`, `athd_artere` (filtre `dispopp_ar != 0`) ou `t_cheminement` |
| Doublons Infra | Paires d'entités C0 dont les tracés se superposent |
| Parcours les plus longs | Classement par longueur décroissante (Top N configurable) |
| BAL éloignées infra | Contrôle de la distance BAL→infra par PM. Mode isolation (rayon 500 m par défaut) ou analyse globale. Filtre distance infra configurable (défaut 1000 m) |
| PA sans infra | ZAPA du périmètre PM courant sans infra dans le groupement livrables (double contrôle attributaire id_pa + spatial) |
| Extractions | Extraction EPA/PA filtrée sur le périmètre PM courant, export CSV/Excel/SHP |
| Tableau de bord | Synthèse des résultats des autres onglets avec indicateurs clés |

## Changelog

### Version 1.1.9
- Ajout d'un nouvel onglet **📤 Extractions**
- Première extraction : **EPA / PA du périmètre PM courant**, filtré sur `self._pm_set`
- Colonnes exportées : `id_epa` (id_metier → id_ftth → gid → fid), `pmz` (sro → id_ftth_pf → pmz → pm → nom_pm)
- Auto-détection robuste de la couche EPA/PA (jamais ZAPA)
- Export **CSV** (`id_epa;pmz`, UTF-8-sig, séparateur `;`)
- Export **Excel** et **SHP** disponibles
- Zoom / Sélectionner dans QGIS sur les lignes du tableau

### Version 1.1.8
- Renommage de l'onglet **BAL isolées** en **BAL éloignées infra**
- Ajout d'un mode permettant d'analyser toutes les BAL du périmètre PM sans appliquer le rayon d'isolation (option « Utiliser le rayon d'isolation », cochée par défaut à 500 m)
- Ajout d'un filtre facultatif sur la distance à l'infra la plus proche dans le même PM (option « Filtrer par distance », cochée par défaut à 1000 m)
- Les seuils sont librement saisissables (plage 0–999 999 999 m)
- Les BAL sans infra trouvée dans leur PM sont toujours remontées quel que soit le filtre distance
- Mise à jour des compteurs, exports (renommés `bal_eloignees`) et tableau de bord

### Version 1.1.7
- Correction de l'onglet **PA sans infra** : exclusion des ZAPA sans BAL
- Ajout de la couche BAL dans la configuration de l'onglet (auto-détection PostGIS `rad_aw_2026.bal`)
- Comptage BAL par ZAPA : priorité attributaire (`bal.zapa = zapa.id_metier`), fallback spatial
- Les ZAPA avec 0 BAL sont ignorées (absence d'infra est normale)
- Colonne **Nb BAL** et **Source BAL** ajoutées dans le tableau et les exports
- Compteur indiquant le nombre de ZAPA sans BAL ignorées (tableau de bord inclus)

### Version 1.1.6
- Correction de l'auto-sélection des couches dans l'onglet **PA sans infra**
- Nouvelle détection prioritaire : source PostGIS (`table="rad_aw_2026"."zapa"/"infra"`), puis nom exact (`zapa`/`infra` avec validation des champs), puis nom contenant `livrable_zapa`/`livrable_infra`
- Mise à jour des libellés et tooltips des combos couche dans l'onglet PA sans infra
- Messages d'erreur améliorés : affichent le nom de la couche sélectionnée en cas de champ manquant

### Version 1.1.5
- Ajout de l'onglet **PA sans infra** : contrôle des ZAPA du périmètre PM courant
- Détection des ZAPA sans infra dans `livrable_infra` (via `livrable_zapa`)
- Double contrôle : attributaire (`livrable_infra.id_pa ↔ livrable_zapa.id_metier`) et spatial (intersection ZAPA / infra avec tolérance configurable)
- Résultat : une ligne par ZAPA
- Option pour afficher les discordances attributaires / spatiales
- Exports Excel, SHP des ZAPA visibles
- Gestion automatique des CRS différents entre les deux couches

### Version 1.1.4
- Export Excel (.xlsx) en tableau structuré à la place du CSV sur tous les onglets

### Version 1.1.3
- Onglet BAL : colonne SRO BAL (vérification croisée) + export SHP avec liste des IDs ignorés copiable

### Version 1.1.2
- Bouton « Exporter SHP » sur tous les onglets d'analyse
