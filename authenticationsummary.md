# Vertex AI API 鉴权方式大全

本文档基于 `google-genai` Python SDK 与 Vertex AI 的实际测试与研究整理，涵盖本地开发到生产部署的常见鉴权路径。核心结论先行：

> **ADC（Application Default Credentials）不是某一种具体文件，而是一套「凭证发现链」**——SDK 按固定顺序在环境中寻找可用凭证。JSON 文件只是本地开发链路中的一种来源；生产环境通常**没有 JSON、无人登录**，由平台元数据服务器自动签发短期 token。

---

## 目录

1. [ADC 凭证发现链（总览）](#1-adc-凭证发现链总览)
2. [用户 ADC JSON（application-default login）](#2-用户-adc-jsonapplication-default-login)
3. [服务账号密钥 JSON（SA Key）](#3-服务账号密钥-jsonsa-key)
4. [短期 Access Token（gcloud CLI / .env）](#4-短期-access-tokengcloud-cli--env)
5. [SDK 隐式 ADC（不传 credentials）](#5-sdk-隐式-adc不传-credentials)
6. [元数据服务器（GCE / GKE / Cloud Run）](#6-元数据服务器gce--gke--cloud-run)
7. [Workload Identity Federation（CI/CD）](#7-workload-identity-federationcicd)
8. [服务账号模拟（Service Account Impersonation）](#8-服务账号模拟service-account-impersonation)
9. [方式对比与场景选型](#9-方式对比与场景选型)
10. [安全事件响应清单](#10-安全事件响应清单)
11. [官方参考文档](#11-官方参考文档)

---

## 1. ADC 凭证发现链（总览）

### 底层工作原理

当 Python/Java/Go SDK 调用 `google.auth.default()` 或 `genai.Client()` 且**未显式传入 `credentials`** 时，会按顺序查找凭证：

```
1. GOOGLE_APPLICATION_CREDENTIALS 环境变量指向的文件
2. ~/.config/gcloud/application_default_credentials.json（用户 ADC）
3. GCE/GKE/Cloud Run 元数据服务器（附加的服务账号）
4. Cloud SDK 默认用户凭证（gcloud login 缓存）
5. Workload Identity Federation（外部身份联邦）
```

找到第一个可用来源后，SDK 用其中的长期凭证（`refresh_token` 或 `private_key`）向 Google OAuth 换取**短期 access token**（约 1 小时），再以 `Authorization: Bearer <token>` 调用 Vertex AI API。

### 安全风险来源

- 混淆「ADC」与「某个 JSON 文件」——以为没有 JSON 就没有长期凭证。
- 本地开发留下的用户 ADC / SA Key 被误提交 Git、打进 Docker 镜像或分享给他人。

### 最佳实践

- 把 ADC 理解为**发现机制**，不是**存储机制**。
- 本地：尽量不让凭证文件出现在项目目录。
- 生产：走元数据服务器或 Workload Identity，**零 JSON 落地**。

---

## 2. 用户 ADC JSON（application-default login）

### 底层工作原理

执行 `gcloud auth application-default login` 后，gcloud 打开浏览器完成 OAuth 授权，将结果写入：

```
~/.config/gcloud/application_default_credentials.json
```

典型文件结构：

```json
{
  "type": "authorized_user",
  "client_id": "EXAMPLE_CLIENT_ID.apps.googleusercontent.com",
  "client_secret": "example-client-secret",
  "refresh_token": "1//example-refresh-token-do-not-use",
  "quota_project_id": "your-project-id",
  "account": "",
  "universe_domain": "googleapis.com"
}
```

**文件内没有 access token**，只有 `refresh_token`。SDK 每次调用前：

```
refresh_token + client_id + client_secret
    → POST https://oauth2.googleapis.com/token
    → access_token（~1h）
    → 调用 Vertex AI API
```

`account` 字段为空不代表匿名——**用户身份绑定在 `refresh_token` 里**，Google 服务端知道是哪个用户授权的。

### 安全风险来源

| 风险 | 说明 |
|------|------|
| **文件泄露 = 长期盗用** | 攻击者拿到 JSON 后，在任意机器、任意语言 SDK 中设置 `GOOGLE_APPLICATION_CREDENTIALS` 即可调用 API，**无需 gcloud CLI、无需 Google 登录** |
| **权限范围 = 用户 IAM** | 冒充的是授权用户的全部 GCP 权限，往往比 SA 更宽 |
| **可无限续期** | `refresh_token` 未被撤销前，可持续换取新 access token |
| **误提交项目** | 复制到项目目录后极易进入 Git 历史 |

### 最佳实践

- **仅用于本地开发**，禁止用于生产、CI、Docker 镜像。
- **永远不要**将 ADC JSON 复制到项目目录。
- 优先使用 [服务账号模拟](#8-服务账号模拟service-account-impersonation) 缩小权限面。
- 泄露后立即到 [Google 账号第三方应用权限](https://myaccount.google.com/permissions) 撤销授权，并执行 `gcloud auth application-default revoke`。

### Prerequisites

```bash
# 安装 gcloud CLI 并登录
gcloud auth login
gcloud config set project YOUR_PROJECT_ID

# 生成本地 ADC（会弹浏览器）
gcloud auth application-default login

# 可选：指定配额项目
gcloud auth application-default login --project YOUR_PROJECT_ID
```

### 示例代码

```python
import os
from google import genai
from google.genai.types import HttpOptions

# SDK 自动读取 ~/.config/gcloud/application_default_credentials.json
# 或 GOOGLE_APPLICATION_CREDENTIALS 指向的文件
os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "true")
os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "your-project-id")
os.environ.setdefault("GOOGLE_CLOUD_LOCATION", "us-central1")

client = genai.Client(
    http_options=HttpOptions(api_version="v1"),
)
response = client.models.generate_content(
    model="gemini-2.5-flash",
    contents="Hello",
)
print(response.text)
```

---

## 3. 服务账号密钥 JSON（SA Key）

### 底层工作原理

通过 GCP 控制台或 CLI 为 Service Account 创建密钥对，下载 JSON 文件：

```json
{
  "type": "service_account",
  "project_id": "your-project-id",
  "private_key_id": "...",
  "private_key": "-----BEGIN PRIVATE KEY-----\n...",
  "client_email": "my-sa@your-project-id.iam.gserviceaccount.com",
  "client_id": "...",
  ...
}
```

SDK 使用 `private_key` 对 JWT 签名，向 Google OAuth 换取 access token，之后调用 API。流程与用户 ADC 类似，但冒充的是**服务账号**而非真人用户。

### 安全风险来源

| 风险 | 说明 |
|------|------|
| **与用户 ADC 同等严重** | 私钥泄露后可在任意环境长期使用，无需 Google 身份验证 |
| **难以轮换** | 团队多人持有一份 key 时，轮换成本高 |
| **Google 官方不推荐** | 长期 SA Key 已被明确列为应避免的做法 |

### 最佳实践

- **避免创建 SA Key**；若已存在，尽快迁移到元数据服务器或 Workload Identity。
- 必须使用时的底线：不进 Git、不进镜像、权限最小化、定期轮换、用 Secret Manager 注入。
- 生产首选：给 Cloud Run/GKE 附加 SA，**不下载密钥**。

### Prerequisites

```bash
# 创建 SA（若不存在）
gcloud iam service-accounts create my-sa \
  --display-name="My Service Account"

# 授予 Vertex AI 权限（示例）
gcloud projects add-iam-policy-binding YOUR_PROJECT_ID \
  --member="serviceAccount:my-sa@YOUR_PROJECT_ID.iam.gserviceaccount.com" \
  --role="roles/aiplatform.user"

# 下载密钥（不推荐，仅作了解）
gcloud iam service-accounts keys create sa-key.json \
  --iam-account=my-sa@YOUR_PROJECT_ID.iam.gserviceaccount.com

# 指定凭证文件
export GOOGLE_APPLICATION_CREDENTIALS=/path/to/sa-key.json
export GOOGLE_CLOUD_PROJECT=YOUR_PROJECT_ID
export GOOGLE_CLOUD_LOCATION=us-central1
```

### 示例代码

```python
import os
from google import genai
from google.genai.types import HttpOptions

os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = "/path/to/sa-key.json"

client = genai.Client(
    vertexai=True,
    project=os.environ["GOOGLE_CLOUD_PROJECT"],
    location=os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1"),
    http_options=HttpOptions(api_version="v1"),
)
response = client.models.generate_content(
    model="gemini-2.5-flash",
    contents="Hello",
)
print(response.text)
```

---

## 4. 短期 Access Token（gcloud CLI / .env）

> **说明**：「gcloud 子进程取 token」与「`.env` 注入 token」本质是**同一种鉴权方式**的两个环节——前者解决「怎么拿 token」，后者解决「token 放哪」。本节合并讲解。

### 底层工作原理

**凭证来源**：`gcloud auth login` 在浏览器完成 OAuth 后，将 refresh token 存入 `~/.config/gcloud/`（CLI 专用缓存，与用户 ADC 文件分开存储）。

**换取短期 token**：`gcloud auth print-access-token` 不会弹浏览器，它只是：

```
读取 ~/.config/gcloud/ 中的 CLI refresh token
    → 向 Google OAuth 换取 access token
    → 打印到 stdout（约 1 小时有效）
```

**注入 SDK**：代码侧统一使用仅含 token、不含 `refresh_token` 的 `Credentials`：

```python
Credentials(token="ya29.example-access-token")
```

SDK 直接使用该 token；无 `refresh_token` 时无法自动续期，过期后须重新获取。

**token 背后的身份**（与交付方式无关，取决于获取命令）：

| 获取命令 | token 代表谁 | API 权限范围 |
|----------|-------------|--------------|
| `gcloud auth print-access-token` | 当前 `gcloud login` 的用户 | 该用户的全部 IAM |
| `gcloud auth print-access-token --impersonate-service-account=SA` | 被模拟的服务账号 | **仅该 SA 的 IAM** |

### 两种交付方式

同一种短期 token，有两种常见用法——**区别仅在 token 的存放与读取时机**：

| | **A. 运行时子进程** | **B. 预写入 .env** |
|---|---|---|
| token 获取时机 | 脚本每次启动时调用 gcloud | 开发前手动/脚本写入 `.env` |
| 代码读取 | `subprocess.run(["gcloud", "auth", "print-access-token"])` | `os.environ["GOOGLE_ACCESS_TOKEN"]` |
| 过期处理 | 每次启动重新获取（若未缓存到 .env） | 约 1h 后须手动更新 `.env` |
| 典型场景 | 个人本地脚本（如本项目 `main.py` 回退路径） | 对误泄露敏感、需明确 token 来源 |
| 可组合 | ✅ 优先读 `.env`，为空再子进程获取 | ✅ 推荐模式 |

结合 SA 模拟的推荐流程（兼顾短泄露窗口 + 权限收窄）：

```
你的用户（gcloud login，需 roles/iam.serviceAccountTokenCreator）
    → gcloud auth print-access-token --impersonate-service-account=dev-sa@...
    → 短期 access token（身份 = dev-sa，约 1h）
    → 写入 .env 或运行时子进程获取
    → Credentials(token=...) → 调用 Vertex AI
```

与 [第 8 节 SDK 内建 SA 模拟](#8-服务账号模拟service-account-impersonation) 对比：

| | 短期 token（本节） | SDK 内建 Impersonation |
|---|---|---|
| token 存放 | `.env` 或子进程临时获取 | 内存中自动刷新 |
| 泄露危害 | 约 1h，可加 SA 模拟收窄 | 取决于 source 凭证 |
| 代码复杂度 | 低 | 需 `impersonated_credentials` |
| 过期处理 | 手动重新执行 gcloud | SDK 自动续期 |

### 安全风险来源

| 风险 | 说明 |
|------|------|
| **本机长期凭证仍在** | `~/.config/gcloud/` 泄露后果与用户 ADC 类似（获取 token 的前提） |
| **token 泄露窗口约 1 小时** | 比 refresh_token 直接泄露危害小得多 |
| **.env 误提交** | 1 小时内可被滥用 |
| **未模拟时权限过宽** | 用户身份 token 泄露 1h 内可调用户全部 IAM 允许的 API |
| **模拟可收窄权限** | 加 `--impersonate-service-account` 后，泄露 token 仅能用 SA 权限 |
| **子进程依赖 gcloud** | 生产环境通常无 gcloud，不适合正式服务 |

### 最佳实践

- 适合**本地脚本 / 个人开发**，不适合生产服务。
- **推荐**用 SA 模拟生成 token，而非用户全权限 token。
- 优先「读 `.env` → 为空再子进程」组合（本项目 `main.py` 模式）。
- `.env` 必须加入 `.gitignore`；不要把 token 提交到 Git。
- 配合 `Credentials(token=...)` 不传 `refresh_token`，避免 SDK 自动续期。

### Prerequisites

```bash
# 首次登录（会弹浏览器）
gcloud auth login
gcloud config set project YOUR_PROJECT_ID
gcloud auth list

# ── 用户身份 token（不推荐日常使用）──
gcloud auth print-access-token

# ── SA 模拟 token（推荐，前置见第 8 节）──
DEV_SA=dev-vertex-sa@YOUR_PROJECT_ID.iam.gserviceaccount.com
gcloud auth print-access-token --impersonate-service-account="$DEV_SA"

# 写入 .env（交付方式 B）
echo "GOOGLE_ACCESS_TOKEN=$(gcloud auth print-access-token --impersonate-service-account=$DEV_SA)" >> .env
echo "GOOGLE_IMPERSONATE_SERVICE_ACCOUNT=$DEV_SA" >> .env
```

`.env` 示例：

```env
GOOGLE_CLOUD_PROJECT=your-project-id
GOOGLE_CLOUD_LOCATION=us-central1
GOOGLE_ACCESS_TOKEN=ya29.example-access-token
GOOGLE_IMPERSONATE_SERVICE_ACCOUNT=dev-vertex-sa@your-project-id.iam.gserviceaccount.com
GOOGLE_GENAI_API_VERSION=v1
GEMINI_MODEL=gemini-2.5-flash
```

### 示例代码

推荐写法：优先 `.env`，回退子进程（交付方式 A + B 组合）：

```python
import os
import subprocess
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from google.genai.types import HttpOptions
from google.oauth2.credentials import Credentials

load_dotenv(Path(__file__).parent / ".env")


def get_access_token() -> str:
    token = os.getenv("GOOGLE_ACCESS_TOKEN", "").strip()
    if token:
        return token

    cmd = ["gcloud", "auth", "print-access-token"]
    if sa := os.getenv("GOOGLE_IMPERSONATE_SERVICE_ACCOUNT", "").strip():
        cmd += ["--impersonate-service-account", sa]

    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return result.stdout.strip()


credentials = Credentials(token=get_access_token())

client = genai.Client(
    vertexai=True,
    project=os.environ["GOOGLE_CLOUD_PROJECT"],
    location=os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1"),
    credentials=credentials,
    http_options=HttpOptions(api_version="v1"),
)
response = client.models.generate_content(
    model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash"),
    contents="Hello",
)
print(response.text)
```

> 本项目 `main.py` 即采用此合并模式（优先读 `.env` 中的 `GOOGLE_ACCESS_TOKEN`，为空时回退 gcloud；若设置 `GOOGLE_IMPERSONATE_SERVICE_ACCOUNT` 则子进程回退路径会附带 SA 模拟）。

---

## 5. SDK 隐式 ADC（不传 credentials）

### 底层工作原理

`genai.Client()` 不传 `credentials` 时，SDK 内部调用 `google.auth.default()`，自动走 [第 1 节](#1-adc-凭证发现链总览) 的凭证发现链。开发者无需关心具体来源——在 Cloud Run 上会自动用元数据服务器，在本地有 ADC 文件时会用 ADC。

同时可通过环境变量配置 Vertex AI 模式：

```bash
export GOOGLE_GENAI_USE_VERTEXAI=true
export GOOGLE_CLOUD_PROJECT=your-project-id
export GOOGLE_CLOUD_LOCATION=us-central1
```

### 安全风险来源

- **隐式行为不透明**：团队成员不清楚实际用的是用户 ADC、SA Key 还是元数据服务器。
- **本地与生产行为不一致**：本地靠 JSON，生产靠元数据，问题难以复现。

### 最佳实践

- 团队内文档化「本地应如何配置 ADC」。
- 生产部署时显式设置运行环境（Cloud Run SA、GKE Workload Identity），不依赖隐式猜测。
- 本地开发优先用受限 SA 模拟，而非个人用户全权限 ADC。

### Prerequisites

```bash
# 方式 A：用户 ADC
gcloud auth application-default login

# 方式 B：SA Key（不推荐）
export GOOGLE_APPLICATION_CREDENTIALS=/path/to/sa-key.json

# 方式 C：Cloud Run / GKE（无需额外配置，平台自动处理）
# 确保服务绑定了具有 roles/aiplatform.user 的 SA

# 通用环境变量
export GOOGLE_GENAI_USE_VERTEXAI=true
export GOOGLE_CLOUD_PROJECT=your-project-id
export GOOGLE_CLOUD_LOCATION=us-central1
```

### 示例代码

```python
import os
from google import genai
from google.genai.types import HttpOptions

os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "true")
os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "your-project-id")
os.environ.setdefault("GOOGLE_CLOUD_LOCATION", "us-central1")

client = genai.Client(
    http_options=HttpOptions(api_version="v1"),
)

response = client.models.generate_content(
    model="gemini-2.5-flash",
    contents="Hello",
)
print(response.text)
```

---

## 6. 元数据服务器（GCE / GKE / Cloud Run）

### 底层工作原理

在 GCP 计算资源上，平台为实例附加一个 Service Account。SDK 通过访问元数据服务器获取短期 token：

```
GET http://metadata.google.internal/computeMetadata/v1/
     instance/service-accounts/default/token
Header: Metadata-Flavor: Google
```

返回：

```json
{
  "access_token": "ya29.example-access-token",
  "expires_in": 3599,
  "token_type": "Bearer"
}
```

特点：

- **无 JSON 文件、无 refresh_token、无用户登录**
- token 约 1 小时过期，由 SDK 自动刷新（再次请求元数据服务器）
- token 权限 = 附加 SA 的 IAM 角色

### 安全风险来源

| 风险 | 说明 |
|------|------|
| **SA 权限过大** | 附加了 Owner/Editor 的 SA 仍会造成严重危害 |
| **元数据 SSRF** | 若应用存在 SSRF 漏洞，攻击者可能从实例内读取 token（需加固应用） |
| **错误配置** | 使用默认 Compute Engine SA 且权限未收紧 |

### 最佳实践

- **生产环境首选**。
- 为每个服务创建专用 SA，只授予 `roles/aiplatform.user` 等最小权限。
- Cloud Run：`--service-account=my-sa@project.iam.gserviceaccount.com`
- GKE：启用 Workload Identity，Pod 使用 KSA → GSA 绑定。
- **绝不**在镜像中打包 SA Key 或用户 ADC JSON。

### Prerequisites

```bash
# Cloud Run 部署示例
gcloud run deploy vertex-demo \
  --image=gcr.io/YOUR_PROJECT_ID/vertex-demo \
  --service-account=vertex-sa@YOUR_PROJECT_ID.iam.gserviceaccount.com \
  --set-env-vars=GOOGLE_CLOUD_PROJECT=YOUR_PROJECT_ID,GOOGLE_CLOUD_LOCATION=us-central1 \
  --region=us-central1

# 确保 SA 有 Vertex AI 权限
gcloud projects add-iam-policy-binding YOUR_PROJECT_ID \
  --member="serviceAccount:vertex-sa@YOUR_PROJECT_ID.iam.gserviceaccount.com" \
  --role="roles/aiplatform.user"
```

### 示例代码

```python
# 与隐式 ADC 相同——在 Cloud Run 上无需任何凭证配置
from google import genai
from google.genai.types import HttpOptions

client = genai.Client(
    vertexai=True,
    project="your-project-id",       # 或由 GOOGLE_CLOUD_PROJECT 环境变量提供
    location="us-central1",
    http_options=HttpOptions(api_version="v1"),
)
response = client.models.generate_content(
    model="gemini-2.5-flash",
    contents="Hello from Cloud Run",
)
print(response.text)
```

---

## 7. Workload Identity Federation（CI/CD）

### 底层工作原理

适用于 GitHub Actions、GitLab CI、Azure DevOps 等**不在 GCP 内运行**的流水线。通过 OIDC 将外部身份联邦到 GCP，换取**短期** SA access token，全程无 SA Key 文件：

```
CI 平台签发 OIDC Token（如 GitHub Actions ID Token）
    → GCP STS（Security Token Service）验证外部身份
    → 颁发短期 access token（冒充指定 SA）
    → 调用 Vertex AI API
```

### 安全风险来源

| 风险 | 说明 |
|------|------|
| **WIF 配置过宽** | `principalSet` 条件不严谨时，非预期仓库/分支可冒充 SA |
| **SA 权限过大** | 联邦成功后权限仍取决于 SA IAM |
| **误回退到 SA Key** | 团队图省事下载 key 会破坏零密钥模型 |

### 最佳实践

- CI/CD 的**推荐方式**，替代 SA Key JSON。
- WIF 条件精确到 `repository`、`ref`（如 `refs/heads/main`）。
- CI 中使用 `google-github-actions/auth` 等官方 Action。

### Prerequisites

```bash
# 1. 创建 Workload Identity Pool 和 Provider（一次性基础设施）
gcloud iam workload-identity-pools create "github-pool" \
  --location="global" \
  --display-name="GitHub Pool"

gcloud iam workload-identity-pools providers create-oidc "github-provider" \
  --location="global" \
  --workload-identity-pool="github-pool" \
  --issuer-uri="https://token.actions.githubusercontent.com" \
  --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository" \
  --attribute-condition="assertion.repository=='org/repo'"

# 2. 允许特定 GitHub 仓库冒充 SA
gcloud iam service-accounts add-iam-policy-binding \
  ci-sa@YOUR_PROJECT_ID.iam.gserviceaccount.com \
  --role="roles/iam.workloadIdentityUser" \
  --member="principalSet://iam.googleapis.com/projects/PROJECT_NUMBER/locations/global/workloadIdentityPools/github-pool/attribute.repository/org/repo"
```

GitHub Actions 示例：

```yaml
# .github/workflows/vertex.yml
jobs:
  call-vertex:
    runs-on: ubuntu-latest
    permissions:
      id-token: write
      contents: read
    steps:
      - uses: actions/checkout@v4

      - uses: google-github-actions/auth@v2
        with:
          workload_identity_provider: projects/PROJECT_NUMBER/locations/global/workloadIdentityPools/github-pool/providers/github-provider
          service_account: ci-sa@YOUR_PROJECT_ID.iam.gserviceaccount.com

      - uses: google-github-actions/setup-gcloud@v2

      - name: Run script
        env:
          GOOGLE_CLOUD_PROJECT: YOUR_PROJECT_ID
          GOOGLE_CLOUD_LOCATION: us-central1
        run: python main.py
```

---

## 8. 服务账号模拟（Service Account Impersonation）

### 底层工作原理

已登录的用户（或另一个 SA）通过 IAM `roles/iam.serviceAccountTokenCreator` 权限，**临时冒充**目标 SA 获取 access token：

```
你的用户凭证（gcloud login）
    → IAM Credentials API: generateAccessToken
    → 目标 SA 身份的短期 access token
    → 调用 Vertex AI（权限 = 目标 SA 的 IAM）
```

token 短期有效，且权限限于被模拟的 SA，而非你的完整用户权限。

### 安全风险来源

| 风险 | 说明 |
|------|------|
| **用户账号仍可能过宽** | 模拟前用户本身若有 Token Creator 等权限，账号泄露仍有风险 |
| **模拟目标 SA 权限过大** | 缩小了范围但仍需最小化 SA 角色 |

### 最佳实践

- **本地 SDK 开发的推荐方式**：个人用户登录 + 模拟受限开发 SA。
- 比直接使用用户全权限 ADC 安全得多。
- 配合 `gcloud auth application-default login --impersonate-service-account=...` 或代码中显式 impersonation。

### Prerequisites

```bash
# 1. 创建开发用 SA
gcloud iam service-accounts create dev-vertex-sa \
  --display-name="Dev Vertex SA"

# 2. 授予 SA Vertex AI 权限
gcloud projects add-iam-policy-binding YOUR_PROJECT_ID \
  --member="serviceAccount:dev-vertex-sa@YOUR_PROJECT_ID.iam.gserviceaccount.com" \
  --role="roles/aiplatform.user"

# 3. 允许你的用户模拟该 SA
gcloud iam service-accounts add-iam-policy-binding \
  dev-vertex-sa@YOUR_PROJECT_ID.iam.gserviceaccount.com \
  --member="user:you@example.com" \
  --role="roles/iam.serviceAccountTokenCreator"

# 4. 登录并配置 ADC 模拟
gcloud auth application-default login \
  --impersonate-service-account=dev-vertex-sa@YOUR_PROJECT_ID.iam.gserviceaccount.com

# 或通过 CLI 直接获取模拟 token（可写入 .env，见第 4 节）
gcloud auth print-access-token \
  --impersonate-service-account=dev-vertex-sa@YOUR_PROJECT_ID.iam.gserviceaccount.com
```

### 与第 4 节的关系

第 8 节的 SA 模拟也可通过 `gcloud auth print-access-token --impersonate-service-account=...` 产出短期 token 写入 `.env`，代码侧与 [第 4 节](#4-短期-access-tokengcloud-cli--env) 完全相同。两种方式对比如下：

- **第 4 节（token 落盘）**：手动/脚本取 token → `.env` → `Credentials(token=...)`；过期需重新获取；泄露窗口约 1h。
- **第 8 节（SDK 内建）**：`impersonated_credentials.Credentials` 在内存中自动续期；无需 `.env` 存 token；适合长期运行的本地 SDK 开发。

本地开发可二选一，或组合使用：日常用第 8 节 SDK 模拟，对 token 落盘有顾虑时用第 4 节 + SA 模拟。

### 示例代码（SDK 内建 impersonation）

```python
import google.auth
import google.auth.impersonated_credentials
from google import genai
from google.genai.types import HttpOptions

target_sa = "dev-vertex-sa@YOUR_PROJECT_ID.iam.gserviceaccount.com"

# 基于当前 ADC（用户登录）构建模拟凭证
source_creds, _ = google.auth.default()
credentials = google.auth.impersonated_credentials.Credentials(
    source_credentials=source_creds,
    target_principal=target_sa,
    target_scopes=["https://www.googleapis.com/auth/cloud-platform"],
)

client = genai.Client(
    vertexai=True,
    project="YOUR_PROJECT_ID",
    location="us-central1",
    credentials=credentials,
    http_options=HttpOptions(api_version="v1"),
)
response = client.models.generate_content(
    model="gemini-2.5-flash",
    contents="Hello via impersonation",
)
print(response.text)
```

---

## 9. 方式对比与场景选型

### 安全与便携性对比

| 鉴权方式 | 凭证形态 | 泄露后有效期 | 需 gcloud CLI | 适合场景 |
|----------|----------|--------------|---------------|----------|
| 用户 ADC JSON | refresh_token | **长期** | 生成时需要 | 本地 SDK 开发（不推荐放项目内） |
| SA Key JSON | private_key | **长期** | 生成时需要 | 遗留系统（应避免） |
| 短期 token（gcloud / .env） | access_token only | **~1 小时** | 获取时需要 | **本地脚本推荐**（可加 SA 模拟） |
| 隐式 ADC | 取决于环境 | 取决于来源 | 本地需要 | 通用 SDK 调用 |
| 元数据服务器 | 平台签发 token | ~1 小时（自动刷新） | **不需要** | **生产首选** |
| Workload Identity Federation | 联邦短期 token | 短期 | **不需要** | **CI/CD 首选** |
| SA Impersonation | 模拟短期 token | 短期 | 配置时需要 | **本地开发推荐** |

### 按环境推荐

| 环境 | 推荐方案 | 避免 |
|------|----------|------|
| 本地个人脚本 | gcloud login + 短期 token（优先 SA 模拟），见 [第 4 节](#4-短期-access-tokengcloud-cli--env) | 项目内 ADC/SA JSON |
| 本地 SDK 开发 | SA Impersonation + ADC | 用户全权限 ADC 进项目 |
| CI/CD | Workload Identity Federation | SA Key JSON |
| Cloud Run / GKE / GCE | 元数据服务器 + 专用 SA | 任何密钥文件 |
| 临时调试 | `gcloud auth print-access-token` 手动注入 | 将 refresh_token 写入 .env |

### 本地开发安全分层（推荐）

```
第一层：凭证永不进入项目目录（零 JSON in repo）
第二层：IAM 最小化 + SA 模拟（权限限于 dev-sa）
第三层：可选 .env 短期 token + SA 模拟（降低误泄露窗口，且权限限于 dev-sa）
第四层：泄露事件 → 撤销 + 审计（而非每次开发完 revoke）
```

---

## 10. 安全事件响应清单

若怀疑 `application_default_credentials.json`、SA Key 或 token 泄露：

1. **立即撤销**
   - 用户 ADC：[Google 账号 → 第三方应用访问权限](https://myaccount.google.com/permissions)
   - CLI：`gcloud auth revoke ACCOUNT` / `gcloud auth application-default revoke`
   - SA Key：在 GCP 控制台删除对应密钥

2. **审计**
   - 查看 Cloud Audit Logs（`your-project-id` 或对应项目）
   - 筛选异常 IP、异常 API 调用时间窗口

3. **清理**
   - 从项目目录删除 JSON 文件
   - 确认 `.gitignore` 包含 `*.json` 凭证模式
   - 若曾提交 Git，清理历史记录并轮换所有受影响凭证

4. **加固**
   - 迁移到 SA 模拟或元数据服务器
   - 收紧 IAM：移除 Owner/Editor，改用细粒度角色

---

## 附录：攻击者利用泄露 ADC JSON 的最小步骤

以下说明**为何必须保护 JSON 文件**（仅供防御参考）：

```bash
# 攻击者无需安装 gcloud，无需 Google 登录
export GOOGLE_APPLICATION_CREDENTIALS=/tmp/stolen_adc.json
export GOOGLE_CLOUD_PROJECT=your-project-id
export GOOGLE_CLOUD_LOCATION=us-central1

python -c "
from google import genai
client = genai.Client(vertexai=True, http_options={'api_version': 'v1'})
print(client.models.generate_content(model='gemini-2.5-flash', contents='pwned').text)
"
```

**结论**：泄露的 ADC JSON / SA Key 是可移植的长期密钥，危害不取决于是否在 `~/.config/gcloud/` 默认路径，也不取决于是否安装 gcloud CLI。

---

## 11. 官方参考文档

以下链接为 Google 官方文档，按本文各节主题归类，便于对照核实结论。

### 总览：认证与 ADC

| 主题 | 官方文档 |
|------|----------|
| Google Cloud 认证总览 | [Authentication for Google Cloud APIs and services](https://cloud.google.com/docs/authentication) |
| ADC 工作原理（凭证发现顺序） | [How Application Default Credentials works](https://cloud.google.com/docs/authentication/application-default-credentials) |
| 配置 ADC（按环境选择） | [Set up Application Default Credentials](https://cloud.google.com/docs/authentication/provide-credentials-adc) |
| Client Library 自动认证 | [Authenticate with client libraries](https://cloud.google.com/docs/authentication/client-libraries) |

### 对应本文 §2、§5：用户 ADC 与隐式 ADC

| 主题 | 官方文档 |
|------|----------|
| 本地开发环境配置 ADC | [Set up ADC for a local development environment](https://cloud.google.com/docs/authentication/set-up-adc-local-dev-environment) |
| `gcloud auth application-default login` | [gcloud auth application-default login 命令参考](https://cloud.google.com/sdk/gcloud/reference/auth/application-default/login) |
| `GOOGLE_APPLICATION_CREDENTIALS` 环境变量 | [How ADC works — GOOGLE_APPLICATION_CREDENTIALS](https://cloud.google.com/docs/authentication/application-default-credentials#GAC) |
| gcloud CLI 凭证 ≠ ADC 凭证 | [Set up ADC — The gcloud CLI and ADC](https://cloud.google.com/docs/authentication/provide-credentials-adc#cli-adc) |

### 对应本文 §3：服务账号密钥（SA Key）

| 主题 | 官方文档 |
|------|----------|
| 创建/删除 SA Key | [Create and delete service account keys](https://cloud.google.com/iam/docs/keys-create-delete) |
| SA 安全最佳实践（含避免使用 Key） | [Best practices for using service accounts securely](https://cloud.google.com/iam/docs/best-practices-service-accounts) |
| 从 SA Key 迁移到更安全方案 | [Migrate from service account keys](https://cloud.google.com/iam/docs/migrate-from-service-account-keys) |

### 对应本文 §4：短期 Access Token（gcloud CLI）

| 主题 | 官方文档 |
|------|----------|
| gcloud CLI 认证方式总览 | [Authenticate for the gcloud CLI](https://cloud.google.com/docs/authentication/gcloud) |
| `gcloud auth login`（CLI 登录，会弹浏览器） | [gcloud auth login 命令参考](https://cloud.google.com/sdk/gcloud/reference/auth/login) |
| `gcloud auth print-access-token` | [gcloud auth print-access-token 命令参考](https://cloud.google.com/sdk/gcloud/reference/auth/print-access-token) |
| 在另一设备使用 access token | [Use the gcloud CLI with an access token](https://cloud.google.com/docs/authentication/gcloud#access-tokens) |

### 对应本文 §6：元数据服务器（生产环境）

| 主题 | 官方文档 |
|------|----------|
| 为附加 SA 的资源配置 ADC | [Set up ADC for a resource with an attached service account](https://cloud.google.com/docs/authentication/set-up-adc-attached-service-account) |
| Cloud Run 服务身份与元数据服务器 | [Cloud Run — Introduction to service identity](https://cloud.google.com/run/docs/securing/service-identity) |
| Compute Engine 认证与 ADC | [Authenticate to Compute Engine](https://cloud.google.com/compute/docs/authentication) |
| 工作负载身份类型总览 | [Identities for workloads](https://cloud.google.com/iam/docs/workload-identities) |

### 对应本文 §7：Workload Identity Federation（CI/CD）

| 主题 | 官方文档 |
|------|----------|
| WIF 概念总览 | [Workload Identity Federation](https://cloud.google.com/iam/docs/workload-identity-federation) |
| 部署流水线（GitHub Actions 等） | [Configure WIF with deployment pipelines](https://cloud.google.com/iam/docs/workload-identity-federation-with-deployment-pipelines) |
| GitHub Actions 无密钥认证（官方博客） | [Enabling keyless authentication from GitHub Actions](https://cloud.google.com/blog/products/identity-security/enabling-keyless-authentication-from-github-actions) |
| `google-github-actions/auth` Action | [google-github-actions/auth（GitHub）](https://github.com/google-github-actions/auth) |

### 对应本文 §8：服务账号模拟（Impersonation）

| 主题 | 官方文档 |
|------|----------|
| SA 模拟概念与权限 | [Service account impersonation](https://cloud.google.com/iam/docs/service-account-impersonation) |
| 在认证流程中使用 SA 模拟 | [Use service account impersonation](https://cloud.google.com/docs/authentication/use-service-account-impersonation) |
| 创建 SA 短期凭证（含 `print-access-token --impersonate-service-account`） | [Create short-lived credentials for a service account](https://cloud.google.com/iam/docs/create-short-lived-credentials-direct) |
| 本地 ADC + `--impersonate-service-account` | [Set up ADC for local dev — Service account impersonation](https://cloud.google.com/docs/authentication/set-up-adc-local-dev-environment#service-account-impersonation) |

### Vertex AI 与 Google Gen AI SDK

| 主题 | 官方文档 |
|------|----------|
| Vertex AI / Agent Platform 认证 | [Authenticate to Agent Platform](https://cloud.google.com/vertex-ai/docs/authentication) |
| 本地开发环境配置 | [Set up a project and a development environment](https://cloud.google.com/vertex-ai/docs/start/cloud-environment) |
| Google Gen AI Python SDK 文档 | [Google Gen AI SDK documentation](https://googleapis.github.io/python-genai/) |
| Google Gen AI Python SDK 源码与 README | [googleapis/python-genai（GitHub）](https://github.com/googleapis/python-genai) |

### 安全与审计

| 主题 | 官方文档 |
|------|----------|
| 禁用 SA Key 创建（组织策略） | [Best practices — Limit service account key creation](https://cloud.google.com/iam/docs/best-practices-service-accounts#limit-key-creation) |
| 限制元数据服务器访问 | [Best practices — Limit metadata server access](https://cloud.google.com/iam/docs/best-practices-service-accounts#limit-metadata-access) |
| Cloud Audit Logs | [Cloud Audit Logs overview](https://cloud.google.com/logging/docs/audit) |

---

*文档版本：基于 2026-06 项目实践整理，适用于 `google-genai >= 2.10.0` 与 Vertex AI Generative API。*
