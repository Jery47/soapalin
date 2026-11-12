# Soap à l'un

Outil CLI de duplication et migration de serveurs Zimbra vers une destination.
La source est toujours lue en lecture seule ; la destination est uniquement
complétée, jamais vidée.

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

## Interface

```text
soapalin [options globales] <commande> [options]
```

Options globales, avant la sous-commande :

```text
-h, --help
--version
-q, --quiet
-v, --verbose
--debug
--dry-run
--foreground
```

Sous-commandes : `server`, `provision`, `migrate`, `job`.

## Architecture

```text
soaplun/cli.py             parser et dispatch
soaplun/runner.py          orchestration partagee CLI
soaplun/pipeline.py        flux provision/migrate
soaplun/planner.py         construction des plans
soaplun/diff.py            comparaisons/normalisations
soaplun/apply.py           execution destination
soaplun/mailbox*.py        copies de boites (backend/archive/orchestration)
soaplun/settings*.py       reglages, filtres, partages, droits
soaplun/inventory*.py      collecte et persistance des inventaires
soaplun/cleanup.py         clean et deplacement vers trash
soaplun/storage.py         ecritures atomiques
soaplun/schema.py          validation des artefacts
```

## Serveurs

```bash
soapalin server add src1 --role source
soapalin server add dst1 --role destination
soapalin server list
soapalin server show src1
soapalin server test src1
soapalin server inventory src1
soapalin server inventory src1 --no-stats
soapalin server inventory src1 --no-stats --parallelism 4
soapalin server rename src1 zimbra-prod
soapalin server remove src1
```

`server inventory --no-stats` ?vite un `GetMailbox` par compte : l'inventaire est plus rapide, mais les tailles et volumes des bo?tes ne sont pas collect?s.

`--parallelism N` collecte les comptes avec plusieurs sessions ind?pendantes (d?faut : 1).

Les serveurs sont stockés dans `config/servers/<label>.json`. Editer ensuite
ces fichiers pour renseigner les hôtes, ports, comptes admin et mots de passe.

## Provisioning

```bash
soapalin provision domains.csv --type domain --from src1 --to dst1
soapalin provision all --type cos --from src1 --to dst1
soapalin provision dl lists.csv --from src1 --to dst1
soapalin provision resource resources.csv --from src1 --to dst1

soapalin provision users.csv --type account --from src1 --to dst1 --password "Secret!"
soapalin provision users.csv --type account --from src1 --to dst1 --password-file pwd.txt
soapalin provision users.csv --type account --from src1 --to dst1 --random-password --keep-admin
```

`all` est accepté à la place du CSV. Les mappings disponibles :

```text
--mapping-domain CSV
--mapping-host CSV
```

`provision account` applique la COS source d?s la cr?ation du compte, puis ajoute une action `modify_account` pour confirmer la COS apr?s cr?ation.

## Migration

```bash
soapalin migrate users.csv mail calendar settings --type account --from src1 --to dst1
soapalin migrate all --type account --from src1 --to dst1
soapalin migrate resources.csv mail calendar --type resource --from src1 --to dst1
soapalin migrate lists.csv members --type dl --from src1 --to dst1
soapalin migrate all --type all --from src1 --to dst1
```

Éléments valides :

```text
account  : mail calendar contacts tasks briefcase cos folder color settings
           tags filter banlist whitelist share sendas sendonbehalfof
resource : mail calendar share
dl       : members
```

`--type` definit le type d'objet migre. `dl` n'accepte que `members` : une liste de distribution n'a ni dossiers, ni settings, ni partages.

`--type all` exige le CSV `all`. Les tags manquants sont resynchronisés en mode
différentiel.

L'?l?ment `cos` applique uniquement la COS source aux comptes d?j? existants.
Il faut d'abord cr?er les COS, puis rafra?chir l'inventaire destination :

```bash
soapalin --foreground provision all --type cos --from src1 --to dst1
soapalin --foreground server inventory dst1 --no-stats
soapalin --foreground migrate users.csv cos --type account --from src1 --to dst1
```

### Partages

`share` migre les partages sortants et recr?e en mode accept? les partages
entrants d?j? accept?s sur la source (`mid` pr?sent). Les partages entrants
non accept?s sont ignor?s.

### Dossiers

- `folder` cr?e uniquement l'arborescence de dossiers manquante, sans copier les messages.
- `color` applique uniquement les couleurs aux dossiers existants, sans cr?er de dossier.
- Les deux peuvent ?tre combin?s : `folder` est ex?cut? avant `color`.

```bash
soapalin migrate users.csv folder --type account --from src1 --to dst1
soapalin migrate users.csv color --type account --from src1 --to dst1
soapalin migrate users.csv folder color --type account --from src1 --to dst1
```

### Différentiel

```bash
soapalin migrate users.csv mail calendar settings --type account \
  --from src1 --to dst1 --differential

soapalin migrate users.csv mail calendar settings --type account \
  --from src1 --to dst1 --differential=2026-10-01
```

La date filtre les arrivées pour les mails et les modifications pour les
contacts, agendas et tâches.

### Reprise des échecs

```bash
soapalin migrate users.csv mail --type account --retry-failed \
  --from src1 --to dst1
```

## Simulation

L'option globale `--dry-run` se place avant la sous-commande :

```bash
soapalin --dry-run migrate users.csv all --type account --from src1 --to dst1
```

`--json` n'est autorisé qu'avec `--dry-run`.

## Jobs

Les migrations, provisioning et inventaires sont lancés en arrière-plan par
défaut. La commande rend immédiatement la main et affiche l'identifiant du job.

```bash
soapalin job list
soapalin job status 20261110-120305-29c057
soapalin job live
soapalin job stop all
```

Pour exécuter au premier plan :

```bash
soapalin --foreground migrate users.csv mail --type account --from src1 --to dst1
```

Les états et journaux sont dans `data/jobs/<id>/`.

## Options de migration

```text
--differential [YYYY-MM-DD]
--retry-failed [RAPPORT]
--parallelism N          (d?faut : 1, actions independantes)
--keep-tgz [true|false]
--keep-tgz-days N
--mapping-domain CSV
--mapping-host CSV
--resume                 reprendre la derniere copie de boites interrompue
--json
```

L'option globale `--directly-clean` deplace les archives temporaires de chaque
compte vers `data/trash/` apres un import reussi.

## Nettoyage

```bash
soapalin clean --dry-run
soapalin clean mailboxes --older-than 7
soapalin clean all --older-than 30 --keep-last 5
soapalin clean snapshots --keep-last 10
```

`clean` supprime uniquement les donnees temporaires. Il ne deplace rien et ne
demande pas de confirmation. Cibles : `mailboxes` (defaut), `jobs`, `reports`,
`snapshots`, `trash`, `all`. Les jobs actifs et le repertoire de la migration en
cours sont toujours conserves.

## Données

```text
config/config.json       configuration globale
config/servers/          serveurs source/destination
data/snapshots/          inventaires
data/mailboxes/          archives TGZ temporaires
data/reports/            rapports JSON, CSV et sauvegardes
data/jobs/               états et journaux des jobs
data/trash/              archives deplacees par --directly-clean
```

## Tests et qualite

```bash
pytest
pytest --cov=soaplun --cov-report=term-missing
ruff check soaplun tests
mypy
python -m unittest discover -s tests
```
