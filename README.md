# hybrid-code-analysis

Hybrid automated code review combining **SonarQube static analysis** with an
**OpenAI LLM review**, triggered via GitHub Actions on every pull request and push.
Part of a bachelor thesis comparing LLM, static, and hybrid code review quality.

## How it works

```
Pull Request / Push
       │
       ▼
GitHub Actions workflow (.github/workflows/hybrid-code-review.yml)
       │
       ├─ Runs SonarCloud static analysis on the repository
       ├─ Fetches SonarQube issues via the SonarCloud REST API
       ├─ Computes git diff (PR base ↔ head  or  before ↔ after SHA)
       ├─ Sends diff + SonarQube findings to the OpenAI Chat Completions API
       │
       ├─ Posts the combined feedback as a comment on the pull request  (PR events only)
       └─ Uploads a Markdown feedback file as a GitHub Actions artifact
```

The LLM uses the **same system prompt** as in the
[llm-code-analysis](https://github.com/Goldbursch/llm-code-analysis) repository,
so review quality can be compared directly.  The only difference is that the user
message also contains the SonarQube findings, giving the model additional context.

## Setup

### 1. Connect the repository to SonarCloud

1. Go to [sonarcloud.io](https://sonarcloud.io) and log in with your GitHub account.
2. Click **+** → **Analyze new project** and select this repository.
3. Choose **GitHub Actions** as the analysis method.
4. Note down the **Organisation key** and **Project key** shown on screen.

### 2. Add repository secrets and variables

Go to **Settings → Secrets and variables → Actions** and add:

| Type | Name | Value |
|---|---|---|
| Secret | `OPENAI_API_KEY` | Your OpenAI API key (starts with `sk-…`) |
| Secret | `SONAR_TOKEN` | The SonarCloud token generated in step 1 |
| Variable | `SONAR_PROJECT_KEY` | The project key from SonarCloud (e.g. `my-org_my-project`) |

### 3. Update `sonar-project.properties`

Edit `sonar-project.properties` and set your `sonar.organization` and
`sonar.projectKey` to the values from step 1.

### 4. (Optional) Override the OpenAI model

Add a repository **variable** (not a secret):

| Name | Example value |
|---|---|
| `OPENAI_MODEL` | `gpt-4-turbo`, `gpt-3.5-turbo`, … |

By default the workflow uses **`gpt-4o`**.

### 5. (Optional) Self-hosted SonarQube

If you are running SonarQube on-premises instead of SonarCloud, add a variable:

| Name | Value |
|---|---|
| `SONAR_HOST_URL` | Your SonarQube base URL, e.g. `https://sonarqube.example.com` |

### 6. Required permissions

The workflow requests these permissions automatically:

| Permission | Reason |
|---|---|
| `contents: read` | Check out the repository |
| `pull-requests: write` | Post the review as a PR comment |
| `issues: write` | Required by the GitHub API for PR comments |

Make sure **Actions → General → Workflow permissions** in your repository settings
is set to *Read and write permissions*.

## Artifacts

Every workflow run uploads a Markdown file to the **Artifacts** section of the run:

```
feedback/<event>_<sha>_<timestamp>.md
```

The file contains both the raw SonarQube findings and the full LLM review, making
it easy to compare results side-by-side for the thesis.  Artifacts are retained for
**90 days**.

## Repository structure

```
.
├── .github/
│   └── workflows/
│       └── hybrid-code-review.yml   # GitHub Actions workflow
├── scripts/
│   └── analyze_code.py              # SonarQube fetcher + OpenAI integration
├── sonar-project.properties         # SonarCloud project configuration
├── requirements.txt                 # Python dependencies
└── README.md
```