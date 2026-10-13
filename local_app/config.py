from dataclasses import dataclass
from pathlib import Path
import os

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Config:
    data_dir: Path
    model: str = ""
    api_url: str = ""
    api_key: str = ""

    @classmethod
    def from_env(cls):
        # Only the public checkout's explicit configuration is loaded.
        from dotenv import load_dotenv
        load_dotenv(ROOT / '.env', override=False)
        return cls(Path(os.getenv('MIRROW_APP_DATA_DIR') or ROOT / 'local-data').resolve(),
            os.getenv('MIRROW_MODEL', '').strip(), os.getenv('MIRROW_API_BASE', '').strip(),
            os.getenv('MIRROW_API_KEY', '').strip())

    @property
    def configured(self):
        return bool(self.model and self.api_url and self.api_key)
