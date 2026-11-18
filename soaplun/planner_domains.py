# mypy: disable-error-code="attr-defined"
"""Domain and COS planning methods."""

from .diff import DOMAIN_HANDLED, action, changed_attrs, filtered_attrs


def _alias_target_name(origin, source_domains):
    attrs = origin.get("attrs", {}) or {}
    catch_all = str(attrs.get("zimbraMailCatchAllForwardingAddress", "") or "")
    if catch_all.startswith("@"):
        return catch_all[1:]
    target_id = str(attrs.get("zimbraDomainAliasTargetId", "") or "")
    if target_id:
        for item in source_domains.values():
            if item.get("id") == target_id:
                return item.get("name", "")
    return ""


class DomainPlanMixin:
    def plan_domains(self) -> None:
        self.target_domains = sorted(
            {
                self.mapper.map_domain(name)
                for name in self.source_domains
                if name
                and (
                    self.selected_domain_names is None
                    or name in self.selected_domain_names
                )
            }
        )
        for target in self.target_domains:
            if target.lower() in self.dest_domains:
                continue
            origins = [
                name
                for name in self.source_domains
                if self.mapper.map_domain(name) == target
            ]
            origin = self.source_domains[origins[0]]
            attrs = filtered_attrs(
                self.mapper,
                origin.get("attrs", {}),
                kind="domain",
                handled=DOMAIN_HANDLED,
            )
            domain_type = str(
                origin.get("attrs", {}).get("zimbraDomainType", "")
            ).lower()
            alias_target = _alias_target_name(origin, self.source_domains)
            if domain_type == "alias" or alias_target:
                if not alias_target:
                    self.warnings.append(
                        "Domaine alias %s sans cible identifiable : ignoré."
                        % origin["name"]
                    )
                    continue
                target_mapped = self.mapper.map_domain(alias_target)
                known_dest = target_mapped.lower() in self.dest_domains
                known_plan = target_mapped.lower() in {
                    d.lower() for d in self.target_domains
                }
                if not known_dest and not known_plan:
                    self.warnings.append(
                        "Domaine alias %s ignoré : la cible %s n'existe pas sur la "
                        "destination." % (origin["name"], target_mapped)
                    )
                    continue
                self.actions.append(
                    action(
                        "create_domain_alias",
                        target,
                        "Créer le domaine alias %s → %s"
                        % (target, target_mapped),
                        {
                            "name": target,
                            "alias_target": target_mapped,
                            "source_name": origin["name"],
                        },
                    )
                )
                continue
            self.actions.append(
                action(
                    "create_domain",
                    target,
                    "Créer le domaine %s" % target,
                    {
                        "name": target,
                        "attrs": attrs,
                        "source_name": origin["name"],
                    },
                )
            )

    # -- COS -----------------------------------------------------------
    def plan_cos(self) -> None:
        for source_name, cos in sorted(self.source_cos.items()):
            if (
                self.selected_cos is not None
                and source_name.lower() not in self.selected_cos
            ):
                continue
            target_name = cos["name"]
            if not target_name:
                continue
            if target_name.lower() not in self.dest_cos:
                attrs = filtered_attrs(
                    self.mapper,
                    cos.get("attrs", {}),
                    kind="cos",
                    handled={"cn"},
                )
                self.actions.append(
                    action(
                        "create_cos",
                        target_name,
                        "Créer la classe de service %s" % target_name,
                        {
                            "name": target_name,
                            "attrs": attrs,
                            "source_name": cos["name"],
                        },
                    )
                )
            elif target_name.lower() != "default":
                attrs = changed_attrs(
                    self.mapper,
                    cos.get("attrs", {}),
                    self.dest_cos[target_name.lower()].get("attrs", {}),
                    kind="cos",
                    handled={"cn"},
                )
                if attrs:
                    self.actions.append(
                        action(
                            "modify_cos",
                            target_name,
                            "Mettre à jour la classe de service %s"
                            % target_name,
                            {"name": target_name, "attrs": attrs},
                            risk="high",
                        )
                    )
            elif source_name != "default" and target_name.lower() == "default":
                self.warnings.append(
                    "La COS %s est mappée sur « default » : la COS destination "
                    "n'est pas modifiée par sécurité." % cos["name"]
                )

    # -- Accounts ------------------------------------------------------
