import time
from pathlib import Path

import pulumi
import pulumi_gcp as gcp

PROJECT_NAME = "entrypoint"
ARCHIVE_DIR = Path(__file__).resolve().parents[1]
LOCATION_ABBREVIATIONS = {"asia-southeast1": "sg"}


def get_location_abbreviation(region: str) -> str:
    return LOCATION_ABBREVIATIONS[region]


def get_location_suffix(region: str) -> str:
    return f"_{get_location_abbreviation(region)}"


def provision_service_account(service_account: dict, project_id: str):
    """Create the service account and attach IAM roles."""
    account_alias = service_account["account_id"].replace("-", "_")
    created_service_account = gcp.serviceaccount.Account(
        resource_name=f"account_{account_alias}",
        account_id=service_account["account_id"],
        display_name=service_account["display_name"],
        description=service_account["description"],
        project=project_id,
    )

    for role in service_account["roles"]:
        role_alias = role.split("/")[-1].replace(".", "_")
        _ = gcp.projects.IAMMember(
            resource_name=f"iammember_{account_alias}_{role_alias}",
            member=created_service_account.member,
            project=project_id,
            role=role,
        )

    return created_service_account


def provision_artifact_registry(
    artifact_registry: dict, component_label: str
) -> dict[str, gcp.artifactregistry.Repository]:
    """Create Artifact Registry repositories across locations."""
    artifact_registry_mapping: dict[str, gcp.artifactregistry.Repository] = {}

    for location in artifact_registry["locations"]:
        artifact_registry_alias = artifact_registry["repository_id"].replace("-", "_")
        created_repository = gcp.artifactregistry.Repository(
            resource_name=f"artifactregistry_{artifact_registry_alias}_{location}",
            cleanup_policy_dry_run=artifact_registry["cleanup_policy_dry_run"],
            cleanup_policies=artifact_registry["cleanup_policies"],
            description=artifact_registry["description"],
            format=artifact_registry["format"],
            labels={"component": component_label},
            location=location,
            repository_id=artifact_registry["repository_id"],
        )
        artifact_registry_mapping[location] = created_repository

    return artifact_registry_mapping


def upload_source_archive(bucket: str, version: str):
    """Zip and upload the function source to GCS, returning the object."""
    file_archive = pulumi.FileArchive(path=str(ARCHIVE_DIR / "temp"))
    obj_version = f"{version}_{int(time.time())}"

    return gcp.storage.BucketObject(
        resource_name=f"storage_bucketobject_{PROJECT_NAME}_zip",
        bucket=bucket,
        source=file_archive,
        name=f"{PROJECT_NAME}/{PROJECT_NAME}_{obj_version}.zip",
    )


def provision_cloud_functions(
    cloud_functions: list[dict],
    gcp_config: pulumi.Config,
    input_config: pulumi.Config,
    label_config: pulumi.Config,
    artifact_registry_mapping: dict[str, gcp.artifactregistry.Repository],
    created_bucket_object: gcp.storage.BucketObject,
    created_service_account: gcp.serviceaccount.Account,
):
    """Create Cloud Functions and expose them via Cloud Run IAM bindings."""
    all_locations = {
        location for cf in cloud_functions for location in cf.get("locations", [])
    }
    cloud_function_mapping = {location: {} for location in all_locations}

    for cloud_function in cloud_functions:
        for location in cloud_function.get("locations", []):
            created_cloud_function = gcp.cloudfunctionsv2.Function(
                resource_name=f"cloudfunctionv2_{cloud_function['name']}_{location}",
                name=cloud_function["name"],
                description=cloud_function["description"],
                labels={
                    "component": label_config.require("component"),
                    "version": input_config.require("version").replace(".", "-"),
                },
                location=location,
                build_config=gcp.cloudfunctionsv2.FunctionBuildConfigArgs(
                    runtime=cloud_function["runtime"],
                    entry_point=cloud_function["entry_point"],
                    docker_repository=pulumi.Output.all(
                        project=gcp_config.require("project"),
                        location=location,
                        repository=artifact_registry_mapping[location].repository_id,
                    ).apply(
                        lambda args: f"projects/{args['project']}/locations/{args['location']}/repositories/{args['repository']}"
                    ),
                    source=gcp.cloudfunctionsv2.FunctionBuildConfigSourceArgs(
                        storage_source=gcp.cloudfunctionsv2.FunctionBuildConfigSourceStorageSourceArgs(
                            bucket=input_config.require("bucket"),
                            object=created_bucket_object.name,
                        ),
                    ),
                ),
                service_config=gcp.cloudfunctionsv2.FunctionServiceConfigArgs(
                    min_instance_count=cloud_function["min_instance_count"],
                    max_instance_count=cloud_function["max_instance_count"],
                    secret_environment_variables=[
                        gcp.cloudfunctionsv2.FunctionServiceConfigSecretEnvironmentVariableArgs(
                            key=variable["key"],
                            project_id=gcp_config.require("project"),
                            secret=variable["secret"].format(
                                region_abbr=get_location_abbreviation(location).upper()
                            ),
                            version=variable["version"],
                        )
                        for variable in cloud_function.get(
                            "secret_environment_variables", []
                        )
                    ],
                    available_cpu=cloud_function["available_cpu"],
                    available_memory=str(cloud_function["available_memory_mb"]),
                    timeout_seconds=cloud_function["timeout"],
                    environment_variables={
                        "COMPONENT": label_config.require("component"),
                        "ENVIRONMENT": input_config.require("environment"),
                        "GCP_PROJECT_ID": gcp_config.require("project"),
                        "GCP_REGION": location,
                        "GCP_REGION_ABBREV": get_location_abbreviation(location),
                        "GCP_REGION_SUFFIX": get_location_suffix(location),
                        "VERSION": input_config.require("version"),
                        **cloud_function["environment_variables"],
                    },
                    all_traffic_on_latest_revision=True,
                    service_account_email=created_service_account.email,
                ),
            )

            cloud_function_mapping[location][cloud_function["name"]] = (
                created_cloud_function
            )

            cloud_run_service_name = created_cloud_function.name.apply(
                lambda n: n.replace("_", "-")
            )

            _ = gcp.cloudrun.IamBinding(
                resource_name=f"cloudrun_iam_{cloud_function['name']}_{location}",
                project=gcp_config.require("project"),
                location=location,
                service=cloud_run_service_name,
                role="roles/run.invoker",
                members=["allUsers"],
                opts=pulumi.ResourceOptions(depends_on=[created_cloud_function]),
            )

    return cloud_function_mapping


def main() -> None:
    gcp_config = pulumi.Config("gcp")
    input_config = pulumi.Config("input")
    label_config = pulumi.Config("label")
    resource = input_config.require_object("resource")

    created_service_account = provision_service_account(
        resource["service_account"], gcp_config.require("project")
    )
    artifact_registry_mapping = provision_artifact_registry(
        resource["artifact_registry"], label_config.require("component")
    )
    created_bucket_object = upload_source_archive(
        input_config.require("bucket"), input_config.require("version")
    )
    _ = provision_cloud_functions(
        resource["cloud_functions"],
        gcp_config,
        input_config,
        label_config,
        artifact_registry_mapping,
        created_bucket_object,
        created_service_account,
    )


if __name__ == "__main__":
    main()
