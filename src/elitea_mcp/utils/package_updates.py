import logging

import requests
from packaging import version

# Configure logger
logger = logging.getLogger(__name__)

package_name = "elitea-mcp"


def check_for_update():
    try:
        response = requests.get(f"https://pypi.org/pypi/{package_name}/json", timeout=15)
        if response.status_code == 200:
            import importlib.metadata
            current_version = importlib.metadata.version(package_name)
            latest_version = response.json()["info"]["version"]
            if version.parse(latest_version) > version.parse(current_version):
                logger.warning(f"New version {latest_version} of {package_name} is available. Current one is {current_version}.")
                #
                return latest_version
    except Exception as e:
        logger.debug(f"Could not check for updates: {e}")
    #
    return None
