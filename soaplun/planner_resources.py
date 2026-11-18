# mypy: disable-error-code="attr-defined"
"""Distribution-list and calendar-resource planning methods."""
from .diff import (
    RESOURCE_HANDLED,
    action,
    changed_attrs,
    dl_attrs,
    map_member,
    resource_attrs,
)


class ResourcePlanMixin:
    def plan_distribution_lists(self) -> None:
        known_source_domains = set(self.source_domains.keys())
        for dl in sorted(
            self.source.get("distribution_lists", []),
            key=lambda d: d.get("name", ""),
        ):
            source_name = dl.get("name", "")
            if (
                self.selected_dls is not None
                and source_name.lower() not in self.selected_dls
            ):
                continue
            if not source_name:
                continue
            target_name = self.mapper.map_email(source_name)
            members = sorted(
                {
                    map_member(self.mapper, member, known_source_domains)
                    for member in dl.get("members", [])
                    if member
                }
            )
            existing = self.dest_dls.get(target_name.lower())
            if existing is None:
                attrs = dl_attrs(
                    self.mapper,
                    dl.get("attrs", {}),
                    dynamic=bool(dl.get("is_dynamic")),
                )
                self.actions.append(
                    action(
                        "create_dl",
                        target_name,
                        "Créer la liste de distribution %s" % target_name,
                        {
                            "name": target_name,
                            "attrs": attrs,
                            "members": members,
                            "source_name": source_name,
                            "dynamic": bool(dl.get("is_dynamic")),
                        },
                    )
                )
            else:
                attrs = changed_attrs(
                    self.mapper,
                    dl_attrs(
                        self.mapper,
                        dl.get("attrs", {}),
                        dynamic=bool(dl.get("is_dynamic")),
                    ),
                    existing.get("attrs", {}),
                    kind="dl",
                    handled=set(),
                )
                if attrs:
                    self.actions.append(
                        action(
                            "modify_dl",
                            target_name,
                            "Mettre à jour la liste %s" % target_name,
                            {
                                "name": target_name,
                                "attrs": attrs,
                                "source_name": source_name,
                            },
                        )
                    )
                missing = sorted(
                    set(members)
                    - {
                        str(m).lower()
                        for m in existing.get("members", [])
                    }
                )
                if missing:
                    self.actions.append(
                        action(
                            "add_dl_member",
                            target_name,
                            "Ajouter %d membre(s) à %s"
                            % (len(missing), target_name),
                            {"name": target_name, "members": missing},
                        )
                    )

    # -- Resources -----------------------------------------------------
    def plan_resources(self) -> None:
        for resource in sorted(
            self.source.get("resources", []),
            key=lambda r: r.get("name", ""),
        ):
            source_name = resource.get("name", "")
            if (
                self.selected_resources is not None
                and source_name.lower() not in self.selected_resources
            ):
                continue
            if not source_name:
                continue
            target_name = self.mapper.map_email(source_name)
            attrs = resource_attrs(self.mapper, resource.get("attrs", {}))
            existing = self.dest_resources.get(target_name.lower())
            if existing is None:
                self.actions.append(
                    action(
                        "create_resource",
                        target_name,
                        "Créer la ressource %s" % target_name,
                        {
                            "name": target_name,
                            "attrs": attrs,
                            "source_name": source_name,
                            "password_source": self.password_source,
                        },
                    )
                )
            else:
                changes = changed_attrs(
                    self.mapper,
                    resource_attrs(self.mapper, resource.get("attrs", {})),
                    existing.get("attrs", {}),
                    kind="resource",
                    handled=RESOURCE_HANDLED,
                )
                if changes:
                    self.actions.append(
                        action(
                            "modify_resource",
                            target_name,
                            "Mettre à jour la ressource %s" % target_name,
                            {
                                "name": target_name,
                                "attrs": changes,
                                "source_name": source_name,
                            },
                        )
                    )

    # -- Warnings ------------------------------------------------------
