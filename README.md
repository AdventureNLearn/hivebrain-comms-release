# HiveBrainComms

## Risks / read first

The real security boundary is a **virtual machine** or a **separate Windows user account**. Signing keys, the session token, and WebView2 profiles are only as isolated as that account. DPAPI stops other Windows users from reading the keys; it does not stop other programs running as you.

This is a **local-only** tool. The hub binds 127.0.0.1. It is not a multi-user service, not a remote agent host, and not safe to expose on a LAN or the public internet.

What a stolen or leaked handle can do:

- **Session token** (`.session-token`). Anyone who can read it can call the hub API on that loopback port for this launch: claim jobs, inspect state, and drive the same actions the app does. Treat it like a login to the hub process.
- **Approvals**. An Approve on a lab job is the human go-ahead to run it. A yes here means the pane may execute the signed job. Do not approve text you did not read.
- **Build panes**. These are real terminals on this PC. They run commands as the current Windows user, in the configured build folder, with that user's files and credentials. A job that reaches a Build pane can do anything you can do in a shell.

If you would not give a stranger a shell on this account, run the app in a VM or on a dedicated Windows user.

**One window to run your AI team.**

**For testers:** see [TESTERS.md](TESTERS.md) for install steps, requirements, and signing in with your own accounts.

## What it is

HiveBrainComms is a free, open-source (MIT) Windows app that brings your AI agents and tools into one window on your own PC. SuperGrok, X, and Imagine sit in tabs that keep your sign-ins, your Grok build terminal runs inside the app (up to 8 sessions side by side), and a routing view shows where each job went and when.

## The idea: hub-first routing

Many multi-agent setups get messy because every agent talks to every other one and work gets lost. Here, every request goes to one hub first. The hub decides who owns it and passes it on. A job that reaches the wrong agent is sent back to the hub instead of being run quietly.

## How it works

- **Plain-text jobs.** Each job is a readable block in a text file that says who sent it, who it's for, when, and what "done" means. You can read every job and every return.
- **Signed jobs.** Jobs carry an HMAC-SHA256 signature made with a key that only your Windows account can read. The hub checks it before approve and again before run, so a faked or edited job is refused.
- **Routed only.** The build agent is instructed to run only jobs the hub has routed, and it writes back pickup and finish times.
- **Start All.** One button turns on the hub if it's down, opens the build session, and opens Grok Bot minimized. A second click focuses what's already open instead of opening copies.

## What it does for you

- **Less window chaos.** Your tools and terminals live in one place.
- **You can see everything.** What was asked, who's on it, and what came back are all in readable files and the routing view.
- **You stay in control.** The agents follow a rule that nothing is sent, posted, pushed, deleted, or made public without your yes, and lab jobs in the hub need your Approve.
- **Work doesn't vanish.** Skipped or misrouted jobs get flagged and rerouted instead of dropped.
- **No API bill.** It runs on the subscriptions you already have (SuperGrok and the Grok CLI), not pay-per-call API keys.

## Security

- The hub listens only on 127.0.0.1, and its API needs a random per-launch token.
- Signing keys are encrypted with Windows DPAPI and locked to your Windows account. They are never written to the bus, the logs, or git.
- Each web tab has its own separate profile. The app never injects scripts, reads page content, or types into web tabs. Only you click and type there.
- Before release it was tested for routing, wrong-owner jobs, spoofed and bad-signature jobs, blocked actions, and leak scans of the repo.

## Who it's for

People running several AI agents or terminals who want them coordinated and easy to watch, without giving up control.

## Status

A living project. It's a work in progress shaped by testers. Setup is in [TESTERS.md](TESTERS.md), and feedback goes in GitHub Issues.

## Run

From this folder, on Windows:

```
dotnet build shell\Shell.csproj -c Release
pip install -r requirements.txt
Open-HiveBrainComms-Shell.cmd
```

The first command builds `Shell.dll`. The launcher stops until that file exists. `pip` installs the hub packages. Then `Open-HiveBrainComms-Shell.cmd` starts the hub if it's down, then opens the app. The Auto-start hub checkbox is off unless you turn it on in settings. The app runs through the .NET 8 host (`dotnet Shell.dll`), which also works on PCs where Smart App Control blocks unsigned programs. The hub page is http://127.0.0.1:8107/. Live uses the default port 8107. `HBC_HUB_PORT` is process-only (an integer 1024-65535 on 127.0.0.1); never `setx` it. A missing or refused value stays 8107 and is logged. Staging is never inferred from the port: it requires process-only `HBC_STAGING=1` and a `.hbc-staging` marker in ROOT.

The app has tabs for SuperGrok, Imagine, X, Freebots, Build, and Hub. The Build tab holds up to 8 sessions in an automatic grid. The first session is the bus watcher, and extra sessions are added only with the button in that tab.

## Bus

The hub's lab bus uses files in `bus/` (this is separate from any live bus you run). Each sender writes its own file, and results go to the matching return file. A job block needs `id`, `from`, `to`, `lane`, `priority`, `time`, `job`, and `done-when`, plus `v: 2`, a `routed` line, and a `sig` line on a signed bus. Format is v2. `pane` is optional: when it is missing it is signed empty and the hub derives it from the signed `to` and `lane`. The `from` line must match the file owner. A malformed or unsigned block is refused and not queued. `tools/Sign-BusJob.ps1` signs and verifies v2 jobs only. The hub is always strict, with no soft window, no date logic, and no off switch. See `docs/SIGNING.md`.

## Web panels

SuperGrok, X, Imagine, and Freebots are plain browser views. The app doesn't inject scripts, read page content, or strip framing headers, and the queue and build agent never click or type in those panels.

## Off by default

The app doesn't read, relay, or show Freebots or HiveBrain game chat. The Freebots tab is only a plain view of the site.

## Limits

- Windows only.
- It needs the Grok CLI, SuperGrok, and the Grok Bot desktop app. Grok Bot can't be embedded, so it opens beside the app, minimized.
- Agents still have to follow lane and routing rules.
- The banned-verb list is `hub/banned-verbs.txt` only. The signer and the hub both load that file. The list has to be complete.
- DPAPI keys only protect against other Windows users, not against code running as the same user.
- A stolen key can sign valid jobs.
- The bus folder's own file permissions still matter.
- The hub is always strict: no soft window, no date logic, and no off switch. `-EnforceAt` on the script is optional (full ISO time with offset; missing, unreadable, or past values enforce now). See rule 13 in the user's bus protocol file.
- `HBC_PASS_GROK_AUTH` is off by default. Set it only if a Build terminal must inherit `XAI_API_KEY` / `GROK_API_KEY`. Lab panes never receive those keys.
- `HBC_BUILD_AUTOREOPEN` is off by default. Set it only in the PC config that should reopen Build sessions after a hub restart. The hub binds 127.0.0.1 first. A Build pane already open in this hub process is not started again.

## Does the routing help?

Testers: does this routing method help you? Say so in a GitHub Issue. A yes, a no, and a short note about where a job got stuck are all useful.

## How to give feedback

Open a GitHub Issue. Name the job id if you have one, say what you expected, and say what happened.

## License

MIT. See `LICENSE`. Copyright (c) 2026 AdventureNLearn.

## Credits

HiveBrainComms was built by AdventureNLearn, inspired by GodTerm by Daniel Farina (MIT, github.com/daniel-farina/godterm). No upstream file is copied here, and this is not an endorsement.

If you use or build on this hub-first routing method, a small credit line like "Routing method from HiveBrainComms by AdventureNLearn" is appreciated but not required.
