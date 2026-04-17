"""
Integration tests for the EliteA client.

These tests require valid credentials in a .env file.
Copy .env.example to .env and fill in with your actual credentials.
"""

import os
import pytest
import warnings
from pathlib import Path
from dotenv import load_dotenv

from src.elitea_mcp.clients.elitea import Agent, Agents, ApiDetailsRequestError


# Skip integration tests if .env file is not present
ENV_FILE = Path(__file__).parent.parent / ".env"
SKIP_INTEGRATION_TESTS = not ENV_FILE.exists()
SKIP_REASON = "No .env file found. Create a .env file with your credentials to run integration tests."


def setup_module():
    """Load environment variables from .env file."""
    load_dotenv()


@pytest.mark.skipif(SKIP_INTEGRATION_TESTS, reason=SKIP_REASON)
class TestEliteAIntegration:
    """Integration tests for EliteA client."""
    
    @pytest.fixture
    def api_credentials(self):
        """Get API credentials from environment variables."""
        required_vars = ["ELITEA_BASE_URL", "ELITEA_PROJECT_ID", "ELITEA_AUTH_TOKEN"]
        
        # Check if all required variables are set
        missing = [var for var in required_vars if not os.environ.get(var)]
        if missing:
            pytest.skip(f"Missing environment variables: {', '.join(missing)}")
        
        return {
            "base_url": os.environ.get("ELITEA_BASE_URL"),
            "project_id": int(os.environ.get("ELITEA_PROJECT_ID")),
            "auth_token": os.environ.get("ELITEA_AUTH_TOKEN")
        }
    
    @pytest.fixture
    def test_app_ids(self):
        """Get test app IDs from environment variables."""
        app_id = os.environ.get("TEST_APP_ID")
        version_id = os.environ.get("TEST_APP_VERSION_ID")
        
        if not all([app_id, version_id]):
            pytest.skip("TEST_APP_ID or TEST_APP_VERSION_ID not set in environment variables")
        
        return {
            "app_id": int(app_id),
            "version_id": int(version_id)
        }
    
    def test_agents_initialization(self, api_credentials):
        """Test that Agents class can be initialized with real credentials."""
        # Disable SSL warnings for testing
        warnings.filterwarnings("ignore", category=Warning)
        
        try:
            agents = Agents(**api_credentials)
            assert isinstance(agents, Agents)
            
            # Check that agents list is populated
            assert len(agents.agents) > 0, "No agents found"
            
            # Check that each agent is properly initialized
            for agent in agents.agents:
                assert isinstance(agent, Agent)
                assert agent.agent_name is not None
                assert agent.pydantic_model is not None
        except ApiDetailsRequestError as e:
            pytest.fail(f"API request failed: {str(e)}")
    
    def test_agent_initialization(self, api_credentials, test_app_ids):
        """Test that Agent class can be initialized with real credentials."""
        warnings.filterwarnings("ignore", category=Warning)
        
        try:
            agent = Agent(
                **api_credentials,
                app_id=test_app_ids["app_id"],
                version_id=test_app_ids["version_id"]
            )
            
            assert isinstance(agent, Agent)
            assert agent.agent_name is not None
            assert agent.description is not None
            assert agent.pydantic_model is not None
        except ApiDetailsRequestError as e:
            pytest.fail(f"API request failed: {str(e)}")
    
    def test_agent_predict(self, api_credentials, test_app_ids):
        """Test the predict method with a real API call."""
        warnings.filterwarnings("ignore", category=Warning)
        
        try:
            agent = Agent(
                **api_credentials,
                app_id=test_app_ids["app_id"],
                version_id=test_app_ids["version_id"]
            )
            
            # Basic predict call
            response = agent.predict(user_input="Hello, how are you?")
            
            # We can't know the exact response, but we can verify the structure
            assert isinstance(response, dict)
            assert "chat_history" in response
            assert "error" in response
            assert response["error"] is None  # Verify no error occurred
            
            # Verify the chat history contains the user message and a response
            assert len(response["chat_history"]) >= 2
            assert any(msg.get("role") == "user" and msg.get("content") == "Hello, how are you?" 
                       for msg in response["chat_history"])
            
        except ApiDetailsRequestError as e:
            pytest.fail(f"API request failed: {str(e)}")
    
    def test_agent_predict_with_chat_history(self, api_credentials, test_app_ids):
        """Test the predict method with chat history."""
        warnings.filterwarnings("ignore", category=Warning)
        
        try:
            agent = Agent(
                **api_credentials,
                app_id=test_app_ids["app_id"],
                version_id=test_app_ids["version_id"]
            )
            
            # Create chat history
            chat_history = [
                {"role": "user", "content": "Hi there!"},
                {"role": "assistant", "content": "Hello! How can I help you today?"}
            ]
            
            # Predict with chat history
            response = agent.predict(
                user_input="What's your name?",
                chat_history=chat_history
            )
            
            # We can't know the exact response, but we can verify the structure
            assert isinstance(response, dict)
            assert "chat_history" in response
            assert "error" in response
            assert response["error"] is None  # Verify no error occurred
            
            # Verify the chat history contains our original history plus new messages
            assert len(response["chat_history"]) >= 4  # Original 2 messages + new user message + response
            assert any(msg.get("role") == "user" and msg.get("content") == "Hi there!" 
                       for msg in response["chat_history"])
            assert any(msg.get("role") == "assistant" and msg.get("content") == "Hello! How can I help you today?" 
                       for msg in response["chat_history"])
            assert any(msg.get("role") == "user" and msg.get("content") == "What's your name?" 
                       for msg in response["chat_history"])
            
        except ApiDetailsRequestError as e:
            pytest.fail(f"API request failed: {str(e)}")
    
    def test_agent_predict_with_variables(self, api_credentials, test_app_ids):
        """Test the predict method with variables."""
        warnings.filterwarnings("ignore", category=Warning)
        
        try:
            agent = Agent(
                **api_credentials,
                app_id=test_app_ids["app_id"],
                version_id=test_app_ids["version_id"]
            )
            
            # Get the model fields so we know what variables we can use
            model_fields = agent.pydantic_model.model_fields
            
            # Skip test if there are no variables other than user_input
            if len(model_fields) <= 1:
                pytest.skip("No additional variables available to test")
            
            # Find a variable to use for testing (other than user_input)
            test_var_name = next((field for field in model_fields if field != "user_input"), None)
            
            if test_var_name:
                # Create kwargs with variable
                kwargs = {test_var_name: "Test value"}
                
                # Make API call with variable
                response = agent.predict(
                    user_input="Hello with variable",
                    **kwargs
                )
                
                # Verify response
                assert isinstance(response, dict)
                assert "chat_history" in response
                assert "error" in response
                assert response["error"] is None
            
        except ApiDetailsRequestError as e:
            pytest.fail(f"API request failed: {str(e)}")


if __name__ == "__main__":
    """Run tests if script is executed directly."""
    import sys
    
    # Load environment variables
    load_dotenv()
    
    # Check if .env file exists
    if not ENV_FILE.exists():
        print(f"Error: {SKIP_REASON}", file=sys.stderr)
        sys.exit(1)
    
    # Run tests
    pytest.main(["-xvs", __file__])