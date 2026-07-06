import os
import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from google.genai.types import HttpOptions
from google.oauth2.credentials import Credentials

load_dotenv(Path(__file__).resolve().parent / ".env")


def get_access_token() -> str:
    """Return a short-lived access token (~1 hour).

    Prefer GOOGLE_ACCESS_TOKEN from .env. If unset, fetch once via gcloud CLI.
    """
    token = os.getenv("GOOGLE_ACCESS_TOKEN", "").strip()
    if token:
        return token

    cmd = ["gcloud", "auth", "print-access-token"]
    if sa := os.getenv("GOOGLE_IMPERSONATE_SERVICE_ACCOUNT", "").strip():
        cmd += ["--impersonate-service-account", sa]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
        )
    except FileNotFoundError:
        sys.exit(
            "未找到 gcloud。请在 .env 中设置 GOOGLE_ACCESS_TOKEN，"
            "或安装并登录 gcloud CLI。"
        )
    except subprocess.CalledProcessError as exc:
        sys.exit(
            "无法获取 access token。请先执行:\n"
            "  gcloud auth login\n"
            "  gcloud auth print-access-token\n"
            f"错误: {exc.stderr.strip() or exc}"
        )

    token = result.stdout.strip()
    if not token:
        sys.exit(
            "无法获取 access token。请先执行:\n"
            "  gcloud auth login\n"
            "  gcloud auth print-access-token"
        )
    return token


def get_short_lived_credentials() -> Credentials:
    """Credentials with access token only; no refresh_token, cannot auto-renew."""
    return Credentials(token=get_access_token())


project = os.getenv("GOOGLE_CLOUD_PROJECT")
if not project:
    sys.exit("请在 .env 中设置 GOOGLE_CLOUD_PROJECT")

client = genai.Client(
    vertexai=True,
    project=project,
    location=os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1"),
    credentials=get_short_lived_credentials(),
    http_options=HttpOptions(
        api_version=os.getenv("GOOGLE_GENAI_API_VERSION", "v1")
    ),
)
response = client.models.generate_content(
    model=os.getenv("GEMINI_MODEL", "gemini-3.5-flash"),
    contents=os.getenv("GEMINI_PROMPT", "How does AI work?"),
)
print(response.text)
