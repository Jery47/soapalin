# AGENTS.md - Instructions de developpement pour OpenCode

Ce document fournit le contexte, l'architecture, les commandes et les regles de
contribution indispensables pour travailler sur **Soap a l'un** (`soapalin`).

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
4. **Compatibilite Python** : Python 3.6+ (3.8+ recommande). Conserver les
   imports `typing` (`List`, `Dict`, `Optional`, `Union`) si necessaire.

---

## 2. Commandes de developpement

```bash
pip install -e .[dev]
pip install -r requirements.txt
pytest
python -m unittest discover -s tests
```

### CLI

```bash
python soapalin.py --help
soapalin --server-action list
soapalin --server-action create --server-type source --server-label src1
soapalin --server-action test --server-label src1
soapalin --server-action inventory --server-label src1
soapalin --create-user --password "Secret!" \
  --server-source src1 --server-destination dst1 --list-user users.csv
soapalin --import-user all \
  --server-source src1 --server-destination dst1 --list-user users.csv
soapalin --diff-user \
  --server-source src1 --server-destination dst1 --list-user users.csv
```

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
    cli.py                    CLI principal
    config.py                 configuration, serveurs, secrets
    connections.py            sessions SOAP, garde lecture seule
    diff.py                   calcul de migration en memoire
    apply.py                  provisioning destination
    mailbox.py                export/import boites (zmmailbox ou REST)
    inventory.py              inventaires JSON
    jobs.py                   jobs CLI en arriere-plan (etat, logs, stop)
    rest.py                   client REST stdlib
    settings.py               reglages, filtres, partages, droits
  vendor/                     bibliotheques Zimbra embarquees
  config/                     configuration locale
  data/                       snapshots, archives, rapports, jobs
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
