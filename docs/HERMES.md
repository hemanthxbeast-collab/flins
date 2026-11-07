# Hermes builds FLINS skills

Hermes Agent runs in WSL2 and builds skills through FLINS's skill-dev MCP server. You review
and switch them on in the FLINS dashboard. Hermes can't switch anything on, and it can't see your
messages, contacts, settings or keys.

```
 Windows                                         WSL2 (Ubuntu)
 ──────────────────────────────────────          ─────────────────────────────────────
 FLINS (flins run)                               Hermes Agent
   dashboard 127.0.0.1:8765  ← you review          └─ MCP: flins skilldev (stdio)
   data/, .env, config/      (Hermes can't see)         └─ ~/flins (a copy of this repo)
   skills/_incoming/  ◄────── shared folder ─────►  /mnt/flins-incoming
```

## Why the setup is strict
The dashboard's page token stops other **websites**. It doesn't stop a program on the same PC
that can reach `127.0.0.1:8765`. That program could read the token and approve things. So
Hermes gets four walls:

1. **No Windows files.** Drives aren't mounted in WSL; only `skills/_incoming` is.
2. **No Windows programs.** WSL interop is off, so Hermes can't run `flins.exe` or `powershell.exe`.
3. **No dashboard.** WSL stays on its default **NAT** networking. In NAT mode WSL can't reach
   Windows' `127.0.0.1`. Don't switch on `networkingMode=mirrored`; `flins doctor` warns if you do.
4. **No messaging.** Hermes' own messaging gateways stay off, so it can't send on WhatsApp/Telegram itself.

Everything Hermes hands over is re-checked on Windows before you can approve it, and approval is
tied to the exact files you looked at.

## 1. Copy FLINS into WSL (before turning off the drives)
In a WSL terminal:
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh      # uv, if you don't have it
git clone /mnt/c/Users/Hemanth/Documents/Flins ~/flins
cd ~/flins && uv sync
mkdir -p /mnt/c/Users/Hemanth/Documents/Flins/skills/_incoming
```
The copy is only used for the skill-dev server. It never gets your `config/*.yaml`, `.env` or
`data/` (they aren't in git), and the safety eval inside it uses the example policy.

## 2. Lock WSL down
Edit `/etc/wsl.conf` (`sudo nano /etc/wsl.conf`):
```ini
[automount]
enabled = false        # don't mount C:\ and other drives
mountFsTab = true      # but do mount what /etc/fstab lists

[interop]
enabled = false        # can't start Windows programs
appendWindowsPath = false
```
Add the one shared folder to `/etc/fstab` (`sudo nano /etc/fstab`):
```
C:/Users/Hemanth/Documents/Flins/skills/_incoming  /mnt/flins-incoming  drvfs  rw,noatime,uid=1000,gid=1000  0 0
```
```bash
sudo mkdir -p /mnt/flins-incoming
```
Then restart WSL from **Windows** PowerShell: `wsl --shutdown`, and open WSL again. Check:
```bash
ls /mnt/c                  # should fail: No such file or directory
cmd.exe /c ver             # should fail: command not found / cannot execute
ls /mnt/flins-incoming     # works (empty at first)
```
If the fstab line doesn't mount, try `sudo mount -t drvfs 'C:\Users\Hemanth\Documents\Flins\skills\_incoming' /mnt/flins-incoming`
once to see the error.

Also check `%USERPROFILE%\.wslconfig` on Windows doesn't contain `networkingMode=mirrored`.

To update `~/flins` later, pull from your Git remote (GitHub), or briefly turn automount back on.

## 3. Hermes
Install Hermes Agent by its own instructions and pick whatever model provider you like.

- **Gateways off:** don't set up or start Hermes' messaging gateway, and don't give it WhatsApp,
  Telegram, Discord, Slack or e-mail credentials. FLINS is the only thing connected to your WhatsApp.
- **Add the FLINS MCP server.** Hermes reads MCP servers from its config file (`~/.hermes/config.yaml`).
  A stdio server is a command plus arguments:
  ```yaml
  mcp_servers:
    flins-skilldev:
      command: /home/<you>/flins/.venv/bin/flins
      args: ["skilldev", "--incoming", "/mnt/flins-incoming"]
  ```
  If your Hermes version names these keys differently, follow its MCP docs. What matters is the
  command line: `~/flins/.venv/bin/flins skilldev --incoming /mnt/flins-incoming`.

Test the server by hand: `~/flins/.venv/bin/flins skilldev --incoming /mnt/flins-incoming`
should print nothing and wait (it talks MCP on stdin/stdout). Press Ctrl+C.

## 4. Use it
Ask Hermes, for example: *"Use the flins-skilldev tools to build a FLINS skill that, while I'm
away, answers 'where are you?' with when I'll be back. Read get_skill_spec first."*

Or ask FLINS by voice or in your self-chat: *"make a skill that replies to Mom's 'did you eat'
with a warm message"*. It's saved for Hermes, and you then tell Hermes *"check FLINS skill
requests"*. The request text goes to Hermes (and its AI provider), so keep it general.

Hermes then:
1. reads `get_skill_spec` and `list_capabilities`,
2. `scaffold_skill`, then `write_skill_file` for the manifest, code and test fixtures,
3. runs `test_skill` (validation, fixtures in the sandbox, 162-case safety eval with the skill on),
4. `submit_skill` with a short summary.

You open the dashboard → **Skills** (it shows "1 to review") → **Review**. You see what the skill
may do, what's new compared with the version you approved before, the full diff, and the test
results, all run again on Windows. Hermes' summary is shown separately, marked as unchecked.
**Approve and enable** runs the safety eval once more, copies the skill into `skills/<name>`,
pins its hash in `skills.lock` and writes the audit log. **Reject** keeps it off and leaves your
note for Hermes (`list_skills` shows it).

## What Hermes can and can't do
| Can | Can't |
|---|---|
| read the installed skills as examples | enable, approve or disable any skill |
| write files in `_incoming/<name>/` only (plain names, .py/.md/.yaml/.json/.txt, size-limited, no links) | write anywhere else (`..`, absolute paths, drive letters and links are refused) |
| validate, test and submit its skills | read `data/`, `.env`, `config/`, messages or contacts |
| read your skill requests and review notes | change policy, modes or contact tiers |
| | send anything: skills have no send permission at all |

## Without Hermes
The same server works with any MCP client, e.g. Claude Code on Windows:
`uv run flins skilldev` (it uses `skills/_incoming` by default). That runs on Windows itself, so
none of the WSL walls apply. Use it only with a client you trust.
