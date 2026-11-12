# AGENTS.md - Instructions de developpement pour OpenCode

Ce document fournit le contexte, l'architecture, les commandes et les regles de
contribution pour travailler sur **Soap a l'un** (`soapalin`).

---

## 1. Vue d'ensemble

**Soap a l'un** est un outil CLI de duplication, provisioning et migration de
serveurs Zimbra vers une destination.

### Invariants fondamentaux

1. **Source en lecture seule absolue** : aucune commande SOAP mutante sur la
   source. Chaque appel est valide par `ReadOnlyViolation`.
2. **Destination additive uniquement** : aucune suppression sur la destination.
   `resolve=skip` est toujours applique aux archives de boites.
3. **Multi-serveurs decouple** : chaque serveur a son fichier
   `config/servers/<label>.json`.
4. **Compatibilite Python** : Python 3.8+.

---

## 2. Commandes de developpement

```bash
pip install -e .[dev]
pip install -r requirements.txt
pytest
pytest --cov=soaplun --cov-report=term-missing
ruff check soaplun tests
mypy
python -m unittest discover -s tests
```

### CLI

```bash
python soapalin.py --help
soapalin server list
soapalin server add src1 --role source
soapalin server test src1
soapalin server inventory src1
soapalin provision account users.csv --from src1 --to dst1 --password "Secret!"
soapalin migrate users.csv mail calendar settings --type account --from src1 --to dst1
soapalin job list
soapalin clean --dry-run
soapalin clean mailboxes --older-than 7
```

Les options globales (`--dry-run`, `--foreground`, `--directly-clean`, `-q`,
`-v`, `--debug`) doivent etre placees avant la sous-commande.

### Verification obligatoire du help

- Toujours executer `python soapalin.py -h` **et** le help de la sous-commande
  concernee (`python soapalin.py migrate -h`, etc.) avant de modifier ou de
  documenter la CLI.
- Le help est la reference de l'interface : ne pas supposer qu'une option ou
  qu'un element s'applique a un type.
- `--type` definit le type d'objet migre. Les elements doivent appartenir a ce
  type :
  - `account` : `mail calendar contacts tasks briefcase cos folder color settings tags filter banlist whitelist share sendas sendonbehalfof`
  - `resource` : `mail calendar`
  - `dl` : `members` uniquement (une DL n'a ni dossier, ni settings, ni partage)
  - `all` : combinaison account + resource + dl, avec CSV `all`
- Verifier le help apres toute modification du parser ou de la validation.

---

## 3. Architecture

```text
Soap_a_lun/
  soapalin                    wrapper shell CLI
  soapalin.cmd                wrapper batch Windows CLI
  soapalin.py                 point d'entree Python CLI
  pyproject.toml              packaging et entry point
  pytest.ini                  configuration pytest
  install.sh / uninstall.sh   installation Linux
  install.ps1 / uninstall.ps1 installation Windows
  soaplun/
    cli.py                    parser global, validation, dispatch
    commands/
      server.py               sous-commandes serveur
      provision.py            sous-commandes provisioning
      migrate.py              migration et differentiel
      job.py                  gestion des jobs
      clean.py                suppression des donnees temporaires
    config.py                 configuration, serveurs, secrets
    connections.py            sessions SOAP, garde lecture seule
    diff.py                   comparaisons et normalisations
    planner.py                construction des plans de migration
    apply.py                  provisioning destination
    pipeline.py               orchestration provision/migrate
    runner.py                 orchestration partagee CLI
    context.py                RunContext (options, cancel, rapports)
    mailbox.py                orchestration des copies de boites
    mailbox_common.py         constantes/erreurs communes boites
    mailbox_archive.py        backend zmmailbox/REST, export/import
    inventory.py              collecteurs d'inventaire
    inventory_store.py        persistance/index/cache des snapshots
    jobs.py                   jobs en arriere-plan (etat, logs, stop)
    cleanup.py                nettoyage manuel et deplacement vers trash
    rest.py                   client REST stdlib
    settings.py               orchestration des reglages
    settings_common.py        constantes/helpers communs reglages
    settings_copy.py          signatures, prefs, dossiers, filtres, partages
    storage.py                ecritures atomiques JSON/bytes
    models.py                 contrats TypedDict (Plan, Snapshot, ...)
    inputs.py                 lecture CSV et selection d'elements
    mapping.py                mapping domaines/emails/hotes
    parallel.py               helper de parallelisation ordonnee
    schema.py                 validation des snapshots/plans
  vendor/                     bibliotheques Zimbra embarquees
  config/                     configuration locale
  data/                       snapshots, archives, rapports, jobs, trash
  tests/                      tests unitaires
```

Il n'y a plus d'interface web.

---

## 4. Regles de codage

### Dependances

- Privilegier la stdlib (`urllib`, `http.client`, `json`, `pathlib`).
- Ne pas ajouter de dependance lourde sans justification.

### Ecritures atomiques

- Toute persistance d'etat doit utiliser `tempfile.mkstemp` + `os.replace`.

### Journalisation

- Utiliser `logging`.
- Dans `soaplun/cli.py`, utiliser `_out()` pour respecter `--quiet`.
- Prise en charge de `--verbose` / `--debug`.

### Tests

- Tout ajout ou modification de logique metier doit avoir un test.
- Aucun test ne doit dependre d'un serveur Zimbra reel.
- `pytest` doit passer integralement.
