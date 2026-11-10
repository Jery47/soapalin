# REFACTOR_NOTES.md

## 1. Analyse CLI

| Ancien flag / commande | Nouvelle commande | Fonctions metier appelees |
|---|---|---|
| `--server-action create` | `server add LABEL --role ROLE` | `Config.create_server` |
| `--server-action test` | `server test LABEL` | `ZimbraSession.test` |
| `--server-action inventory` | `server inventory LABEL` | `_run_server_inventory` / `collect_snapshot` |
| `--server-action list/show/rename/remove` | `server list/show/rename/remove` | `Config.servers/server/rename_server/remove_server` |
| `--create-domain` | `provision domain CSV|all` | `_run_objects` |
| `--create-cos` / `--import-cos` | `provision cos CSV|all` | `_run_objects` |
| `--create-dl` / `--import-dl` | `provision dl CSV|all` | `_run_objects` |
| `--create-resources` | `provision resource CSV|all` | `_run_objects` |
| `--import-resources` | `provision resource` + `migrate ... --type resource` | `_run_objects`, `copy_mailboxes_*` |
| `--create-user` | `provision account CSV|all` | `_build_plan`, `_run_create` |
| `--import-user` | `migrate CSV ELEMENTS --type account` | `_build_plan`, `_run_import` |
| `--diff-user` / `--since` | `migrate ... --differential[=DATE]` | `_build_plan`, `_run_diff`, `sync_tags` |
| `--retry-failed` | `migrate ... --retry-failed [RAPPORT]` | `_retry_users` |
| `--jobs` / `--jobs-action` | `job list/status/live/stop [ID|all]` | `jobs.JobStore` |
| `--dry-run` | option globale avant la commande | transmis aux handlers |
| `--force-sync-tags` | supprime ; `--differential` resynchronise les tags | `sync_tags` |
| `--mapping-cos` | supprime ; les COS gardent le meme nom | aucun mapping COS |
| `--label` | supprime ; `--from` / `--to` et labels positionnels | config serveurs |
| `--import-user cos` | retire de `migrate` ; utiliser `provision cos` | `_run_objects` |

### Anciens flags non couverts directement

- `--import-user cos` : deplace vers `provision cos`.
- `--import-cos`, `--import-dl`, `--import-distribution-lists`, `--create-resource`, `--import-resource`, `--label` : alias supprimes.
- `--sync-tags`, `--force-sync-tags`, `--since`, `--incremental`, `--diff-user`, sous-commande `diff` : supprimes.
- `--jobs` / `--jobs-action` : remplaces par la sous-commande `job`.
- `--mapping-cos` : supprime.

## 2. Architecture

- `soaplun/cli.py` : parser global, validation, dispatch, helpers metier existants.
- `soaplun/commands/server.py` : handlers serveur.
- `soaplun/commands/provision.py` : handlers provisioning.
- `soaplun/commands/migrate.py` : handler migration/differentiel/retry.
- `soaplun/commands/job.py` : handler de gestion des jobs.
- `soaplun/jobs.py` : etat, lancement, logs et arret des jobs.

## 3. Decisions prises

- Aucune couche de compatibilite : les anciens flags sont supprimes.
- `provision` cree la structure ; `migrate` copie les donnees.
- `--type all` exige un CSV `all` et traite account, puis resource, puis dl.
- Les elements valides mais inapplicables a un type sont ignores avec un message stderr ; une selection entierement inapplicable est une erreur.
- `--differential` est un `nargs="?"` avec `const=True` et type `datetime.date`.
- La date est transmise au backend existant pour les mails et les elements structures (calendar/contacts/tasks) via les requetes de recherche et les exports ; les types sans date applicable (settings, tags, etc.) ne sont pas filtres.
- `--differential` resynchronise les tags via `sync_tags`.
- `--retry-failed` conserve le CSV obligatoire, filtre les echecs par type quand le rapport le fournit, et retombe sur `account` pour les anciens rapports.
- `--parallelism` est disponible sur toutes les commandes `provision` et `migrate`, avec une valeur par defaut de 1.
- `--json` est accepte uniquement avec `--dry-run` et restitue les rapports de simulation collectes.
- Un code de retour `2` signale une reussite partielle (echec ou traitement partiel d'un compte) ; `1` reste une erreur globale.
- `--keep-tgz` et `--keep-tgz-days` gardent le comportement existant.
- Les options globales doivent etre placees avant la sous-commande.
- Les jobs restent en arriere-plan par defaut ; `--foreground` permet l'execution bloquante.

## 4. Aide generee

### `soapalin --help`

```text
usage: soapalin [-h] [--version] [-q] [-v] [--debug] [--dry-run]
                [--foreground]
                {server,provision,migrate,job} ...

Migration Zimbra en sous-commandes.

positional arguments:
  {server,provision,migrate,job}
    server              gerer les serveurs Zimbra
    provision           creer la structure sur la destination
    migrate             copier les donnees vers la destination
    job                 suivre et arreter les jobs en arriere-plan

options:
  -h, --help            show this help message and exit
  --version             show program's version number and exit
  -q, --quiet
  -v, --verbose
  --debug
  --dry-run
  --foreground

Exemples:
  soapalin server add src1 --role source
  soapalin server add dst1 --role destination
  soapalin server list
  soapalin server test src1
  soapalin server inventory src1
  soapalin provision domain domains.csv --from src1 --to dst1
  soapalin provision cos all --from src1 --to dst1
  soapalin provision account users.csv --from src1 --to dst1 --password Secret!
  soapalin provision account users.csv --from src1 --to dst1 --random-password --keep-admin
  soapalin migrate users.csv mail calendar settings --type account --from src1 --to dst1
  soapalin migrate all --type account --from src1 --to dst1
  soapalin migrate resources.csv mail calendar --type resource --from src1 --to dst1
  soapalin migrate lists.csv members --type dl --from src1 --to dst1
  soapalin migrate all --type all --from src1 --to dst1
  soapalin migrate users.csv mail --type account --differential --from src1 --to dst1
  soapalin migrate users.csv mail --type account --differential=2026-10-01 --from src1 --to dst1
  soapalin migrate users.csv mail --type account --retry-failed --from src1 --to dst1
  soapalin --dry-run migrate users.csv all --type account --json --from src1 --to dst1
  soapalin --foreground migrate users.csv mail --type account --from src1 --to dst1
  soapalin job list
  soapalin job status JOB_ID
  soapalin job live
  soapalin job stop all
```

### `soapalin server --help`

```text
usage: soapalin server [-h] ACTION ...

positional arguments:
  ACTION
    add        ajouter un serveur
    test       tester la connexion
    inventory  lancer un inventaire
    rename     renommer un serveur
    list       lister les serveurs
    show       afficher un ou tous les serveurs
    remove     supprimer un serveur

options:
  -h, --help   show this help message and exit

Exemples:
  soapalin server add src1 --role source
  soapalin server list
  soapalin server test src1
  soapalin server inventory src1
  soapalin server inventory src1 --no-stats
```

### `soapalin provision --help`

```text
usage: soapalin provision [-h] TYPE ...

positional arguments:
  TYPE
    domain    creer/mettre a jour les domaines
    cos       creer/mettre a jour les COS
    dl        creer/mettre a jour les listes de distribution
    resource  creer/mettre a jour les ressources
    account   creer les comptes

options:
  -h, --help  show this help message and exit

Exemples:
  soapalin provision domain domains.csv --from src1 --to dst1
  soapalin provision cos all --from src1 --to dst1
  soapalin provision account users.csv --from src1 --to dst1 --password Secret!
```

### `soapalin migrate --help`

```text
usage: soapalin migrate [-h] --from LABEL --to LABEL [--mapping-domain CSV]
                        [--mapping-host CSV] --type {account,resource,dl,all}
                        [--differential [YYYY-MM-DD]]
                        [--retry-failed [RAPPORT]] [--parallelism PARALLELISM]
                        [--keep-tgz [KEEP_TGZ]]
                        [--keep-tgz-days KEEP_TGZ_DAYS] [--json]
                        CSV|all [elements ...]

positional arguments:
  CSV|all
  elements

options:
  -h, --help            show this help message and exit
  --from LABEL
  --to LABEL
  --mapping-domain CSV
  --mapping-host CSV
  --type {account,resource,dl,all}
  --differential [YYYY-MM-DD]
  --retry-failed [RAPPORT]
  --parallelism PARALLELISM
                        nombre de taches en parallele (defaut : 1)
  --keep-tgz [KEEP_TGZ]
  --keep-tgz-days KEEP_TGZ_DAYS
  --json

Exemples:
  soapalin migrate users.csv mail calendar settings --type account --from src1 --to dst1
  soapalin migrate all --type account --from src1 --to dst1
  soapalin migrate resources.csv mail calendar --type resource --from src1 --to dst1
  soapalin migrate lists.csv members --type dl --from src1 --to dst1
  soapalin migrate all --type all --from src1 --to dst1

Attention : --differential accepte une date optionnelle et peut avaler
le CSV positionnel. Ecrivez le CSV avant l'option, ou utilisez
--differential=YYYY-MM-DD.
```

### `soapalin job --help`

```text
usage: soapalin job [-h] ACTION ...

positional arguments:
  ACTION
    list      lister les jobs
    status    afficher le statut
    live      suivre les logs en direct
    stop      arreter un job ou tous les jobs

options:
  -h, --help  show this help message and exit

Exemples:
  soapalin job list
  soapalin job status 20261110-120305-29c057
  soapalin job live
  soapalin job stop all
```
