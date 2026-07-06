"""应用配置 —— 仅从 .env 文件读取"""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # LLM
    deepseek_api_key: str
    deepseek_base_url: str = "https://api.deepseek.com/v1"
    model: str = "deepseek-chat"

    # 搜索
    bocha_api_key: str = ""

    # 工作流
    max_iterations: int = 2

    # 记忆
    enable_memory: bool = True
    db_path: str = "data/memory.db"

    # 服务
    host: str = "0.0.0.0"
    port: int = 8765

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
