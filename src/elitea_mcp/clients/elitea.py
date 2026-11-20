import logging
import requests
from pydantic import create_model, Field
from typing import Dict, List, Any, Optional
import urllib3

from ..utils.name import build_agent_identifier

urllib3.disable_warnings()

logger = logging.getLogger(__name__)

class ApiDetailsRequestError(Exception):
    ...


def _describe_failure(response) -> str:
    """Summarise a failed platform response by status code and error field, keeping the raw body out of user-facing messages."""
    logger.debug("Platform response %s: %s characters", response.status_code, len(response.text))
    try:
        body = response.json()
    except ValueError:
        body = None
    detail = body.get("error") if isinstance(body, dict) else None
    return f"HTTP {response.status_code}: {detail}" if detail else f"HTTP {response.status_code}"


class Agents():
    def __init__(self, 
                 base_url: str,
                 project_id: int,
                 auth_token: str,
                 api_extra_headers: Optional[dict] = None,
                 **kwargs):
        self.base_url = base_url.rstrip('/')
        self.api_path = '/api/v2'
        self.project_id = project_id
        self.auth_token = auth_token
        self.headers = {
            "Authorization": f"Bearer {auth_token}"
        }
        self.api_extra_headers = api_extra_headers
        if api_extra_headers is not None:
            self.headers.update(api_extra_headers)
        self.get_tags = f"{self.base_url}{self.api_path}/elitea_core/tags/prompt_lib/"
        self.apps_list_url = f"{self.base_url}{self.api_path}/elitea_core/applications/prompt_lib/"
        self.app_versions_list = f"{self.base_url}{self.api_path}/elitea_core/versions/prompt_lib/"
        self.exposed_versions_url = f"{self.base_url}{self.api_path}/elitea_core/mcp_exposed_versions/prompt_lib/"
        self.agents = []
        self._get_list_of_apps()

    def _mcp_tag_id(self) -> List[str]:
        url = f"{self.get_tags}{self.project_id}?entity_coverage=all"
        response = requests.get(url, headers=self.headers, verify=False)
        if response.status_code != 200:
            raise ApiDetailsRequestError(f"Failed to fetch agent details: {_describe_failure(response)}")
        for tag in response.json().get("rows", []):
            if tag.get("name") == 'mcp':
                return tag.get("id")
        else:
            return None
    
    def _exposed_versions(self) -> Optional[List[Dict[str, Any]]]:
        """The agent versions the platform exposes over MCP, each with its tool name; None from a platform that does not report them yet."""
        response = requests.get(f"{self.exposed_versions_url}{self.project_id}", headers=self.headers, verify=False)
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise ApiDetailsRequestError(f"Failed to read the agent versions exposed over MCP: {_describe_failure(response)}")
        rows = response.json().get("rows", [])
        if any("tool_name" not in row for row in rows):
            return None
        return rows

    def _get_app_version_id(self, app_id: str) -> List[str]:
        url = f"{self.app_versions_list}{self.project_id}/{app_id}"
        response = requests.get(url, headers=self.headers, verify=False)
        if response.status_code != 200:
            raise ApiDetailsRequestError(f"Failed to fetch agent details: {_describe_failure(response)}")
        latest_version = 0
        for version in response.json():
            if version.get("latest"):
                return version.get("id")
            else:
                if version.get("id") > latest_version:
                    latest_version = version.get("id")
        if latest_version > 0:
            return latest_version
        else:
            raise ApiDetailsRequestError(f"Failed to fetch agent details: application {app_id} has no versions in project {self.project_id}")
        
    def _newest_versions_of_tagged_agents(self, tag_id) -> List[Dict[str, Any]]:
        url = f"{self.apps_list_url}{self.project_id}?limit=100&tags={tag_id}&pipeline_only=0"
        response = requests.get(url, headers=self.headers, verify=False)
        if response.status_code != 200:
            raise ApiDetailsRequestError(f"Failed to fetch agent details: {_describe_failure(response)}")
        return [
            {"application_id": app.get("id"), "version_id": self._get_app_version_id(app.get("id"))}
            for app in response.json().get("rows", [])
        ]

    def _get_list_of_apps(self) -> List[Dict[str, Any]]:
        tag_id = self._mcp_tag_id()
        if not tag_id:
            raise ApiDetailsRequestError(
                f'No agents tagged "mcp" in project {self.project_id}. Add the "mcp" tag to the agent versions you want to expose.'
            )
        exposed = self._exposed_versions()
        if exposed is None:
            logger.warning(
                'This platform does not report which agent versions are tagged "mcp"; '
                "serving the newest version of each tagged agent."
            )
            exposed = self._newest_versions_of_tagged_agents(tag_id)
        logger.debug(f"Agent versions listed: {len(exposed)}")
        for row in exposed:
            self.agents.append(
                Agent(
                    base_url=self.base_url,
                    project_id=self.project_id,
                    auth_token=self.auth_token,
                    app_id=row["application_id"],
                    version_id=row["version_id"],
                    tool_name=row.get("tool_name"),
                    description=row.get("description"),
                    api_extra_headers=self.api_extra_headers
                ))

class Agent():
    def __init__(self, 
                 base_url: str,
                 project_id: int,
                 auth_token: str,
                 app_id: int = None,
                 version_id: int = None,
                 api_extra_headers: Optional[dict] = None,
                 tool_name: Optional[str] = None,
                 description: Optional[str] = None,
                 **kwargs):

        self.base_url = base_url.rstrip('/')
        self.api_path = '/api/v2'
        self.project_id = project_id
        self.version_id = version_id
        self.application_id = app_id
        self.auth_token = auth_token
        self.headers = {
            "Authorization": f"Bearer {auth_token}"
        }
        if api_extra_headers is not None:
            self.headers.update(api_extra_headers)
        
        self.app_predict_url = f"{self.base_url}{self.api_path}/elitea_core/predict/prompt_lib/"
        self.app_details = f"{self.base_url}{self.api_path}/elitea_core/application/prompt_lib/"
        self.application_versions = f"{self.base_url}{self.api_path}/elitea_core/version/prompt_lib/"
        self.agent_name = None
        self.description = None
        self.pydantic_model = None
        self.exposed_tool_name = tool_name
        self._get_app_details()
        if description is not None:
            self.description = description
        self._get_vestion_details()
        
    
    @property
    def tool_name(self) -> str:
        return self.exposed_tool_name or build_agent_identifier(self.agent_name)

    def _create_pydantic_model(self, version_data):
        fields = {}
        for variable in version_data.get("variables", []):
            if variable.get("value"):
                fields[variable['name']] = (str, Field(description=f"Variable {variable['name']}", default=variable.get("value")))
            else:
                fields[variable['name']] = (str, Field(description=f"Variable {variable['name']}", default=""))
        fields["user_input"] = (str, Field(description="User input"))
        # fields["chat_history"] = (List[Dict[str, str]], Field(description="Chat history list of dict [{'user': message}, {'assistant': message}]", default=[]))
        return create_model(self.agent_name, **fields)

    
    def _get_app_details(self) -> Dict[str, Any]:
        url = f"{self.app_details}{self.project_id}/{self.application_id}"
        response = requests.get(url, headers=self.headers, verify=False)
        if response.status_code != 200:
            raise ApiDetailsRequestError(f"Failed to fetch agent details: {_describe_failure(response)}")
        self.agent_name = response.json().get("name")
        self.description = response.json().get("description")
        
        
    def _get_vestion_details(self) -> Dict[str, Any]:
        """
        Fetches the details of the agent.
        
        Returns:
            A dictionary containing the agent details.
        """
        url = f"{self.application_versions}{self.project_id}/{self.application_id}/{self.version_id}"
        response = requests.get(url, headers=self.headers, verify=False)
        if response.status_code != 200:
            raise ApiDetailsRequestError(f"Failed to fetch agent details: {_describe_failure(response)}")
        self.pydantic_model = self._create_pydantic_model(response.json())
        
    
    def predict(self, user_input: str, chat_history: Optional[List[Dict[str, str]]] = None, **kwargs) -> Dict[str, Any]:
        """
        Sends a prediction request to the agent.
        
        Args:
            user_input: The input text to send to the model
            chat_history: Optional list of previous message exchanges
        
        Returns:
            The response from the API
        """
        url = f"{self.app_predict_url}{self.project_id}/{self.version_id}"
        payload = {
            "chat_history": [],
            "variables": []
        }
        
        # Include chat history in payload if provided
        if chat_history:
            payload["chat_history"] = chat_history
        payload['user_input'] = user_input
        
        for key, value in kwargs.items():
            if key in self.pydantic_model.model_fields:
                payload['variables'].append({
                    "name": key,
                    "value": value
                })
            else:
                logger.warning(f"Key '{key}' is not a valid field in the model.")
            
        response = requests.post(url, headers=self.headers, json=payload, verify=False)
        
        if response.status_code != 200:
            raise ApiDetailsRequestError(f"Failed to fetch prediction: {_describe_failure(response)}")
        
        return response.json()