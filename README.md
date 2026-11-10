# Soap à l'un

Outil CLI de duplication et migration de serveurs Zimbra vers une destination.
La source est toujours lue en lecture seule ; la destination est uniquement
complétée, jamais vidée.

## Fonctions

- Connexions admin SOAP source et destination.
- Inventaires JSON : domaines, COS, comptes, alias, listes, ressources.
- Provisioning des domaines, COS, comptes, listes de distribution et ressources.
- Copie des boîtes mail, agendas, contacts et tâches.
- Copie additive des réglages, filtres, partages et droits.
- Mots de passe fixes ou aléatoires.
- Migration différentielle par date (`--since`).
- Reprise ciblée des échecs (`--retry-failed`).
- Rapports JSON et export CSV.

Aucune suppression n'est effectuée sur la source ni sur la destination.

## Installation

### Linux / Zimbra

```bash
sudo ./install.sh
```

La CLI est installée dans `/opt/soaplun` (ou `SOAPLUN_DIR`) avec un virtualenv.
Aucun service web n'est installé.

### Windows

```powershell
.\install.ps1
```

### Développement

```bash
pip install -e .
soapalin --help
```

## Serveurs

Les serveurs sont configurés dans `config/servers/<label>.json`.

```bash
soapalin --server-action create --server-type source --server-label src1
soapalin --server-action create --server-type destination --server-label dst1
soapalin --server-action list
```

Éditer ensuite les fichiers `config/servers/src1.json` et
`config/servers/dst1.json` : hôte STORE, port admin, hôte PROXY, port web,
compte administrateur et mot de passe.

```bash
soapalin --server-action test --server-label src1
soapalin --server-action inventory --server-label src1
soapalin --server-action inventory --server-label dst1
soapalin --server-action show --server-label src1
soapalin --server-action rename --server-label src1 --new-label src2
soapalin --server-action remove --server-label src2
```

## Migration

```bash
# Provisioning des objets globaux
soapalin --create-domain domains.csv --server-source src1 --server-destination dst1
soapalin --create-cos cos.csv --server-source src1 --server-destination dst1
soapalin --import-cos all --server-source src1 --server-destination dst1
soapalin --create-dl lists.csv --server-source src1 --server-destination dst1
soapalin --create-resources resources.csv --server-source src1 --server-destination dst1

# Tous les objets d'une catégorie
soapalin --create-resources all --server-source src1 --server-destination dst1
soapalin --create-dl all --server-source src1 --server-destination dst1

# Comptes
soapalin --create-user --password "Secret!" \
  --server-source src1 --server-destination dst1 --list-user users.csv

soapalin --create-user --random-password \
  --server-source src1 --server-destination dst1 --list-user users.csv

# Contenus et réglages
soapalin --import-user all \
  --server-source src1 --server-destination dst1 --list-user users.csv

# Briefcase uniquement
soapalin --import-user briefcase \
  --server-source src1 --server-destination dst1 --list-user users.csv

# Tags uniquement
soapalin --import-user tags \
  --server-source src1 --server-destination dst1 --list-user users.csv

# R?appliquer la COS source (m?me nom) sur les comptes
soapalin --import-user cos \
  --server-source src1 --server-destination dst1 --list-user users.csv

# Ressources : objets + agendas + mails
soapalin --import-resources resources.csv \
  --server-source src1 --server-destination dst1

soapalin --import-resources all \
  --server-source src1 --server-destination dst1

# Rattrapage différentiel
soapalin --import-user mail --since 2026-11-01 \
  --server-source src1 --server-destination dst1 --list-user users.csv

# Reprise du dernier rapport en échec
soapalin --import-user mail --retry-failed \
  --server-source src1 --server-destination dst1

# Rapport précis
soapalin --import-user mail --retry-failed data/reports/mailboxes-20261107-100000.json \
  --server-source src1 --server-destination dst1
```

`--dry-run` simule les opérations sans écrire sur la destination.
`--random-password` écrit les mots de passe générés dans
`data/reports/passwords-<date>.csv`.

## Jobs en arriere-plan

Les migrations, provisioning et inventaires sont lances en arriere-plan par
defaut. La commande affiche l'identifiant du job et rend la main immediatement.

```bash
soapalin --jobs all --jobs-action list
soapalin --jobs all --jobs-action status
soapalin --jobs all --jobs-action live
soapalin --jobs all --jobs-action stop
soapalin --jobs 20261110-120305-29c057 --jobs-action status
soapalin --jobs 20261110-120305-29c057 --jobs-action stop
```

Pour forcer l'execution au premier plan :

```bash
soapalin --foreground --import-user mail \
  --server-source src1 --server-destination dst1 --list-user users.csv
```

## Options utiles

```text
-q, --quiet
-v, --verbose
--debug
--dry-run
--keep-tgz true|false
--keep-tgz-days N
--parallelism N
--mapping-domain CSV
--mapping-host CSV
```

## Données

```text
config/config.json       configuration globale
config/servers/          serveurs source/destination
data/snapshots/          inventaires
data/mailboxes/          archives TGZ temporaires
data/reports/            rapports JSON, CSV et sauvegardes
data/jobs/               état des tâches
```

## Tests

```bash
pytest
python -m unittest discover -s tests
```
