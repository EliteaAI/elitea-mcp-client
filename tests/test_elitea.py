import pytest
from unittest.mock import patch, MagicMock
import requests
import json
from src.elitea_mcp.clients.elitea import Agent, Agents, ApiDetailsRequestError

# Fixtures for testing

@pytest.fixture
def mock_agent_response():
    """Mock responses for Agent initialization API calls."""
    class MockResponse:
        def __init__(self, status_code, json_data, text=""):
            self.status_code = status_code
            self._json_data = json_data
            self.text = text
        
        def json(self):
            return self._json_data
    
    app_details = MockResponse(
        200, 
        {"id": 123, "name": "TestAgent", "description": "Test description"}, 
        ""
    )
    
    version_details = MockResponse(
        200,
        {"id": 456, "variables": [{"name": "var1", "value": "default1"}]},
        ""
    )
    
    return {
        "app_details": app_details,
        "version_details": version_details
    }

@pytest.fixture
def mock_agents_response():
    """Mock responses for Agents initialization API calls."""
    class MockResponse:
        def __init__(self, status_code, json_data, text=""):
            self.status_code = status_code
            self._json_data = json_data
            self.text = text
        
        def json(self):
            return self._json_data
    
    tag_response = MockResponse(
        200,
        {"rows": [{"id": "tag1", "name": "mcp"}]},
        ""
    )
    
    apps_response = MockResponse(
        200,
        {"rows": [
            {"id": "app1", "name": "TestApp1"},
            {"id": "app2", "name": "TestApp2"}
        ]},
        ""
    )
    
    version_response = MockResponse(
        200,
        [{"id": "ver1", "latest": True}],
        ""
    )
    
    return {
        "tag_response": tag_response,
        "apps_response": apps_response,
        "version_response": version_response
    }

# Tests for Agent class

class TestAgent:
    """Tests for the Agent class."""

    @patch("requests.get")
    def test_agent_initialization(self, mock_get, mock_agent_response):
        """Test successful Agent initialization."""
        mock_get.side_effect = [
            mock_agent_response["app_details"],
            mock_agent_response["version_details"]
        ]
        
        agent = Agent(
            base_url="http://example.com",
            project_id=123,
            auth_token="test_token",
            app_id=456,
            version_id=789
        )
        
        assert agent.agent_name == "TestAgent"
        assert agent.description == "Test description"
        assert agent.pydantic_model is not None
        
        # Check URLs are constructed correctly
        assert agent.app_predict_url == "http://example.com/api/v2/elitea_core/predict/prompt_lib/"
        assert agent.app_details == "http://example.com/api/v2/elitea_core/application/prompt_lib/"
        
        # Check headers are set correctly
        assert "Authorization" in agent.headers
        assert agent.headers["Authorization"] == "Bearer test_token"
    
    @patch("requests.get")
    def test_agent_initialization_with_extra_headers(self, mock_get, mock_agent_response):
        """Test Agent initialization with extra headers."""
        mock_get.side_effect = [
            mock_agent_response["app_details"],
            mock_agent_response["version_details"]
        ]
        
        agent = Agent(
            base_url="http://example.com",
            project_id=123,
            auth_token="test_token",
            app_id=456,
            version_id=789,
            api_extra_headers={"X-Custom": "Value"}
        )
        
        assert "Authorization" in agent.headers
        assert agent.headers["Authorization"] == "Bearer test_token"
        assert "X-Custom" in agent.headers
        assert agent.headers["X-Custom"] == "Value"
    
    @patch("requests.get")
    def test_agent_get_app_details_error(self, mock_get):
        """Test error handling in _get_app_details method."""
        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_response.text = "Not Found"
        mock_get.return_value = mock_response
        
        with pytest.raises(ApiDetailsRequestError, match="Failed to fetch agent details"):
            Agent(
                base_url="http://example.com",
                project_id=123,
                auth_token="test_token",
                app_id=456,
                version_id=789
            )
    
    @patch("requests.get")
    def test_agent_get_version_details_error(self, mock_get, mock_agent_response):
        """Test error handling in _get_vestion_details method."""
        # First call succeeds for app details, second fails for version details
        mock_response_error = MagicMock()
        mock_response_error.status_code = 500
        mock_response_error.text = "Internal Server Error"
        
        mock_get.side_effect = [
            mock_agent_response["app_details"],  # First call succeeds
            mock_response_error                  # Second call fails
        ]
        
        # We need to patch _create_pydantic_model since it will be called before the error occurs
        with patch.object(Agent, '_create_pydantic_model') as mock_create_model:
            mock_create_model.return_value = None
            
            with pytest.raises(ApiDetailsRequestError, match="Failed to fetch agent details"):
                Agent(
                    base_url="http://example.com",
                    project_id=123,
                    auth_token="test_token",
                    app_id=456,
                    version_id=789
                )
    
    @patch("requests.post")
    def test_agent_predict(self, mock_post):
        """Test the predict method."""
        # Create a minimal Agent instance
        agent = Agent.__new__(Agent)
        agent.app_predict_url = "http://example.com/api/v2/elitea_core/predict/prompt_lib/"
        agent.project_id = 123
        agent.version_id = 789
        agent.headers = {"Authorization": "Bearer test_token"}
        
        # Create a fake pydantic model
        from pydantic import create_model, Field
        agent.pydantic_model = create_model(
            "TestModel",
            var1=(str, Field(description="var1")),
            user_input=(str, Field(description="user input"))
        )
        
        # Mock the API response
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"result": "success"}
        mock_post.return_value = mock_response
        
        # Test predict method
        result = agent.predict(user_input="Hello", var1="test_value")
        
        assert result == {"result": "success"}
        mock_post.assert_called_once()
        
        # Check payload construction
        call_args = mock_post.call_args
        payload = call_args[1]["json"]
        assert "chat_history" in payload
        assert len(payload["chat_history"]) == 0  # No chat history provided, so empty
        assert payload["user_input"] == "Hello"  # user_input is added separately
        
        assert "variables" in payload
        assert len(payload["variables"]) == 1
        assert payload["variables"][0]["name"] == "var1"
        assert payload["variables"][0]["value"] == "test_value"
    
    @patch("requests.post")
    def test_agent_predict_with_chat_history(self, mock_post):
        """Test predict method with chat history."""
        agent = Agent.__new__(Agent)
        agent.app_predict_url = "http://example.com/api/v2/elitea_core/predict/prompt_lib/"
        agent.project_id = 123
        agent.version_id = 789
        agent.headers = {"Authorization": "Bearer test_token"}
        
        # Create a fake pydantic model
        from pydantic import create_model, Field
        agent.pydantic_model = create_model(
            "TestModel",
            user_input=(str, Field(description="user input"))
        )
        
        # Mock the API response
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"result": "success"}
        mock_post.return_value = mock_response
        
        # Test with chat history
        chat_history = [
            {"role": "user", "content": "Previous message"},
            {"role": "assistant", "content": "Previous response"}
        ]
        
        result = agent.predict(user_input="New message", chat_history=chat_history)
        
        assert result == {"result": "success"}
        payload = mock_post.call_args[1]["json"]
        assert len(payload["chat_history"]) == 2  # Only the provided chat_history items
        assert payload["chat_history"][0]["content"] == "Previous message"
        assert payload["chat_history"][1]["content"] == "Previous response"
        assert payload["user_input"] == "New message"  # user_input is separate
    
    @patch("requests.post")
    def test_agent_predict_with_invalid_field(self, mock_post):
        """Test predict method with invalid field in kwargs."""
        agent = Agent.__new__(Agent)
        agent.app_predict_url = "http://example.com/api/v2/elitea_core/predict/prompt_lib/"
        agent.project_id = 123
        agent.version_id = 789
        agent.headers = {"Authorization": "Bearer test_token"}
        
        # Create a fake pydantic model with only a user_input field
        from pydantic import create_model, Field
        agent.pydantic_model = create_model(
            "TestModel",
            user_input=(str, Field(description="user input"))
        )
        
        # Mock the API response
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"result": "success"}
        mock_post.return_value = mock_response
        
        # Test with an invalid field
        with patch("src.elitea_mcp.clients.elitea.logger.warning") as mock_warning:
            result = agent.predict(user_input="Hello", invalid_field="value")
            
            # Check warning was logged
            mock_warning.assert_called_once_with("Key 'invalid_field' is not a valid field in the model.")
            
            # Check payload has no variables since invalid_field was ignored
            payload = mock_post.call_args[1]["json"]
            assert len(payload["variables"]) == 0
    
    @patch("requests.post")
    def test_agent_predict_error(self, mock_post):
        """Test error handling in predict method."""
        agent = Agent.__new__(Agent)
        agent.app_predict_url = "http://example.com/api/v2/elitea_core/predict/prompt_lib/"
        agent.project_id = 123
        agent.version_id = 789
        agent.headers = {"Authorization": "Bearer test_token"}
        
        # Create a fake pydantic model
        from pydantic import create_model, Field
        agent.pydantic_model = create_model(
            "TestModel",
            user_input=(str, Field(description="user input"))
        )
        
        # Mock the API error response
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.text = "Internal Server Error"
        mock_post.return_value = mock_response
        
        with pytest.raises(ApiDetailsRequestError, match="Failed to fetch prediction"):
            agent.predict(user_input="Hello")
    
    def test_create_pydantic_model(self):
        """Test the _create_pydantic_model method."""
        agent = Agent.__new__(Agent)
        agent.agent_name = "TestModel"
        
        # Test with variables having default values
        version_data = {
            "variables": [
                {"name": "var1", "value": "default1"},
                {"name": "var2", "value": "default2"}
            ]
        }
        
        with patch("src.elitea_mcp.clients.elitea.print"):  # Suppress print statement
            model = agent._create_pydantic_model(version_data)
        
        # Check that model fields were created correctly
        assert "var1" in model.model_fields
        assert "var2" in model.model_fields
        assert "user_input" in model.model_fields
        
        # Check default values
        assert model.model_fields["var1"].default == "default1"
        assert model.model_fields["var2"].default == "default2"
        
        # Test with variables not having default values
        version_data = {
            "variables": [
                {"name": "var3", "value": None},
                {"name": "var4"}
            ]
        }
        
        with patch("src.elitea_mcp.clients.elitea.print"):  # Suppress print statement
            model = agent._create_pydantic_model(version_data)
        
        # Check that fields were created with empty default values
        assert "var3" in model.model_fields
        assert "var4" in model.model_fields
        assert model.model_fields["var3"].default == ""
        assert model.model_fields["var4"].default == ""


# Tests for Agents class

class TestAgents:
    """Tests for the Agents class."""

    @patch("src.elitea_mcp.clients.elitea.Agent")
    @patch("requests.get")
    def test_agents_initialization(self, mock_get, mock_agent_class, mock_agents_response):
        """Test successful Agents initialization."""
        # Set up side effects for requests.get calls to handle all potential calls
        mock_get.side_effect = [
            mock_agents_response["tag_response"],       # For _mcp_tag_id
            mock_agents_response["apps_response"],      # For _get_list_of_apps
            mock_agents_response["version_response"],   # For _get_app_version_id (first app)
            mock_agents_response["version_response"]    # For _get_app_version_id (second app)
        ]
        
        # Create mock Agent instances to be returned by the Agent constructor
        mock_agent1 = MagicMock()
        mock_agent2 = MagicMock()
        mock_agent_class.side_effect = [mock_agent1, mock_agent2]
        
        # Initialize Agents
        agents = Agents(
            base_url="http://example.com",
            project_id=123,
            auth_token="test_token"
        )
        
        # Check that URLs were constructed correctly
        assert agents.get_tags == "http://example.com/api/v2/elitea_core/tags/prompt_lib/"
        assert agents.apps_list_url == "http://example.com/api/v2/elitea_core/applications/prompt_lib/"
        
        # Check that agents were created (note: Agents class creates Agent instances internally)
        assert len(agents.agents) == 2  # Two apps in our mock response
        
        # Verify that Agent class was called with correct parameters
        assert mock_agent_class.call_count == 2
        first_call = mock_agent_class.call_args_list[0]
        assert first_call[1]["base_url"] == "http://example.com"
        assert first_call[1]["project_id"] == 123
        assert first_call[1]["auth_token"] == "test_token"
        assert first_call[1]["app_id"] == "app1"
        assert first_call[1]["version_id"] == "ver1"
    
    @patch("src.elitea_mcp.clients.elitea.Agent")
    @patch("requests.get")
    def test_agents_initialization_with_extra_headers(self, mock_get, mock_agent_class, mock_agents_response):
        """Test Agents initialization with extra headers."""
        # Set up side effects for requests.get calls
        mock_get.side_effect = [
            mock_agents_response["tag_response"],
            mock_agents_response["apps_response"],
            mock_agents_response["version_response"],
            mock_agents_response["version_response"]
        ]
        
        # Create mock Agent instances
        mock_agent1 = MagicMock()
        mock_agent2 = MagicMock()
        mock_agent_class.side_effect = [mock_agent1, mock_agent2]
        
        # Initialize Agents with extra headers
        extra_headers = {"X-Custom-Header": "custom-value"}
        agents = Agents(
            base_url="http://example.com",
            project_id=123,
            auth_token="test_token",
            api_extra_headers=extra_headers
        )
        
        # Check that headers include both authorization and custom header
        assert "Authorization" in agents.headers
        assert "X-Custom-Header" in agents.headers
        assert agents.headers["X-Custom-Header"] == "custom-value"
        
        # Verify that Agent instances were created with the extra headers
        assert mock_agent_class.call_count == 2
        first_call = mock_agent_class.call_args_list[0]
        assert first_call[1]["api_extra_headers"] == extra_headers
    
    @patch("requests.get")
    @patch.object(Agent, "_get_app_details")  # Mock Agent's _get_app_details to avoid API calls
    @patch.object(Agent, "_get_vestion_details")  # Mock Agent's _get_vestion_details to avoid API calls
    def test_agents_initialization_with_patched_agent_methods(self, mock_get_version, mock_get_app, mock_get, mock_agents_response):
        """Test successful Agents initialization with Agent methods patched."""
        # Set up side effects for requests.get calls to handle all potential calls
        # We need responses for:
        # 1. _mcp_tag_id call
        # 2. _get_list_of_apps call
        # 3-4. _get_app_version_id calls (one for each app in the list)
        mock_get.side_effect = [
            mock_agents_response["tag_response"],       # For _mcp_tag_id
            mock_agents_response["apps_response"],      # For _get_list_of_apps
            mock_agents_response["version_response"],   # For _get_app_version_id (first app)
            mock_agents_response["version_response"]    # For _get_app_version_id (second app)
        ]
        
        # Create mock Agent instances to be returned by the Agent constructor
        mock_agent1 = MagicMock()
        mock_agent2 = MagicMock()
        Agent.side_effect = [mock_agent1, mock_agent2]
        
        # Initialize Agents
        agents = Agents(
            base_url="http://example.com",
            project_id=123,
            auth_token="test_token"
        )
        
        # Check that URLs were constructed correctly
        assert agents.get_tags == "http://example.com/api/v2/elitea_core/tags/prompt_lib/"
        assert agents.apps_list_url == "http://example.com/api/v2/elitea_core/applications/prompt_lib/"
        
        # Check that agents were created (note: Agents class creates Agent instances internally)
        assert len(agents.agents) == 2  # Two apps in our mock response
    
    @patch("requests.get")
    def test_mcp_tag_id_success(self, mock_get, mock_agents_response):
        """Test _mcp_tag_id method with successful response."""
        mock_get.return_value = mock_agents_response["tag_response"]
        
        agents = Agents.__new__(Agents)
        agents.base_url = "http://example.com"
        agents.api_path = "/api/v2"
        agents.project_id = 123
        agents.headers = {"Authorization": "Bearer test_token"}
        agents.get_tags = "http://example.com/api/v2/elitea_core/tags/prompt_lib/"
        
        tag_id = agents._mcp_tag_id()
        assert tag_id == "tag1"
        
        mock_get.assert_called_once()
        # The URL is passed as the first positional argument, not as a keyword arg
        url = mock_get.call_args[0][0]
        assert "123" in url  # Check project_id is in URL
    
    @patch("requests.get")
    def test_mcp_tag_id_error(self, mock_get):
        """Test error handling in _mcp_tag_id method."""
        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_response.text = "Not Found"
        mock_get.return_value = mock_response
        
        agents = Agents.__new__(Agents)
        agents.base_url = "http://example.com"
        agents.api_path = "/api/v2"
        agents.project_id = 123
        agents.headers = {"Authorization": "Bearer test_token"}
        agents.get_tags = "http://example.com/api/v2/elitea_core/tags/prompt_lib/"
        
        with pytest.raises(ApiDetailsRequestError, match="Failed to fetch agent details"):
            agents._mcp_tag_id()
    
    @patch("requests.get")
    def test_mcp_tag_id_no_mcp_tag(self, mock_get):
        """Test _mcp_tag_id when no 'mcp' tag is found."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"rows": [{"id": "tag1", "name": "not-mcp"}]}
        mock_get.return_value = mock_response
        
        agents = Agents.__new__(Agents)
        agents.base_url = "http://example.com"
        agents.api_path = "/api/v2"
        agents.project_id = 123
        agents.headers = {"Authorization": "Bearer test_token"}
        agents.get_tags = "http://example.com/api/v2/elitea_core/tags/prompt_lib/"
        
        # Should return None if no 'mcp' tag found
        result = agents._mcp_tag_id()
        assert result is None
    
    @patch("requests.get")
    def test_get_app_version_id_with_latest(self, mock_get):
        """Test _get_app_version_id with a 'latest' version available."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = [
            {"id": 1, "latest": False},
            {"id": 2, "latest": True},
            {"id": 3, "latest": False}
        ]
        mock_get.return_value = mock_response
        
        agents = Agents.__new__(Agents)
        agents.headers = {"Authorization": "Bearer test_token"}
        agents.app_versions_list = "http://example.com/api/v2/elitea_core/versions/prompt_lib/"
        agents.project_id = 123
        
        version_id = agents._get_app_version_id("app1")
        assert version_id == 2  # Should return ID of version marked as latest
    
    @patch("requests.get")
    def test_get_app_version_id_no_latest(self, mock_get):
        """Test _get_app_version_id when no 'latest' version is marked."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = [
            {"id": 1, "latest": False},
            {"id": 3, "latest": False},
            {"id": 2, "latest": False}
        ]
        mock_get.return_value = mock_response
        
        agents = Agents.__new__(Agents)
        agents.headers = {"Authorization": "Bearer test_token"}
        agents.app_versions_list = "http://example.com/api/v2/elitea_core/versions/prompt_lib/"
        agents.project_id = 123
        
        version_id = agents._get_app_version_id("app1")
        assert version_id == 3  # Should return highest ID when no latest marked
    
    @patch("requests.get")
    def test_get_app_version_id_error_no_versions(self, mock_get):
        """Test _get_app_version_id error when no versions are found."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = []
        mock_response.text = "Empty list"
        mock_get.return_value = mock_response
        
        agents = Agents.__new__(Agents)
        agents.headers = {"Authorization": "Bearer test_token"}
        agents.app_versions_list = "http://example.com/api/v2/elitea_core/versions/prompt_lib/"
        agents.project_id = 123
        
        with pytest.raises(ApiDetailsRequestError, match="Failed to fetch agent details"):
            agents._get_app_version_id("app1")
    
    @patch("requests.get")
    def test_get_app_version_id_api_error(self, mock_get):
        """Test _get_app_version_id with API error response."""
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.text = "Internal Server Error"
        mock_get.return_value = mock_response
        
        agents = Agents.__new__(Agents)
        agents.headers = {"Authorization": "Bearer test_token"}
        agents.app_versions_list = "http://example.com/api/v2/elitea_core/versions/prompt_lib/"
        agents.project_id = 123
        
        with pytest.raises(ApiDetailsRequestError, match="Failed to fetch agent details"):
            agents._get_app_version_id("app1")
    
    @patch.object(Agents, "_mcp_tag_id")
    @patch.object(Agents, "_get_app_version_id")
    @patch("requests.get")
    @patch("src.elitea_mcp.clients.elitea.Agent")
    def test_get_list_of_apps(self, mock_agent_class, mock_get, mock_get_version, mock_tag_id):
        """Test _get_list_of_apps method."""
        # Setup mocks
        mock_tag_id.return_value = "tag1"
        mock_get_version.return_value = "ver1"
        
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "rows": [
                {"id": "app1", "name": "TestApp1"},
                {"id": "app2", "name": "TestApp2"}
            ]
        }
        mock_get.return_value = mock_response
        
        mock_agent_instances = [MagicMock(), MagicMock()]
        mock_agent_class.side_effect = mock_agent_instances
        
        # Initialize partially and call method
        agents = Agents.__new__(Agents)
        agents.base_url = "http://example.com"
        agents.project_id = 123
        agents.auth_token = "test_token"
        agents.api_extra_headers = None
        agents.apps_list_url = "http://example.com/api/v2/elitea_core/applications/prompt_lib/"
        agents.headers = {"Authorization": "Bearer test_token"}
        agents.agents = []
        
        agents._get_list_of_apps()
        
        # Verify agents were created correctly
        assert len(agents.agents) == 2
        assert mock_agent_class.call_count == 2
        
        # Check first Agent initialization
        first_call = mock_agent_class.call_args_list[0]
        assert first_call[1]["base_url"] == "http://example.com"
        assert first_call[1]["project_id"] == 123
        assert first_call[1]["auth_token"] == "test_token"
        assert first_call[1]["app_id"] == "app1"
        assert first_call[1]["version_id"] == "ver1"
    
    @patch.object(Agents, "_mcp_tag_id")
    def test_get_list_of_apps_no_mcp_tag(self, mock_tag_id):
        """Test _get_list_of_apps with no MCP tag found."""
        mock_tag_id.return_value = None
        
        agents = Agents.__new__(Agents)
        
        with pytest.raises(ApiDetailsRequestError, match="No MCP tag found"):
            agents._get_list_of_apps()
    
    @patch.object(Agents, "_mcp_tag_id")
    @patch("requests.get")
    def test_get_list_of_apps_api_error(self, mock_get, mock_tag_id):
        """Test _get_list_of_apps with API error."""
        mock_tag_id.return_value = "tag1"
        
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.text = "Internal Server Error"
        mock_get.return_value = mock_response
        
        agents = Agents.__new__(Agents)
        agents.apps_list_url = "http://example.com/api/v2/elitea_core/applications/prompt_lib/"
        agents.project_id = 123
        agents.headers = {"Authorization": "Bearer test_token"}
        
        with pytest.raises(ApiDetailsRequestError, match="Failed to fetch agent details"):
            agents._get_list_of_apps()
