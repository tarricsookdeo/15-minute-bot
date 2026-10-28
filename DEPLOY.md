# Deploying the 15-Minute Bot

This repository, **tarricsookdeo/15-minute-bot**, is a fork of
[PublicDotCom/15-minute-bot](https://github.com/PublicDotCom/15-minute-bot), the source of truth
for the app, and is the copy that's deployed. It's the upstream code plus only these files, which
upstream doesn't have:

| File | What it is |
|---|---|
| `DEPLOY.md` | This guide |
| `Dockerfile`, `.dockerignore` | The production image (non-root, never writes to disk) |
| `.do/app.yaml` | The DigitalOcean App Platform spec: one instance, plus the Postgres cluster |
| `.github/workflows/deploy.yml` | Test, build and deploy on every push to `main` |
| `.github/workflows/container.yml` | Checks the image builds and starts healthy as a non-root user |

Everything else (app code, tests, `README.md`, `.github/workflows/ci.yml`) is shared with upstream.

## Making changes

- **App changes go to upstream first:** open the pull request against
  `PublicDotCom/15-minute-bot`, and merge it there.
- **Then sync this fork**, which deploys it: the **Sync fork** button on this repository's GitHub
  page, or:

  ```sh
  gh repo sync tarricsookdeo/15-minute-bot --source PublicDotCom/15-minute-bot --branch main
  ```

  Never add `--force`: it resets this fork to upstream and deletes the deployment files. The same
  with plain git, in a clone of this fork with upstream added as a remote named `upstream`:

  ```sh
  git checkout main && git pull && git fetch upstream && git merge upstream/main && git push
  ```
- **Deployment changes stay here:** edit only the files in the table above, through pull
  requests against **this** repository (`gh pr create --repo tarricsookdeo/15-minute-bot`), since
  GitHub suggests upstream as the target for a fork's pull requests.
- **Don't edit shared files here.** Syncing then never hits a merge conflict, and nothing from
  the deployment leaks into upstream.

## How it runs

The app runs as one container on DigitalOcean App Platform, which provides HTTPS and supports the
WebSocket each session uses, plus a DigitalOcean Managed PostgreSQL cluster that keeps sessions
through deploys. It must run as **a single instance**: only one server can run the bots
(`instance_count: 1` in `.do/app.yaml`). Deploys don't end sessions: bots pause for a few seconds
while the new server takes over, then carry on (see the README, "Reliability").

| Workflow | Runs on | What it does |
|---|---|---|
| **CI** (`ci.yml`, shared) | every pull request | Runs the tests (also against a real Postgres) and checks the dashboard script |
| **Container** (`container.yml`) | every pull request | Builds the image and checks it starts healthy as a non-root user |
| **Deploy** (`deploy.yml`) | every push to `main`: a merged pull request or a sync from upstream (or by hand from the Actions tab) | Runs CI and Container, then builds the image, pushes it tagged with the commit, deploys exactly that image with the encryption key filled in, waits for `/healthz` to report the new server ready, and prunes old images |

A deploy only happens if both checks pass. Deploys run one at a time, in order. Each deployment
uses the image for one commit, so the App Platform dashboard can roll back to any of the last five.

## One-time setup

Needs [`doctl`](https://docs.digitalocean.com/reference/doctl/), the GitHub CLI and `uv`.

1. **Enable GitHub Actions** on this fork (they start disabled on forks): the repository's
   **Actions** tab → enable workflows.
2. **Create the registry and database** on DigitalOcean:

   ```sh
   doctl auth init                                      # log in to DigitalOcean
   doctl registry create <registry-name>                # a container registry (one per account)
   # The database (about $15/month). The name must match cluster_name in .do/app.yaml.
   doctl databases create fifteen-minute-bot-db --engine pg --version 16 --region nyc1 \
     --size db-s-1vcpu-1gb --num-nodes 1
   ```

3. **Create the encryption key** for stored API keys, and keep a copy somewhere safe (e.g. a
   password manager): if it's lost, every saved session is lost with it.

   ```sh
   uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   ```

4. **Create a DigitalOcean API token** with read and write access (DigitalOcean → API → Generate
   New Token).
5. **Give this repository the token, the encryption key and the registry name:**

   ```sh
   gh secret set DIGITALOCEAN_ACCESS_TOKEN --repo tarricsookdeo/15-minute-bot   # paste when asked
   gh secret set SESSION_ENCRYPTION_KEY --repo tarricsookdeo/15-minute-bot      # paste when asked
   gh variable set DO_REGISTRY --repo tarricsookdeo/15-minute-bot --body <registry-name>
   ```

6. **Require the checks before merging** (recommended): Settings → Branches → add a rule for
   `main` that requires "Tests" and "Container builds and starts".
7. **Deploy:** merge a pull request, sync from upstream, or run **Deploy** from the Actions tab.
   The first run creates the app; later runs update it. The run summary shows the live URL.
8. **Lock down the database:** attaching the cluster in the app spec adds the app to its trusted
   sources. Then restrict the cluster to trusted sources only (DigitalOcean → Databases → the
   cluster → Settings → Trusted sources).

Optional: to approve each deploy by hand (e.g. to deploy only when markets are quiet), add
required reviewers to the `production` environment (Settings → Environments). A sync from upstream
then waits for that approval too.

The repository is public. That's fine: it contains no secrets, the token and encryption key live
in GitHub's encrypted secrets (and App Platform's, once deployed), and workflows from other forks
never get access to them.

## Rotating the encryption key

Set the `SESSION_ENCRYPTION_KEY` secret to `<new key>,<old key>` and deploy (Actions → Deploy →
Run workflow). On start the server re-encrypts every saved session with the new key, so once that
deploy reports ready you can set the secret to just `<new key>` and deploy again. Replacing the
key outright, without the old one alongside, makes every saved session unreadable: they're
deleted on the next start and users must connect again.

## Deploying by hand

Without GitHub Actions (needs Docker):

```sh
doctl registry login
docker build --platform linux/amd64 -t registry.digitalocean.com/<registry-name>/fifteen-minute-bot:latest .
docker push registry.digitalocean.com/<registry-name>/fifteen-minute-bot:latest
# Put the encryption key into a copy of the spec (never commit it), then create or update the app.
sed "s|__SESSION_ENCRYPTION_KEY__|<the key>|" .do/app.yaml > /tmp/app.yaml
doctl apps create --spec /tmp/app.yaml --upsert && rm /tmp/app.yaml
```

`doctl apps list` shows the app's URL once it's live.

## Settings

`.do/app.yaml` sets the environment variables described in the README ("Configuration"):
`MAX_SESSIONS=200`, `DATABASE_URL` from the attached cluster, and `SESSION_ENCRYPTION_KEY` from
the GitHub secret. The `Dockerfile` sets `PORT=8080` and `FORWARDED_ALLOW_IPS=*`, which is safe
only because the container is reachable solely through App Platform's load balancer.

## Troubleshooting

| Problem | Fix |
|---|---|
| Deploy fails at "Check configuration" | Add the missing secret or variable it names (step 5). |
| Deploy waits on "ready" and then fails | The new server couldn't take over: check the app's runtime logs, and that the database is up and the app is a trusted source. |
| The page shows "The bot is restarting after an update" for minutes | Same as above. |
| "Sync fork" reports a conflict | A shared file was edited in this fork. Undo that change here, make it upstream instead, then sync. |
