"""环境配置：不输出密钥。"""

from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    data_dir: Path = Path("data")
    request_interval: float = Field(default=1.0, ge=0)
    request_attempts: int = Field(default=3, ge=1, le=10)
    request_timeout: float = Field(default=15.0, gt=0)
    tushare_token: SecretStr = SecretStr("")
    # 为空时访问 tushare.pro；填写后改用该地址。
    tushare_http_url: str = ""
    # snapshot 命令写入、--provider tushare 读取的本地快照目录。
    tushare_snapshot_dir: Path = Path("data/tushare_raw")
    llm_api_key: SecretStr = SecretStr("")
    llm_model: str = ""
