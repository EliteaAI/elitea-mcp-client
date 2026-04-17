import logging
import requests
from pydantic import create_model, Field
from typing import Dict, List, Any, Optional
import urllib3
urllib3.disable_warnings()

logger = logging.getLogger(__name__)

class ApiDetailsRequestError(Exception):
    ...


class Agents():
    def __init__(self, 
                 base_url: str,
                 project_id: int,
                 auth_token: str,
                 api_extra_headers: Optional[dict] = None,
                 **kwargs):
        self.base_url = base_url.rstrip('/')
        self.api_path = '/api/v1'
        self.project_id = project_id
        self.auth_token = auth_token
        self.headers = {
            "Authorization": f"Bearer {auth_token}"
        }
        self.api_extra_headers = api_extra_headers
        if api_extra_headers is not None:
            self.headers.update(api_extra_headers)
        self.get_tags = f"{self.base_url}{self.api_path}/promptlib_shared/tags/prompt_lib/"
        self.get_tags_1_6 = f"{self.base_url}{self.api_path}/prompt_lib/tags/prompt_lib/"
        self.apps_list_url = f"{self.base_url}{self.api_path}/applications/applications/prompt_lib/"
        self.app_versions_list = f"{self.base_url}{self.api_path}/applications/versions/prompt_lib/"
        self.agents = []
        self._get_list_of_apps()

    def _mcp_tag_id(self) -> List[str]:
        url = f"{self.get_tags}{self.project_id}?entity_coverage=all"
        response = requests.get(url, headers=self.headers, verify=False)
        if response.status_code == 404:
            # backward compatibility for versions < 1.7
            url = f"{self.get_tags_1_6}{self.project_id}?entity_coverage=all"
            response = requests.get(url, headers=self.headers, verify=False)
        if response.status_code != 200:
            raise ApiDetailsRequestError(f"Failed to fetch agent details: {response.text}")
        for tag in response.json().get("rows", []):
            if tag.get("name") == 'mcp':
                return tag.get("id")
        else:
            return None
    
    def _get_app_version_id(self, app_id: str) -> List[str]:
        url = f"{self.app_versions_list}{self.project_id}/{app_id}"
        response = requests.get(url, headers=self.headers, verify=False)
        if response.status_code != 200:
            raise ApiDetailsRequestError(f"Failed to fetch agent details: {response.text}")
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
            raise ApiDetailsRequestError(f"Failed to fetch agent details: {response.text}")
        
    def _get_list_of_apps(self) -> List[Dict[str, Any]]:
        tag_id = self._mcp_tag_id()
        if not tag_id:
            raise ApiDetailsRequestError("No MCP tag found")
        url = f"{self.apps_list_url}{self.project_id}?limit=100&tags={tag_id}&pipeline_only=0"
        response = requests.get(url, headers=self.headers, verify=False)
        if response.status_code != 200:
            raise ApiDetailsRequestError(f"Failed to fetch agent details: {response.text}")
        logger.debug(f"Response: {response.json()}")
        for app in response.json().get("rows", []):
            app_version =self._get_app_version_id(app.get("id"))
            self.agents.append(
                Agent(
                    base_url=self.base_url,
                    project_id=self.project_id,
                    auth_token=self.auth_token,
                    app_id=app.get("id"),
                    version_id=app_version,
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
                 **kwargs):

        self.base_url = base_url.rstrip('/')
        self.api_path = '/api/v1'
        self.project_id = project_id
        self.version_id = version_id
        self.application_id = app_id
        self.auth_token = auth_token
        self.headers = {
            "Authorization": f"Bearer {auth_token}"
        }
        if api_extra_headers is not None:
            self.headers.update(api_extra_headers)
        
        self.app_predict_url = f"{self.base_url}{self.api_path}/applications/predict/prompt_lib/"
        self.app_details = f"{self.base_url}{self.api_path}/applications/application/prompt_lib/"
        self.application_versions = f"{self.base_url}{self.api_path}/applications/version/prompt_lib/"
        self.agent_name = None
        self.description = None
        self.pydantic_model = None
        self._get_app_details()
        self._get_vestion_details()
        
    
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
            raise ApiDetailsRequestError(f"Failed to fetch agent details: {response.text}")
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
        self.pydantic_model = self._create_pydantic_model(response.json())
        if response.status_code != 200:
            raise ApiDetailsRequestError(f"Failed to fetch agent details: {response.text}")
        
    
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
            raise ApiDetailsRequestError(f"Failed to fetch prediction: {response.text}")
        
        return response.json()