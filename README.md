# Tally

Automated Talos OS bare-metal bootstrapping on Hetzner Robot servers.

## Install

Tally installs as a standalone CLI with [uv](https://docs.astral.sh/uv/). If you
don't have uv yet:

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Then install Tally from the repository:

```sh
uv tool install git+https://github.com/bhark/tally
```

Upgrade or remove it with:

```sh
# upgrade
uv tool upgrade tally

# uninstall
uv tool uninstall tally
```

## Usage

Run `tally` from inside your GitOps repository:

```sh
cd my-gitops-repo
tally
```

It writes two directories into the current working directory:

- `talos/` - the git-safe cluster definition (`tally.yaml` + generated
  fragments). **Commit this.**
- `talos-secrets/` - CA keys, tokens, rendered configs, `talosconfig`/`kubeconfig`
  and build artifacts. Hardened to `0700`/`0600` and auto-added to a root
  `.gitignore` - never commit it.

Tally is stateless: rerun it any time to add nodes or resume a bring-up; state
lives in the on-disk artifacts, not a progress file.
