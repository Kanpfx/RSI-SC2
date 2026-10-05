"""Project and static resource paths."""
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RESOURCES_ROOT = PROJECT_ROOT / "resources"
KNOWLEDGE_ROOT = RESOURCES_ROOT / "knowledge"
PROMPTS_ROOT = RESOURCES_ROOT / "prompts"
ARES_ROOT = PROJECT_ROOT / "ares-sc2"
