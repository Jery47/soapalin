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

## Serveurs

```bash
soapalin server add src1 --role source
soapalin server add dst1 --role destination
soapalin server list
soapalin server show src1
soapalin server test src1
soapalin server inventory src1
soapalin server rename src1 zimbra-prod
soapalin server remove src1
```

Les serveurs sont stockés dans `config/servers/<label>.json`. Editer ensuite
ces fichiers pour renseigner les hôtes, ports, comptes admin et mots de passe.

## Provisioning

```bash
soapalin provision domain domains.csv --from src1 --to dst1
soapalin provision cos all --from src1 --to dst1
soapalin provision dl lists.csv --from src1 --to dst1
soapalin provision resource resources.csv --from src1 --to dst1

soapalin provision account users.csv --from src1 --to dst1 --password "Secret!"
soapalin provision account users.csv --from src1 --to dst1 --password-file pwd.txt
soapalin provision account users.csv --from src1 --to dst1 --random-password --keep-admin
```

`all` est accepté à la place du CSV. Les mappings disponibles :

```text
--mapping-domain CSV
--mapping-host CSV
```

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
account  : mail calendar contacts tasks briefcase settings tags filter
           banlist whitelist share sendas sendonbehalfof
resource : mail calendar
dl       : members
```

`--type all` exige le CSV `all`. Les tags manquants sont resynchronisés en mode
différentiel.

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
--parallelism N
--keep-tgz [true|false]
--keep-tgz-days N
--mapping-domain CSV
--mapping-host CSV
--json
```

## Données

```text
config/config.json       configuration globale
config/servers/          serveurs source/destination
data/snapshots/          inventaires
data/mailboxes/          archives TGZ temporaires
data/reports/            rapports JSON, CSV et sauvegardes
data/jobs/               états et journaux des jobs
```

## Tests

```bash
pytest
python -m unittest discover -s tests
```
