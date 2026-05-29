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
