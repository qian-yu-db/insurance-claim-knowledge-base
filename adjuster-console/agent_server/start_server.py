from pathlib import Path

from dotenv import load_dotenv
from mlflow.genai.agent_server import AgentServer, setup_mlflow_git_based_version_tracking

# Load env vars from .env before importing the agent for proper auth
load_dotenv(dotenv_path=Path(__file__).parent.parent / ".env", override=True)

# Need to import the agent to register the functions with the server
import agent_server.agent  # noqa: E402
from agent_server.data_api import router as data_router  # noqa: E402

agent_server = AgentServer("ResponsesAgent", enable_chat_proxy=True)
# Define the app as a module level variable to enable multiple workers
app = agent_server.app  # noqa: F841
setup_mlflow_git_based_version_tracking()

# Doc/claim/file APIs for the adjuster-console viewer pane
app.include_router(data_router)

# Serve the 2-pane SPA (client/) as static files. Mounted last so /invocations and
# /api/* explicit routes still take precedence over the catch-all static mount.
_client_dir = Path(__file__).parent.parent / "client"
if _client_dir.exists():
    from fastapi.staticfiles import StaticFiles

    app.mount("/", StaticFiles(directory=str(_client_dir), html=True), name="ui")


def main():
    agent_server.run(app_import_string="agent_server.start_server:app")
