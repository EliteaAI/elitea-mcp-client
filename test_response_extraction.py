#!/usr/bin/env python3
"""
Test script for the new response extraction logic
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

from elitea_mcp.server.mcp import extract_final_response

def test_extract_final_response():
    """Test various response formats"""
    
    # Test case 1: Normal chat_history response
    response1 = {
        'chat_history': [
            {'role': 'user', 'content': 'Check how these context requests are described in Postman'},
            {'role': 'assistant', 'content': 'The search for the specified endpoints in the Postman collection returned no results.'}
        ],
        'error': None,
        'thinking_steps': [],
        'tool_calls': []
    }
    
    result1 = extract_final_response(response1)
    print(f"Test 1 result: {result1}")
    assert result1 == "The search for the specified endpoints in the Postman collection returned no results."
    
    # Test case 2: Complex response with tool calls (from the error message)
    response2 = {
        'chat_history': [
            {'role': 'user', 'content': 'check how these context requests described in postman'},
            {
                'content': 'Could you please provide the specific context or request names you want me to check in Postman?',
                'additional_kwargs': {},
                'response_metadata': {},
                'type': 'ai',
                'name': None,
                'id': None,
                'example': False,
                'tool_calls': [],
                'invalid_tool_calls': [],
                'usage_metadata': None,
                'role': 'assistant'
            }
        ],
        'error': None,
        'thinking_steps': [
            {
                'text': 'Could you please provide the specific context or request names you want me to check in Postman?',
                'generation_info': {'finish_reason': 'stop', 'model_name': 'gpt-4o-2024-11-20'},
                'type': 'ChatGenerationChunk'
            }
        ],
        'tool_calls': [],
        'tool_calls_dict': {}
    }
    
    result2 = extract_final_response(response2)
    print(f"Test 2 result: {result2}")
    assert "Could you please provide the specific context" in result2
    
    # Test case 3: Error response
    response3 = {
        'chat_history': [],
        'error': 'Authentication failed',
        'thinking_steps': [],
        'tool_calls': []
    }
    
    result3 = extract_final_response(response3)
    print(f"Test 3 result: {result3}")
    assert result3 == "Error: Authentication failed"
    
    # Test case 4: Empty response
    response4 = {
        'chat_history': [],
        'error': None,
        'thinking_steps': [],
        'tool_calls': []
    }
    
    result4 = extract_final_response(response4)
    print(f"Test 4 result: {result4}")
    assert result4 == "No response content found"
    
    print("All tests passed!")

if __name__ == "__main__":
    test_extract_final_response()
