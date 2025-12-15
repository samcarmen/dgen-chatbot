import google.auth
import google.auth.exceptions
import google.cloud.logging
from google.auth.credentials import AnonymousCredentials
from google.cloud.logging_v2.handlers import StructuredLogHandler, setup_logging


def attach_gcp_logger(
    version: str,
    environment: str | None = None,
):
    """Set up a google-cloud-logging handler and attach it to the Python root logger.

    Args:
        version (str): Semantic release version.
        environment (str | None): Environment tag to add for filtering (e.g., "prod").
    """
    try:
        credentials, project = google.auth.default()
        client = google.cloud.logging.Client(project=project, credentials=credentials)
    except google.auth.exceptions.DefaultCredentialsError:
        client = google.cloud.logging.Client(
            project="local-dev", credentials=AnonymousCredentials()
        )

    handler = StructuredLogHandler(
        project_id=client.project,
        labels={
            "version": version,
            **({"environment": environment} if environment else {}),
        },
    )

    setup_logging(handler)
