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
| BAL Isolées | BAL sans voisin dans un rayon configurable (défaut 500 m), corrélées avec l'infra C0 la plus proche et son linéaire |
| PA sans infra | ZAPA du périmètre PM courant sans infra dans le groupement livrables (double contrôle attributaire id_pa + spatial) |
| Tableau de bord | Synthèse des résultats des autres onglets avec indicateurs clés |

## Changelog

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
