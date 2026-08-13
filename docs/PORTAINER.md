# Portainer deployment guide

This guide deploys the watcher from a GitHub branch into a Portainer-managed **Docker
Standalone** environment. That is the simplest supported setup because `compose.yaml` builds
the image directly from this repository. Docker Swarm does not build images from a Compose
`build:` instruction; for Swarm, publish the image to a registry such as GHCR and change the
stack to use `image:` instead.

## What the installation provides

Portainer clones the selected Git branch, builds the Docker image, runs a short-lived data-volume
initialization container, starts the watcher container, publishes its web UI, and creates the
persistent `watcher-data` volume. The initialization container only corrects volume ownership and
then exits successfully. The long-running watcher runs as a non-root user and exposes a `/healthz`
endpoint that Docker uses for its health check.

The image is complete, but a fresh installation is intentionally not preconfigured. It can start
and show the web UI without a Marktplaats URL or model key. The background watcher records a
configuration error and retries every 60 seconds until the required values are saved. No source
file or custom image rebuild is needed for normal configuration.

## Prerequisites

You need:

- Portainer connected to a Docker Standalone environment.
- A GitHub repository containing this project and the branch you want to deploy.
- Outbound HTTPS access from the Docker host to Marktplaats, the selected model provider, and
  optionally Telegram.
- An unused host TCP port, 8080 by default.
- A long random value for `WEB_ADMIN_TOKEN`.

Do not commit `.env`, API keys, Telegram tokens, or the `data` directory. They are excluded by
the repository's `.gitignore` and belong in Portainer or the persistent volume.

## Put the project on GitHub

If this folder is not yet a Git repository, create an empty GitHub repository and push the
project to it. A typical first push is:

```powershell
git init
git add .
git commit -m "Add the Marktplaats ad watcher service."
git branch -M main
git remote add origin https://github.com/<owner>/<repository>.git
git push -u origin main
```

Use a dedicated deployment branch if preferred. Portainer follows the selected branch, so only
merge changes into that branch when they are ready to deploy.

For a private repository, create a fine-grained GitHub personal access token that has read-only
access to repository contents. Store it in Portainer's Git credentials; never put it in the
Compose file.

## Create the Portainer stack

Open the target Docker Standalone environment in Portainer, select **Stacks**, then **Add stack**
and choose **Git repository**. Use these values:

| Portainer field | Value |
| --- | --- |
| Name | `marktplaats-ad-watcher` |
| Repository URL | `https://github.com/<owner>/<repository>.git` |
| Repository reference | The deployment branch, usually `refs/heads/main` |
| Compose path | `compose.yaml` |
| Authentication | Off for public repositories; on with saved Git credentials for private repositories |

Under **Environment variables**, add:

| Name | Required | Value |
| --- | --- | --- |
| `WEB_ADMIN_TOKEN` | Yes | A long random token, preferably at least 32 random characters |
| `WATCHER_PORT` | No | Host port for the UI; defaults to `8080` |

Do not add the model or Telegram keys here unless configuration must be fully environment-driven.
The intended first-run workflow is to enter those through the watcher web page, where they are
stored in the persistent settings file.

Click **Deploy the stack**. The first build downloads the Python base image and dependencies, so
it takes longer than later deployments. When complete, Portainer should show the data initializer
as successfully exited and the watcher container in a healthy state. The initializer is expected
to stop after completion and should not restart. The health check proves that the web process
responds; it does not prove that the Marktplaats or model settings are valid.

## First-run web configuration

A domain is not required. On the same network as the Docker host, open:

```text
http://<docker-host-ip>:8080/?token=<WEB_ADMIN_TOKEN>
```

Use the configured `WATCHER_PORT` instead of 8080 if it was changed. The status page links to
**Edit configuration**. At minimum, enter:

- The Marktplaats `lrp/api/search` URL copied from the browser network inspector.
- A precise description of which ads should count as relevant.
- The model provider API key, base URL, and model name.
- Telegram bot token and chat ID if Telegram notifications are wanted.

Save the form, return to the status page, and select **Run now**. With
`BOOTSTRAP_EXISTING_ADS=true`, the first successful normal run marks the current result set as
already seen and does not notify for all existing ads. Later runs evaluate only new ads.

The web form is the normal configuration mechanism. Internally it writes
`/app/data/settings.env`; existing secret values are not displayed, and leaving a secret field
blank keeps its current value. The same volume also contains:

- `seen_ads.json`, which prevents repeated evaluations.
- `evaluations.jsonl`, containing model decisions.
- `runtime_status.json`, shown on the status page.

Deleting or replacing the `watcher-data` volume deletes this configuration and history. Updating
or recreating only the container preserves it.

## Follow a GitHub branch automatically

Because the stack was created with **Git repository**, its Compose source remains read-only in
Portainer and must be changed in GitHub. In the stack details, Portainer can manually **Pull and
redeploy** the selected branch.

To automate updates, enable **GitOps updates** for the stack. Polling is the simplest mechanism:
choose **Polling** and an interval such as five minutes. Portainer checks the selected branch and
redeploys when its Git content changes. Leave **Force redeployment** off unless the stack must be
recreated even when there is no Git change.

This stack builds locally from the cloned source, rather than pulling an application image with a
moving tag. Consequently, **Re-pull image** is not needed for application updates. It only affects
base or separately referenced images. A source change should invalidate the relevant Docker build
layers during redeployment.

Portainer Business Edition can use a GitOps webhook instead of polling. If that feature is
available, choose **Webhook**, copy the generated URL, and invoke it from a GitHub Actions workflow
after a successful push or test job. Treat the webhook URL as a secret. Polling requires less
GitHub configuration and is usually sufficient for this service.

For a more reproducible production pipeline, build and test an immutable image in GitHub Actions,
push it to GHCR with a commit-based tag, and make the Portainer stack reference that image. That
separates image building from deployment and is the required pattern for Docker Swarm.

## Domain and network access

The service works without DNS or a domain. For a trusted home network, access it using the Docker
host's IP address and mapped port. Ensure the host firewall allows that port only from the intended
network.

Do not forward port 8080 directly from the public internet. The UI has token authentication, but
the direct endpoint is plain HTTP and a token placed in the initial URL can appear in browser or
proxy history. For remote access, place the service behind an HTTPS reverse proxy such as Traefik,
Caddy, or Nginx Proxy Manager, use a domain name, and add an additional access-control layer or
VPN. The reverse proxy should forward to the container's published port.

## Updating, backup, and recovery

Before a risky update, back up the `watcher-data` volume or at least its `settings.env`,
`seen_ads.json`, and `evaluations.jsonl` files. Portainer stack redeployments preserve the named
volume. Selecting an option that removes volumes, or manually deleting the volume, does not.

After a Git update, check the stack's container list and logs. The container should return to
`healthy`, and the status page should show a future **Next run** value. If an update fails, select
the previous known-good Git commit or branch and pull/redeploy again.

## Troubleshooting

**The Compose deployment requires `WEB_ADMIN_TOKEN`.** Add it under the stack's environment
variables. Compose intentionally refuses deployment when it is absent.

**Port 8080 is already allocated.** Add or change `WATCHER_PORT`, for example to `8081`, then
redeploy and use that port in the browser.

**The container is healthy but the status page shows a configuration error.** This is expected on
first start. Open the authenticated configuration page, save the required search URL and use-case,
then select **Run now**. The service also retries automatically after 60 seconds.

**The Git clone fails.** Confirm the repository URL, branch reference, and saved Git credential.
For GitHub private repositories with 2FA, use a personal access token rather than the account
password.

**A Git update does not deploy.** Confirm GitOps updates are enabled for the correct branch, or use
the stack's manual pull/redeploy action. Inspect the Portainer logs for clone or build failures.

**The container is unhealthy.** Read the container logs and verify that port 8080 is available
inside the container. The Docker health check calls `http://127.0.0.1:8080/healthz` and does not
require the admin token.

**No Telegram message arrives.** Check that both Telegram fields are configured, that the bot has
received an initial message from the target chat, and that model actions and confidence thresholds
permit notification. Evaluations remain available in `evaluations.jsonl` even when Telegram fails.

**The environment is Docker Swarm.** Do not use this source-building Compose stack. Publish the
image to a registry and deploy an `image:`-based stack instead.

The Portainer workflow described here follows Portainer's official Git repository and GitOps stack
documentation: <https://docs.portainer.io/user/docker/stacks/add#option-3-git-repository>.
