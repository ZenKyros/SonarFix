# Running SonarFix with Docker

Needs only Docker Desktop (or Podman). No Python, Node, Java or git install.

1. `copy .env.example .env` and fill in your own tokens (never share a filled `.env`).
2. Put your repo clones in one folder and tell compose where it is:
   `$env:REPOS_DIR="C:\path\to\clones"` (default: `.\repos`).
3. `docker compose up -d --build`
4. Open http://localhost:8000
5. In **Projects > Use path**, enter the path as the container sees it:
   `/repos/<clone-folder>`.

Data (SQLite DB) lives in the `sonarfix-data` volume and survives restarts.
Update: `git pull` then `docker compose up -d --build`.

## Limits
- The image is Linux: .NET and Visual Studio C++ builds are not available in it.
  Maven, Gradle (wrapper), CMake/g++ and Java are.
- Pushing branches needs an HTTPS `origin` plus `BITBUCKET_TOKEN`; SSH remotes
  need your keys mounted into the container.
- `SONARFIX_ENGINE=claude-code` needs `claude login` inside the container, so
  the default `deepagents` engine with `ANTHROPIC_API_KEY` is simpler.
- If Sonar runs on your machine, use `host.docker.internal` instead of `localhost`.
