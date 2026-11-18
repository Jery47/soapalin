# soapalin

Outil CLI de duplication, de provisioning et de migration de serveurs Zimbra.

La source est toujours lue en lecture seule. La destination est uniquement
complétée, jamais vidée. L'outil est conçu pour les migrations de comptes, de
ressources calendrier et de listes de distribution.

## Fonctionnalités

- gestion multi-serveurs avec un fichier `config/servers/<label>.json` ;
- inventaire source et destination en lecture seule ;
- provisioning des domaines, COS, listes, ressources et comptes ;
- migration des boîtes, agendas, contacts, tâches, briefcase, tags et réglages ;
- migration des partages et des mountpoints acceptés ;
- création des dossiers vides, couleurs et tags personnalisés ;
- jobs en arrière-plan avec journaux et reprise ;
- nettoyage des données temporaires et des partages destination.

## Installation

Prérequis : Python 3.8 ou supérieur.

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
soapalin --help
```

Pour le développement :

```bash
python -m pip install -e ".[dev]"
```

Les scripts `install.sh`, `install.ps1`, `uninstall.sh` et `uninstall.ps1`
permettent une installation dédiée sur un serveur Zimbra ou sous Windows.

## Démarrage rapide

```bash
soapalin server add src1 --role source
soapalin server add dst1 --role destination

soapalin server inventory src1
soapalin server inventory dst1

soapalin provision users.csv \
  --type account \
  --from src1 \
  --to dst1 \
  --password "Secret!"

soapalin migrate users.csv \
  --type account \
  --item mail,calendar,settings \
  --from src1 \
  --to dst1
```

## Serveurs

```bash
soapalin server add src1 --role source
soapalin server add src1 --role source \
  --host zimbra-src.local --port 7071 \
  --admin admin --password "Secret!" --no-test
soapalin server list
soapalin server show src1
soapalin server test src1
soapalin server inventory src1
soapalin server inventory src1 --stats
soapalin server inventory src1 --stats --parallelism 4
soapalin server inventory src1 --follow
soapalin server rename src1 zimbra-prod
soapalin server remove src1
```

Les inventaires sont collectés sans statistiques de boîtes par défaut.
`--stats` active `GetMailbox` par compte pour récupérer les tailles et volumes.
`--no-stats` reste accepté pour compatibilité.

## Provisioning

```bash
soapalin provision domains.csv --type domain --from src1 --to dst1
soapalin provision all --type cos --from src1 --to dst1
soapalin provision lists.csv --type dl --from src1 --to dst1
soapalin provision resources.csv --type resource --from src1 --to dst1

soapalin provision users.csv --type account --from src1 --to dst1 --password "Secret!"
soapalin provision users.csv --type account --from src1 --to dst1 --password-file pwd.txt
soapalin provision users.csv --type account --from src1 --to dst1 --random-password --keep-admin
```

`all` remplace le CSV. Les mappings disponibles sont `--mapping-domain` et
`--mapping-host`.

## Migration

```bash
soapalin migrate users.csv --type account \
  --item mail,calendar,settings \
  --from src1 --to dst1

soapalin migrate all --type account --item all --from src1 --to dst1
soapalin migrate resources.csv --type resource --item mail,calendar --from src1 --to dst1
soapalin migrate lists.csv --type dl --item members --from src1 --to dst1
soapalin migrate all --type all --item all --from src1 --to dst1
```

`--item` est obligatoire. Les valeurs sont séparées par des virgules ; les
espaces autour des virgules sont tolérés.

Éléments valides :

```text
account  : mail calendar contacts tasks briefcase cos folder color settings
           tags filter banlist whitelist share sendas sendonbehalfof
resource : mail calendar folder share
dl       : members
```

`--type all` accepte un CSV de comptes : les comptes sont filtrés par le CSV,
les ressources et les listes sont traitées en `all`.

### COS

L'élément `cos` applique la COS source aux comptes déjà existants. Les COS
doivent d'abord être créées sur la destination, puis l'inventaire destination
doit être rafraîchi :

```bash
soapalin --foreground provision all --type cos --from src1 --to dst1
soapalin --foreground server inventory dst1
soapalin --foreground migrate users.csv --type account --item cos --from src1 --to dst1
```

### Partages

`share` migre les partages sortants et recrée les partages entrants déjà
acceptés sur la source. Les mountpoints déjà présents sont conservés et leurs
couleurs sont synchronisées.

### Dossiers et couleurs

- `folder` crée uniquement l'arborescence de dossiers manquante ;
- `color` applique uniquement les couleurs aux dossiers existants ;
- `folder` est exécuté avant `color` lorsque les deux sont demandés.

```bash
soapalin migrate users.csv --type account --item folder --from src1 --to dst1
soapalin migrate users.csv --type account --item color --from src1 --to dst1
soapalin migrate users.csv --type account --item folder,color --from src1 --to dst1
```

### Différentiel et reprise

```bash
soapalin migrate users.csv --type account \
  --item mail,calendar,settings \
  --from src1 --to dst1 --differential

soapalin migrate users.csv --type account \
  --item mail \
  --from src1 --to dst1 --retry-failed
```

## Simulation et options

Les options globales se placent avant la sous-commande :

```bash
soapalin --dry-run migrate users.csv --type account --item all --from src1 --to dst1
soapalin --foreground migrate users.csv --type account --item mail --from src1 --to dst1
```

Options de migration :

```text
--differential [YYYY-MM-DD]
--retry-failed [RAPPORT]
--parallelism N
--keep-tgz [true|false]
--keep-tgz-days N
--mapping-domain CSV
--mapping-host CSV
--resume
--json
```

`--json` n'est autorisé qu'avec `--dry-run`.

## Jobs

Les provisioning, migrations et inventaires sont lancés en arrière-plan par
défaut.

```bash
soapalin job list
soapalin job list --running
soapalin job list --failed
soapalin job status JOB_ID --tail 30
soapalin job log JOB_ID
soapalin job live
soapalin job stop all
```

Les états et journaux sont stockés dans `data/jobs/<id>/`.

## Nettoyage

Données temporaires :

```bash
soapalin clean --dry-run
soapalin clean mailboxes --older-than 7
soapalin clean all --older-than 30 --keep-last 5
soapalin clean snapshots --keep-last 10
```

Partages destination :

```bash
soapalin clean --share users.csv --type account --to dst1 --dry-run
soapalin clean --share --type account --to dst1 --parallelism 10
soapalin clean --share users.csv --type account --from src1 --to dst1
```

Sans `--from`, tous les mountpoints de partage des comptes sélectionnés sont
supprimés. Avec `--from`, seuls les mountpoints absents de la source sont
supprimés.

## Données locales

```text
config/config.json       configuration globale
config/servers/          serveurs source/destination
data/snapshots/          inventaires
data/mailboxes/          archives TGZ temporaires
data/reports/            rapports JSON/CSV et sauvegardes
data/jobs/               états et journaux des jobs
data/trash/              archives déplacées par --directly-clean
```

Ces répertoires sont ignorés par Git.

## Tests et qualité

```bash
pytest
pytest --cov=soaplun --cov-report=term-missing
ruff check soaplun tests
mypy soaplun
python -m unittest discover -s tests
```

## Intégration GitHub

Le dépôt est prêt pour GitHub Actions :

```text
.github/workflows/ci.yml       tests, lint et build
.github/ISSUE_TEMPLATE/        modèles de bug et de demande
.github/pull_request_template.md
CONTRIBUTING.md
.gitattributes
.editorconfig
```

## Contribution

Les instructions de contribution sont décrites dans `CONTRIBUTING.md`.
