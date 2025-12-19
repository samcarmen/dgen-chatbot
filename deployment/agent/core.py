import argparse
import os
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

import vertexai
from vertexai import agent_engines
from vertexai.preview.reasoning_engines import AdkApp

from src.agent.agent import root_agent


@dataclass
class DeploymentConfig:
    project_id: str
    location: str
    staging_bucket: str
    env_vars: dict[str, str]


def load_config() -> DeploymentConfig:
    required_keys = [
        "GOOGLE_CLOUD_PROJECT",
        "GOOGLE_CLOUD_LOCATION",
        "VERTEX_ENGINE_STAGING_BUCKET",
        "SMTP_HOST",
        "SMTP_PORT",
        "SMTP_USER",
        "SMTP_PASSWORD",
    ]
    missing = [key for key in required_keys if key not in os.environ]
    if missing:
        print(f"Missing required environment variables: {', '.join(missing)}")
        sys.exit(1)

    return DeploymentConfig(
        project_id=os.environ["GOOGLE_CLOUD_PROJECT"],
        location=os.environ["GOOGLE_CLOUD_LOCATION"],
        staging_bucket=os.environ["VERTEX_ENGINE_STAGING_BUCKET"],
        env_vars={
            "SMTP_HOST": os.environ["SMTP_HOST"],
            "SMTP_PORT": os.environ["SMTP_PORT"],
            "SMTP_USER": os.environ["SMTP_USER"],
            "SMTP_PASSWORD": os.environ["SMTP_PASSWORD"],
        },
    )


def get_project_dependencies(
    pyproject_path: Path = Path("pyproject.toml"),
) -> list[str] | None:
    """
    Parses a pyproject.toml file and returns the list of dependencies
    from the [project.dependencies] section.
    """
    pyproject_path = Path(__file__).parent / pyproject_path
    if not pyproject_path.exists():
        print(f"Error: {pyproject_path} not found.")
        return None

    try:
        with open(pyproject_path, "rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        print(f"Error decoding TOML file {pyproject_path}: {e}")
        return None
    except Exception as e:
        print(f"An unexpected error occurred while reading {pyproject_path}: {e}")
        return None

    project_table = data.get("project")
    if not project_table:
        print("Error: [project] table not found in pyproject.toml.")
        return None

    dependencies_list = project_table.get("dependencies")
    if dependencies_list is None:
        print(
            "Warning: 'dependencies' array not found under [project] table or it's empty."
        )
        return []

    if not isinstance(dependencies_list, list):
        print("Error: 'dependencies' under [project] is not a list.")
        return None

    if not all(isinstance(dep, str) for dep in dependencies_list):
        print("Error: Not all items in 'dependencies' list are strings.")
        return None

    return dependencies_list


def find_remote_agent(agent_name: str):
    remote_agents = agent_engines.list()
    for agent in remote_agents:
        if agent.display_name == agent_name:
            return agent
    return None


def create_or_update_agent(adk_app, agent_name, requirements, env_vars):
    """Creates or updates an agent."""
    print(f"--- Creating or updating agent '{agent_name}' ---")
    existing_agent = find_remote_agent(agent_name)

    if existing_agent:
        print(f"Agent '{agent_name}' already exists, updating it...")
        updated_agent = agent_engines.get(existing_agent.name).update(
            agent_engine=adk_app,
            requirements=requirements,
            gcs_dir_name=agent_name,
            env_vars=env_vars,  # pyright: ignore
            extra_packages=[
                "src",
            ],
        )
        print(f"Updated agent: {updated_agent.resource_name}")
    else:
        print(f"Agent '{agent_name}' does not exist, creating it...")
        new_agent = agent_engines.create(
            adk_app,
            display_name=agent_name,
            requirements=requirements,
            gcs_dir_name=agent_name,
            env_vars=env_vars,  # pyright: ignore
            extra_packages=[
                "src",
            ],
        )
        print(f"Created agent: {new_agent.resource_name}")


def delete_agent(agent_name):
    """Deletes an agent."""
    print(f"--- Deleting agent '{agent_name}' ---")
    existing_agent = find_remote_agent(agent_name)

    if not existing_agent:
        print(f"Agent '{agent_name}' does not exist, skipping deletion.")
        return

    print(f"Deleting agent '{agent_name}'...")
    agent_engines.get(existing_agent.name).delete(force=True)
    print(f"Agent '{agent_name}' deleted.")


def main():
    parser = argparse.ArgumentParser(description="Agent Deployment Script")
    parser.add_argument(
        "--action",
        type=str,
        choices=["up", "destroy"],
        help="Action to perform.",
    )
    args = parser.parse_args()

    config = load_config()
    vertexai.init(
        project=config.project_id,
        location=config.location,
        staging_bucket=f"gs://{config.staging_bucket}",
    )

    adk_app = AdkApp(agent=root_agent, enable_tracing=True)
    requirements = get_project_dependencies() or []

    if args.action == "up":
        create_or_update_agent(adk_app, root_agent.name, requirements, config.env_vars)
    elif args.action == "destroy":
        delete_agent(root_agent.name)


if __name__ == "__main__":
    main()
