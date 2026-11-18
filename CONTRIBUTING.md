# Contribuer à soapalin

## Prérequis

- Python 3.8+
- Git
- Un environnement virtuel local

## Installation

```bash
python -m venv .venv
. .venv/bin/activate        # Linux/macOS
.venv\Scripts\Activate.ps1  # Windows PowerShell
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

## Vérifications obligatoires

Avant toute pull request :

```bash
pytest
ruff check soaplun tests
mypy soaplun
```

Pour toute modification de la CLI :

```bash
python soapalin.py -h
python soapalin.py migrate -h
python soapalin.py provision -h
```

## Règles de développement

- La source Zimbra est toujours en lecture seule.
- La destination est additive : aucun nettoyage implicite de comptes ou de données.
- Ne pas dépendre d'un serveur Zimbra réel dans les tests.
- Ajouter ou adapter un test pour toute modification de logique métier.
- Documenter les changements d'interface dans `README.md`, `AGENTS.md` et les aides CLI concernées.

## Pull requests

- Une PR doit rester ciblée sur un sujet.
- Décrire le besoin, la solution et les vérifications effectuées.
- Indiquer les limites ou changements de comportement.
- Ne pas inclure de fichiers de données locaux (`*.csv`, snapshots, rapports, journaux).
