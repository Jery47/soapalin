# Migration de l'ancienne CLI vers les sous-commandes

Aucune couche de compatibilité n'est conservée. Les anciens flags sont remplacés
directement.

| Ancien flag / commande | Nouvelle commande |
|---|---|
| `--server-action create --server-type source --server-label src1` | `server add src1 --role source` |
| `--server-action create --server-type destination --server-label dst1` | `server add dst1 --role destination` |
| `--server-action test --server-label src1` | `server test src1` |
| `--server-action inventory --server-label src1` | `server inventory src1` |
| `--server-action list` | `server list` |
| `--server-action show --server-label src1` | `server show src1` |
| `--server-action rename --server-label src1 --new-label prod` | `server rename src1 prod` |
| `--server-action remove --server-label src1` | `server remove src1` |
| `--create-domain CSV` | `provision domain CSV --from SRC --to DST` |
| `--create-cos CSV` / `--import-cos CSV` | `provision cos CSV --from SRC --to DST` |
| `--create-dl CSV` / `--import-dl CSV` | `provision dl CSV --from SRC --to DST` |
| `--create-resources CSV` | `provision resource CSV --from SRC --to DST` |
| `--import-resources CSV` | `provision resource CSV` puis `migrate CSV --type resource ...` |
| `--create-user` | `provision account CSV --from SRC --to DST --password ...` |
| `--password PWD` | `--password PWD` sur `provision account` |
| `--random-password` | `--random-password` sur `provision account` |
| `--adminrights` | `--keep-admin` sur `provision account` |
| `--server-source SRC --server-destination DST` | `--from SRC --to DST` |
| `--list-user CSV` | CSV positionnel de `provision` / `migrate` |
| `--import-user ELEMENTS` | `migrate CSV ELEMENTS --type account --from SRC --to DST` |
| `--import-user all` | `migrate CSV all --type account --from SRC --to DST` |
| `--import-user cos` | `provision cos` (COS retirée des éléments de `migrate`) |
| `--import-user briefcase` | `migrate CSV briefcase --type account ...` |
| `--diff-user` | `migrate CSV ELEMENTS --type account ... --differential` |
| `--since DATE` | `--differential=DATE` |
| `--force-sync-tags` | supprimé ; la resynchronisation des tags est incluse dans `--differential` |
| `--mapping-domain CSV` | conservé sur `provision` et `migrate` |
| `--mapping-host CSV` | conservé sur `provision` et `migrate` |
| `--mapping-cos CSV` | supprimé ; les COS gardent le même nom |
| `--jobs all --jobs-action list` | `job list` |
| `--jobs all --jobs-action status` | `job status all` |
| `--jobs all --jobs-action live` | `job live` |
| `--jobs all --jobs-action stop` | `job stop all` |
| `--jobs ID --jobs-action stop` | `job stop ID` |
| `--dry-run` | option globale avant la sous-commande |
| `--foreground` | conservé, option globale |

## Exemples de conversion

```bash
# Avant
soapalin --create-user --password "Secret!" \
  --server-source src1 --server-destination dst1 --list-user users.csv

# Après
soapalin provision account users.csv \
  --from src1 --to dst1 --password "Secret!"
```

```bash
# Avant
soapalin --import-user mail calendar settings \
  --server-source src1 --server-destination dst1 --list-user users.csv

# Après
soapalin migrate users.csv mail calendar settings \
  --type account --from src1 --to dst1
```

```bash
# Avant
soapalin --diff-user --since 2026-10-01 \
  --server-source src1 --server-destination dst1 --list-user users.csv

# Après
soapalin migrate users.csv mail calendar contacts tasks \
  --type account --from src1 --to dst1 --differential=2026-10-01
```
