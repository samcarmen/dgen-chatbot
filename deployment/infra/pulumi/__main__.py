import pulumi
import pulumi_gcp as gcp


def provision_buckets(cloud_storage: dict, project_id: str) -> None:
    bucket_alias = cloud_storage["name"].replace("-", "_")
    for location in cloud_storage["locations"]:
        _ = gcp.storage.Bucket(
            resource_name=f"bucket_{bucket_alias}_{location}",
            lifecycle_rules=cloud_storage["lifecycle_rule"],
            location=location,
            name=cloud_storage["name"],
            project=project_id,
            public_access_prevention=cloud_storage["public_access_prevention"],
            storage_class=cloud_storage["storage_class"],
            uniform_bucket_level_access=cloud_storage["uniform_bucket_level_access"],
            force_destroy=cloud_storage["force_destroy"],
        )


def provision_firestore(firestore: dict, project_id: str) -> None:
    _ = gcp.firestore.Database(
        resource_name=f"database_{firestore['name']}",
        project=project_id,
        name=firestore["name"],
        location_id=firestore["region"],
        type=firestore["type"],
        concurrency_mode=firestore["concurrency_mode"],
        app_engine_integration_mode=firestore["app_engine_integration_mode"],
        point_in_time_recovery_enablement=firestore[
            "point_in_time_recovery_enablement"
        ],
        delete_protection_state=firestore["delete_protection_state"],
        deletion_policy=firestore["deletion_policy"],
    )


def main() -> None:
    gcp_config = pulumi.Config("gcp")
    input_config = pulumi.Config("input")
    resource = input_config.require_object("resource")

    project_id = gcp_config.require("project")
    provision_buckets(resource["cloud_storage"], project_id)
    provision_firestore(resource["firestore"], project_id)


if __name__ == "__main__":
    main()
