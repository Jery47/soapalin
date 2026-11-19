# mypy: disable-error-code="attr-defined"
"""Account planning methods."""
from .diff import (
    ACCOUNT_HANDLED,
    action,
    attr_maps_equal,
    changed_attrs,
    filtered_attrs,
    map_account_address_attrs,
)


class AccountPlanMixin:
    def plan_accounts(self) -> None:
        for account in sorted(
            self.source.get("accounts", []),
            key=lambda a: a.get("name", "").lower(),
        ):
            source_name = account.get("name", "")
            if not source_name:
                continue
            if account.get("is_system"):
                continue
            if (
                self.selected_emails
                and source_name.lower() not in self.selected_emails
            ):
                continue
            if not self.selected_emails and not self.picked.get("all_accounts"):
                continue
            target_name = self.mapper.map_email(source_name)
            key = target_name.lower()
            if key in self.planned_emails:
                self.warnings.append(
                    "Collision de mapping : %s et %s pointent tous deux vers %s ; "
                    "le second est ignoré."
                    % (self.planned_emails[key], source_name, target_name)
                )
                continue
            self.planned_emails[key] = source_name

            cos_name = account.get("cos_name") or ""
            host = self.target_host(account.get("mail_host") or "")
            alias_map = {}
            for alias in account.get("aliases", []):
                mapped_alias = self.mapper.map_email(alias)
                if mapped_alias:
                    alias_map[mapped_alias.lower()] = mapped_alias
            alias_list = sorted(
                alias
                for alias in alias_map.values()
                if alias.lower() != target_name.lower()
            )
            usable_aliases = []
            for alias in alias_list:
                owner = self.dest_alias_owner.get(alias.lower())
                if owner and owner.lower() != key:
                    self.warnings.append(
                        "Alias %s ignoré : déjà utilisé par le compte %s sur la "
                        "destination." % (alias, owner)
                    )
                    continue
                usable_aliases.append(alias)
            alias_list = usable_aliases

            existing = self.dest_accounts.get(key)
            if existing is None and key in self.dest_alias_owner:
                owner = self.dest_alias_owner[key]
                self.warnings.append(
                    "Compte %s non créé : l'adresse cible existe déjà comme alias du "
                    "compte %s sur la destination. Supprimez/renommez cet alias ou "
                    "changez le mapping de domaine." % (target_name, owner)
                )
                self.account_pairs.append(
                    {
                        "source": source_name,
                        "destination": target_name,
                        "status": "conflict",
                        "note": "Adresse cible déjà utilisée comme alias par %s"
                        % owner,
                    }
                )
                continue
            if existing is None:
                self._plan_new_account(
                    account,
                    source_name,
                    target_name,
                    key,
                    cos_name,
                    host,
                    alias_list,
                )
            else:
                self._plan_existing_account(
                    account,
                    source_name,
                    target_name,
                    cos_name,
                    host,
                    existing,
                    alias_list,
                )

            if self.include_mailbox:
                mailbox = account.get("mailbox") or {}
                self.mailboxes.append(
                    {
                        "source": source_name,
                        "destination": target_name,
                        "size": mailbox.get("size"),
                        "messages": mailbox.get("messages"),
                        "destination_status": "new"
                        if existing is None
                        else "existing",
                        "note": ""
                        if mailbox
                        else "Taille inconnue (boîte peut-être jamais ouverte)",
                    }
                )

            self.account_pairs.append(
                {
                    "source": source_name,
                    "destination": target_name,
                    "status": "new" if existing is None else "existing",
                }
            )

    def _plan_new_account(
        self,
        account,
        source_name,
        target_name,
        key,
        cos_name,
        host,
        alias_list,
    ) -> None:
        attrs = filtered_attrs(
            self.mapper,
            account.get("attrs", {}),
            kind="account",
            handled=ACCOUNT_HANDLED,
        )
        attrs, delegated_sender = map_account_address_attrs(
            attrs, self.mapper, self.source_domains
        )
        if host:
            attrs["zimbraMailHost"] = host
        if self.sync.get("force_password_change"):
            attrs["zimbraPasswordMustChange"] = "TRUE"
        payload = {
            "name": target_name,
            "attrs": attrs,
            "cos_name": cos_name,
            "source_name": source_name,
            "password_source": self.password_source,
            "aliases": alias_list,
            "mail_host": host,
            "delegated_sender_addresses": delegated_sender,
        }
        if not self.fixed_password and not self.random_password:
            self.warnings.append(
                "Aucun mot de passe fixe configuré : le compte %s sera créé "
                "sans mot de passe utilisable." % target_name
            )
        self.actions.append(
            action(
                "create_account",
                target_name,
                "Créer le compte %s" % target_name,
                payload,
                risk="normal"
                if (self.fixed_password or self.random_password)
                else "high",
            )
        )
        if cos_name:
            self.actions.append(
                action(
                    "modify_account",
                    target_name,
                    "Appliquer la COS %s a %s" % (cos_name, target_name),
                    {
                        "name": target_name,
                        "attrs": {},
                        "cos_name": cos_name,
                        "source_name": source_name,
                        "after_create": True,
                        "changes": ["COS -> %s" % cos_name],
                    },
                    depends_on=["create_account:%s" % key],
                )
            )
        alias_action_ids = []
        for alias in alias_list:
            alias_action_ids.append("add_alias:%s" % alias.lower())
            self.actions.append(
                action(
                    "add_alias",
                    alias,
                    "Ajouter l'alias %s → %s" % (alias, target_name),
                    {"account": target_name, "alias": alias},
                    depends_on=["create_account:%s" % key],
                )
            )
        if delegated_sender:
            self.actions.append(
                action(
                    "set_delegated_sender",
                    target_name,
                    "Restaurer les adresses d'envoi délégué de %s"
                    % target_name,
                    {"name": target_name, "values": delegated_sender},
                    depends_on=["create_account:%s" % key] + alias_action_ids,
                )
            )

    def _plan_existing_account(
        self,
        account,
        source_name,
        target_name,
        cos_name,
        host,
        existing,
        alias_list,
    ) -> None:
        attrs = changed_attrs(
            self.mapper,
            account.get("attrs", {}),
            existing.get("attrs", {}),
            kind="account",
            handled=ACCOUNT_HANDLED,
        )
        attrs, delegated_sender = map_account_address_attrs(
            attrs, self.mapper, self.source_domains
        )
        if host and not attr_maps_equal(host, existing.get("mail_host") or ""):
            attrs["zimbraMailHost"] = host
        existing_cos = (existing.get("cos_name") or "").lower()
        different_cos = bool(cos_name) and existing_cos != cos_name.lower()
        if attrs or different_cos:
            self.actions.append(
                action(
                    "modify_account",
                    target_name,
                    "Mettre à jour le compte %s" % target_name,
                    {
                        "name": target_name,
                        "id": existing.get("id", ""),
                        "attrs": attrs,
                        "cos_name": cos_name if different_cos else "",
                        "source_name": source_name,
                        "changes": sorted(attrs.keys())
                        + (["COS → %s" % cos_name] if different_cos else []),
                    },
                )
            )
        if self.reset_existing and self.fixed_password:
            self.actions.append(
                action(
                    "set_password",
                    target_name,
                    "Réinitialiser le mot de passe de %s" % target_name,
                    {
                        "name": target_name,
                        "id": existing.get("id", ""),
                        "password_source": "fixed",
                        "must_change": bool(
                            self.sync.get("force_password_change")
                        ),
                    },
                    risk="high",
                )
            )
        existing_aliases = {
            str(alias).lower() for alias in existing.get("aliases", [])
        }
        alias_action_ids = []
        for alias in alias_list:
            if alias.lower() not in existing_aliases:
                alias_action_ids.append("add_alias:%s" % alias.lower())
                self.actions.append(
                    action(
                        "add_alias",
                        alias,
                        "Ajouter l'alias %s → %s" % (alias, target_name),
                        {"account": target_name, "alias": alias},
                    )
                )
        if delegated_sender:
            self.actions.append(
                action(
                    "set_delegated_sender",
                    target_name,
                    "Restaurer les adresses d'envoi délégué de %s"
                    % target_name,
                    {"name": target_name, "values": delegated_sender},
                    depends_on=alias_action_ids,
                )
            )

    # -- Distribution lists -------------------------------------------
