"""
Test the log rotation functionality
"""
import logging
from logging.handlers import RotatingFileHandler
import os
import sys
from pathlib import Path

# Add the project root to Python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.elitea_mcp.main import setup_file_logger

# Set up a test log file in the current directory
log_file = Path('test_rotation.log')
if log_file.exists():
    log_file.unlink()

# Configure root logger
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)

logger = logging.getLogger(__name__)

# Add rotating file handler
print(f"Setting up file logger to: {log_file.absolute()}")
file_handler = setup_file_logger(str(log_file.absolute()), max_bytes=1024, backup_count=3)

# Log some messages
print("Logging messages...")
logger.info("Starting test of log rotation")

# Generate enough content to trigger rotation
for i in range(50):
    logger.info(f"Test log message {i}: " + "X" * 50)

logger.info("Finished test of log rotation")
print("Done logging messages")

# Check the results
print("\nRotation test results:")
print(f"Base log file exists: {log_file.exists()}")
for i in range(1, 4):
    backup = log_file.with_suffix(f"{log_file.suffix}.{i}")
    print(f"Backup {i} exists: {backup.exists()}")

# Clean up
file_handler.close()
