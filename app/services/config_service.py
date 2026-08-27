import yaml
import logging

logger = logging.getLogger(__name__)

class ConfigService:
    @staticmethod
    def parse_ghost_yml(yaml_content: str) -> dict:
        """
        Parses the ghost.yml file and returns a configuration dictionary with defaults.
        """
        default_config = {
            "trigger_keyword": "bot/reproduce",
            "timeout": 60,
            "max_retries": 3,
            "allowed_base_images": [],
            "resource_limits": {
                "cpus": 1024,
                "memory": "2g"
            }
        }
        
        if not yaml_content:
            return default_config
            
        try:
            parsed = yaml.safe_load(yaml_content)
            if not parsed or "ghost" not in parsed:
                return default_config
                
            user_config = parsed["ghost"]
            
            # Deep update defaults
            for key, value in user_config.items():
                if key == "resource_limits" and isinstance(value, dict):
                    default_config["resource_limits"].update(value)
                else:
                    default_config[key] = value
                    
            return default_config
        except Exception as e:
            logger.error(f"Failed to parse ghost.yml: {e}")
            return default_config
            
    @staticmethod
    def enforce_security(config: dict, repro_context):
        """
        Enforces prompt injection constraints against the LLM's requested reproduction context.
        Raises ValueError if a security constraint is violated.
        """
        allowed_images = config.get("allowed_base_images", [])
        
        # Enforce base image allowlist (only if the list is not empty)
        if allowed_images and repro_context.base_image not in allowed_images:
            logger.warning(f"SECURITY BLOCK: Requested image {repro_context.base_image} is not in ghost.yml allowlist.")
            raise ValueError(f"SECURITY BLOCK: The requested base image '{repro_context.base_image}' is not allowed by this repository's ghost.yml configuration.")
            
        return True
